"""REST 模式：定时轮询 Crisp 未读会话并推送到 Telegram。"""
import logging

from core import session_map, tg_compat
from core.logbus import bus
from core.runtime import runtime
from core.templates import (build_push_text, build_topic_name, clean_nickname,
                            is_welcome_enabled, match_autoreply,
                            render_welcome_message)

log = logging.getLogger('mod.getUnread')


def _clean_nickname(client, config, website_id, session_id, metas):
    """面板主题常把「用户名 VIP等级 邮箱」整串塞进昵称，Crisp 会话标签就特别长；
    这里清洗后回写 Crisp，让会话标题只显示用户名。"""
    if not (config.get('crisp') or {}).get('clean_nickname', True):
        return
    raw = str(metas.get('nickname') or '').strip()
    clean = clean_nickname(raw)
    if not clean or clean == raw:
        return
    try:
        client.website.update_conversation_metas(website_id, session_id, {'nickname': clean})
        metas['nickname'] = clean
        log.info('已把会话 %s 的昵称「%s」清洗为「%s」', session_id, raw, clean)
    except Exception as err:
        log.warning('清洗会话 %s 昵称失败：%s', session_id, err)


class Conf:
    desc = '推送未读新消息'
    method = 'repeating'
    interval = 60


def enabled(config):
    return config['crisp']['msgapi'] == 'rest'


def get_interval(config):
    try:
        return max(5, int(config['crisp'].get('poll_interval') or 60))
    except (TypeError, ValueError):
        return 60


async def exec(context):
    config = runtime.get_config()
    client = runtime.get_crisp_client()
    if config is None or client is None:
        log.warning('运行时未就绪，本轮跳过')
        return
    website_id = config['crisp']['website']

    conversations = client.website.search_conversations(website_id, 1, filter_unread='1')
    if len(conversations) == 0:
        return
    for conversation in conversations:
        session_id = conversation['session_id']
        messages = client.website.get_messages_in_conversation(website_id, session_id, {})
        metas = client.website.get_conversation_metas(website_id, session_id)
        _clean_nickname(client, config, website_id, session_id, metas)
        session_map.record_email(session_id, metas.get('email'))

        unread_msgs = [m for m in messages if len(m.get('read', [])) == 0]
        if not unread_msgs:
            continue

        try:
            await _push_batch(context, client, config, website_id, session_id, metas, unread_msgs)
        except Exception as err:
            log.exception('处理 Crisp 消息失败（会话 %s）', session_id)
            bus.event('error', f'处理 Crisp 消息失败：{err}', session_id=session_id)


async def _push_batch(context, client, config, website_id, session_id, metas, messages):
    """将同会话中全部未读消息聚合为单卡片推送到 Telegram。"""
    if not messages:
        return

    text_contents = []
    image_urls = []
    fingerprints = []
    latest_ts = 0

    for msg in messages:
        fp = msg.get('fingerprint')
        if fp:
            fingerprints.append(fp)
        ts = msg.get('timestamp') or 0
        if ts > latest_ts:
            latest_ts = ts
        msg_type = msg.get('type')
        if msg_type == 'text':
            c = msg.get('content')
            if c:
                text_contents.append(str(c))
        elif msg_type == 'file' and 'image' in str((msg.get('content') or {}).get('type', '')):
            url = (msg.get('content') or {}).get('url')
            if url:
                image_urls.append(url)
                text_contents.append('[图片]')

    # 检查自动回复与欢迎语匹配
    real_texts = [c for c in text_contents if c != '[图片]']
    welcome_cfg = config.get('welcome') or {}
    ttl_hours = welcome_cfg.get('ttl_hours', 24)
    welcome_active = is_welcome_enabled(welcome_cfg) and session_map.is_welcome_needed(session_id, ttl_hours)

    reply_to_send = ''
    for c in text_contents:
        if c != '[图片]':
            matched, autoreply = match_autoreply(config.get('autoreply'), c, metas=metas)
            if matched:
                reply_to_send = autoreply
                session_map.mark_welcomed(session_id)
                break

    if not reply_to_send and welcome_active:
        reply_to_send = render_welcome_message(welcome_cfg, metas=metas)
        session_map.mark_welcomed(session_id)

    # 构造卡片文本（图片占位符不进正文，图片单独逐张推送）
    text = build_push_text(metas, real_texts,
                           autoreply=reply_to_send,
                           timestamp=latest_ts or None)

    # 发送自动回复
    if reply_to_send:
        client.website.send_message_in_conversation(website_id, session_id, {
            'type': 'text',
            'content': reply_to_send,
            'from': 'operator',
            'origin': 'chat',
        })
        bus.event('autoreply', reply_to_send, session_id=session_id, email=metas.get('email'))
        log.info('会话 %s 触发自动回复/欢迎语', session_id)

    # 推送 Telegram（支持群话题 Topics 模式）
    enable_topics = bool(config.get('bot', {}).get('topics'))
    token = config['bot']['token']
    proxy = str(config['bot'].get('proxy') or '').strip()

    for admin_id in config['bot']['admin_id']:
        thread_id = None
        if enable_topics:
            thread_id = session_map.lookup_topic(admin_id, session_id)
            if thread_id is None:
                topic_title = build_topic_name(metas, session_id)
                try:
                    thread_id = await tg_compat.create_forum_topic(
                        token=token,
                        chat_id=admin_id,
                        name=topic_title,
                        proxy=proxy,
                    )
                    if thread_id:
                        session_map.record_topic(admin_id, session_id, thread_id)
                except Exception as err:
                    log.warning('为会话 %s 创建群话题失败：%s，降级为普通群消息', session_id, err)
                    thread_id = None

        sent_ids = []
        try:
            has_text = bool(real_texts)
            # 先发文字卡片；图片随后逐张补发（旧逻辑图文混发时整张图片会被丢弃）
            if has_text:
                card_id = await tg_compat.send_message(
                    token=token,
                    chat_id=admin_id,
                    text=text,
                    parse_mode='HTML',
                    message_thread_id=thread_id,
                    proxy=proxy,
                )
                if card_id:
                    sent_ids.append(card_id)

            for img_idx, url in enumerate(image_urls):
                # 纯图片批次：首张图片附带用户资料说明；图文混发时后续图片不带说明
                caption = ''
                if not has_text and img_idx == 0:
                    caption = build_push_text(metas, '', image_only=True, timestamp=latest_ts or None)
                photo_id = await tg_compat.send_photo(
                    token=token,
                    chat_id=admin_id,
                    photo_url=url,
                    caption=caption,
                    parse_mode='HTML',
                    message_thread_id=thread_id,
                    proxy=proxy,
                )
                if photo_id:
                    sent_ids.append(photo_id)
        except Exception as send_err:
            log.error('推送 Telegram 失败（目标 %s）：%s', admin_id, send_err)
            bus.event('error', f'推送 Telegram 失败：{send_err}', session_id=session_id)

        for mid in sent_ids:
            session_map.record(admin_id, mid, session_id)
        if thread_id is not None:
            session_map.record_topic(admin_id, session_id, thread_id)

    # 批量标记已读
    if fingerprints:
        client.website.mark_messages_read_in_conversation(website_id, session_id, {
            'from': 'user',
            'origin': 'chat',
            'fingerprints': fingerprints,
        })

    log.info('已推送到 Telegram（会话 %s，文字 %d 条 + 图片 %d 张）',
             session_id, len(real_texts), len(image_urls))
    bus.event('message_in', ' | '.join(text_contents) if text_contents else '[图片]',
              session_id=session_id,
              msg_type='image' if image_urls and not real_texts else ('batch' if len(messages) > 1 else 'text'),
              status='ok', email=metas.get('email'))
