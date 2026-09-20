"""REST 模式：定时轮询 Crisp 未读会话并推送到 Telegram。"""
import logging

from core import session_map
from core.logbus import bus
from core.runtime import runtime
from core.templates import (build_push_text, is_welcome_enabled,
                            match_autoreply, render_welcome_message)

log = logging.getLogger('mod.getUnread')


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

    # 构造卡片文本
    text = build_push_text(metas, text_contents if len(text_contents) > 1 else (text_contents[0] if text_contents else ''),
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

    # 推送 Telegram
    for admin_id in config['bot']['admin_id']:
        if image_urls and len(text_contents) == len(image_urls):
            sent = await context.bot.send_photo(
                chat_id=admin_id,
                photo=image_urls[0],
                caption=build_push_text(metas, '', image_only=True, timestamp=latest_ts or None),
                parse_mode='HTML',
            )
        else:
            sent = await context.bot.send_message(chat_id=admin_id, text=text, parse_mode='HTML')
        session_map.record(admin_id, getattr(sent, 'message_id', None), session_id)

    # 批量标记已读
    if fingerprints:
        client.website.mark_messages_read_in_conversation(website_id, session_id, {
            'from': 'user',
            'origin': 'chat',
            'fingerprints': fingerprints,
        })

    log.info('已推送聚合文本消息到 Telegram（会话 %s，共 %d 条）', session_id, len(messages))
    bus.event('message_in', ' | '.join(text_contents) if text_contents else '[多媒体消息]',
              session_id=session_id, msg_type='batch' if len(messages) > 1 else 'text',
              status='ok', email=metas.get('email'))
