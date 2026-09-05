"""REST 模式：定时轮询 Crisp 未读会话并推送到 Telegram。"""
import logging

from core import session_map
from core.logbus import bus
from core.runtime import runtime
from core.templates import build_push_text, match_autoreply

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
        # Crisp api docs: Returns the last batch of messages. 这个last batch到底能有多少我没整明白.
        messages = client.website.get_messages_in_conversation(website_id, session_id, {})
        metas = client.website.get_conversation_metas(website_id, session_id)
        for message in messages:
            # read长度为0时该条消息未读
            if len(message['read']) != 0:
                continue
            try:
                if message['type'] == 'text':
                    await _push_text(context, client, config, website_id, session_id, metas, message)
                elif message['type'] == 'file' and 'image' in str(message['content']['type']):
                    await _push_image(context, client, config, website_id, session_id, message)
                else:
                    log.info('忽略未处理的消息类型：%s（会话 %s）', message['type'], session_id)
            except Exception as err:
                log.exception('处理 Crisp 消息失败（会话 %s）', session_id)
                bus.event('error', f'处理 Crisp 消息失败：{err}', session_id=session_id)


async def _push_text(context, client, config, website_id, session_id, metas, message):
    # 通过消息指纹将消息置为已读
    mark_read(client, website_id, session_id, message['fingerprint'])

    matched, autoreply = match_autoreply(config.get('autoreply'), message['content'])
    text = build_push_text(metas, message['content'],
                           autoreply=autoreply if matched else '',
                           timestamp=message.get('timestamp'))

    if matched:
        client.website.send_message_in_conversation(website_id, session_id, {
            'type': 'text',
            'content': autoreply,
            'from': 'operator',
            'origin': 'chat',
        })
        bus.event('autoreply', autoreply, session_id=session_id)
        log.info('会话 %s 命中自动回复', session_id)

    for admin_id in config['bot']['admin_id']:
        sent = await context.bot.send_message(chat_id=admin_id, text=text, parse_mode='HTML')
        session_map.record(admin_id, getattr(sent, 'message_id', None), session_id)

    log.info('已推送文本消息到 Telegram（会话 %s）', session_id)
    bus.event('message_in', message['content'], session_id=session_id,
              msg_type='text', status='ok')


async def _push_image(context, client, config, website_id, session_id, message):
    # 通过消息指纹将消息置为已读
    mark_read(client, website_id, session_id, message['fingerprint'])

    text = build_push_text({}, '', image_only=True, timestamp=message.get('timestamp'))
    for admin_id in config['bot']['admin_id']:
        sent = await context.bot.send_photo(
            chat_id=admin_id,
            photo=message['content']['url'],
            caption=text,
            parse_mode='HTML',
        )
        session_map.record(admin_id, getattr(sent, 'message_id', None), session_id)

    log.info('已推送图片消息到 Telegram（会话 %s）', session_id)
    bus.event('message_in', '[图片]', session_id=session_id, msg_type='image', status='ok')


def mark_read(client, website_id, session_id, fingerprint):
    client.website.mark_messages_read_in_conversation(website_id, session_id, {
        'from': 'user',
        'origin': 'chat',
        'fingerprints': [fingerprint],
    })
