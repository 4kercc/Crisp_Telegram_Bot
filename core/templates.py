"""Crisp→Telegram 推送消息模板与自动回复匹配（getUnread 与 crispEventsHandler 共用）。"""
import html
import time


def escape(value):
    return html.escape(str(value)) if value is not None else ''


def format_timestamp(timestamp):
    """Crisp 消息时间戳（秒，兼容毫秒）→ 本地时间字符串，无效时返回 None。"""
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 1e12:  # 毫秒时间戳
        ts /= 1000.0
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


def match_autoreply(autoreply_config, content):
    """关键词匹配：键支持 | 分隔多个关键词，命中任意即返回对应回复。"""
    if isinstance(autoreply_config, dict):
        for keys, reply in autoreply_config.items():
            for keyword in str(keys).split('|'):
                if keyword and keyword in str(content):
                    return True, reply
    return False, ''


def build_push_text(metas, content, session_id, autoreply='', image_only=False, timestamp=None):
    """构造推送进 Telegram 的 HTML 消息文本。"""
    metas = metas or {}
    text = '📠<b>Crisp消息推送</b>\n'
    if not image_only:
        email = metas.get('email') or ''
        if len(email) > 0:
            text += f'📧<b>电子邮箱</b>：{escape(email)}\n'
        data = metas.get('data') or {}
        if isinstance(data, dict) and len(data) > 0:
            if 'Plan' in data:
                text += f'🪪<b>使用套餐</b>：{escape(data["Plan"])}\n'
            if 'UsedTraffic' in data and 'AllTraffic' in data:
                text += (f'🗒<b>流量信息</b>：{escape(data["UsedTraffic"])}'
                         f' / {escape(data["AllTraffic"])}\n')
        text += f'🧾<b>消息内容</b>：{escape(content)}\n'
        if autoreply:
            text += f'💡<b>自动回复</b>：{escape(autoreply)}\n'
    message_time = format_timestamp(timestamp)
    if message_time:
        text += f'🕒<b>发送时间</b>：{message_time}\n'
    # Session 打个码
    text += f'\n🧷<b>Session</b>：<tg-spoiler>{session_id}</tg-spoiler>'
    return text
