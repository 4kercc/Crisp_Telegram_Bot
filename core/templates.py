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


def _profile_lines(data):
    """把 Crisp session:data 里的用户资料渲染成卡片行（有则显示，无则跳过）。

    兼容两套键名：
      SSPanel 主题推送：VIP / Used / Traffic / VIP_Time / Reg_Time / Money
      v2board 脚本推送：Plan / UsedTraffic / AllTraffic
    """
    lines = []

    profile = []
    if data.get('VIP') not in (None, ''):
        profile.append(f'🪪<b>VIP等级</b>：{escape(data["VIP"])}')
    if data.get('Money'):
        profile.append(f'💰<b>账户余额</b>：{escape(data["Money"])}')
    if profile:
        lines.append('  '.join(profile))

    if data.get('Plan'):
        lines.append(f'🪪<b>使用套餐</b>：{escape(data["Plan"])}')

    used = data.get('Used') or data.get('UsedTraffic')
    remain = data.get('Traffic') or data.get('AllTraffic')
    if used and remain:
        lines.append(f'📊<b>已用|剩余</b>：{escape(used)} | {escape(remain)}')
    elif used:
        lines.append(f'📊<b>已用流量</b>：{escape(used)}')
    elif remain:
        lines.append(f'📉<b>剩余流量</b>：{escape(remain)}')

    if data.get('VIP_Time'):
        lines.append(f'⏳<b>套餐到期</b>：{escape(data["VIP_Time"])}')
    if data.get('Reg_Time'):
        lines.append(f'📅<b>注册时间</b>：{escape(data["Reg_Time"])}')
    return lines


def build_push_text(metas, content, autoreply='', image_only=False, timestamp=None):
    """构造推送进 Telegram 的 HTML 消息文本。"""
    metas = metas or {}
    lines = []

    email = metas.get('email') or ''
    if email:
        lines.append(f'📧<b>电子邮箱</b>：{escape(email)}')
    data = metas.get('data') or {}
    if isinstance(data, dict):
        lines.extend(_profile_lines(data))
    message_time = format_timestamp(timestamp)
    if message_time:
        lines.append(f'🕒<b>发送时间</b>：{message_time}')

    text = '\n'.join(lines)
    if image_only:
        return text

    body = f'🧾<b>消息内容</b>：{escape(content)}'
    if autoreply:
        body += f'\n💡<b>自动回复</b>：{escape(autoreply)}'
    if text:
        text += '\n\n'
    return text + body
