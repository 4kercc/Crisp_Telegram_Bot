"""Crisp→Telegram 推送消息模板与自动回复匹配（getUnread 与 crispEventsHandler 共用）。"""
import html
import re
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


def _can_be_number(s):
    if s is None:
        return False
    s = str(s).strip()
    return bool(re.search(r'[-+]?\d+(?:\.\d+)?', s))


def _extract_number(val):
    if val is None or val == '':
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    match = re.search(r'[-+]?\d+(?:\.\d+)?', s)
    if match:
        try:
            return float(match.group(0))
        except (ValueError, OverflowError):
            pass
    return 0.0


def _eval_condition(field, op, val, metas):
    """对访客元数据（如 session:data 里的 VIP、Money 或顶层 email）进行条件判断。"""
    field = str(field or '').strip()
    if not field or field in ('*', 'any', 'all', 'none', ''):
        return True

    metas = metas or {}
    data = (metas.get('data') or {}) if isinstance(metas.get('data'), dict) else {}

    # 优先从 data (session:data) 获取，其次顶层 metas
    user_val = data.get(field)
    if user_val is None:
        user_val = metas.get(field)

    op = str(op or '*').strip().lower()
    val_str = str(val if val is not None else '').strip()

    if op in ('*', '', 'none', 'always', 'any'):
        return True
    if op in ('exists', 'not_empty', 'set'):
        return user_val is not None and str(user_val).strip() != ''
    if op in ('empty', 'not_set', 'null'):
        return user_val is None or str(user_val).strip() == ''
    if op in ('contains', 'in'):
        return val_str.lower() in str(user_val or '').lower()

    # 数值比较
    if op in ('>', '>=', '<', '<='):
        u_num = _extract_number(user_val)
        r_num = _extract_number(val_str)
        if op == '>':
            return u_num > r_num
        elif op == '>=':
            return u_num >= r_num
        elif op == '<':
            return u_num < r_num
        elif op == '<=':
            return u_num <= r_num

    # 相等 / 不等
    if op in ('==', '=', 'eq'):
        u_str = str(user_val if user_val is not None else '').strip()
        if u_str.lower() == val_str.lower():
            return True
        if _can_be_number(u_str) and _can_be_number(val_str):
            return _extract_number(u_str) == _extract_number(val_str)
        if (user_val is None or u_str == '') and _can_be_number(val_str) and _extract_number(val_str) == 0.0:
            return True
        return False

    if op in ('!=', '<>', 'ne'):
        u_str = str(user_val if user_val is not None else '').strip()
        if _can_be_number(u_str) and _can_be_number(val_str):
            return _extract_number(u_str) != _extract_number(val_str)
        return u_str.lower() != val_str.lower()

    return True


def _render_reply_template(reply_text, metas):
    """安全替换回复模板中的变量（例如 {VIP}、{email}、{Money} 等）。"""
    if not reply_text or '{' not in reply_text:
        return reply_text or ''
    metas = metas or {}
    data = (metas.get('data') or {}) if isinstance(metas.get('data'), dict) else {}
    ctx = {}
    for k, v in metas.items():
        if isinstance(v, (str, int, float)):
            ctx[k] = str(v)
    for k, v in data.items():
        if v is not None:
            ctx[k] = str(v)

    out = reply_text
    for k, v in ctx.items():
        out = out.replace('{' + k + '}', str(v))
    return out


def render_welcome_message(welcome_config, metas=None):
    """渲染欢迎语内容（支持变量替换）。"""
    if not welcome_config:
        return ''
    if isinstance(welcome_config, dict):
        text = str(welcome_config.get('message') or '').strip()
    else:
        text = str(welcome_config).strip()
    return _render_reply_template(text, metas) if text else ''


def is_welcome_enabled(welcome_config):
    if not welcome_config:
        return False
    if isinstance(welcome_config, dict):
        return bool(welcome_config.get('enabled', True)) and bool(str(welcome_config.get('message') or '').strip())
    return bool(str(welcome_config).strip())


def _match_keyword(pattern, content):
    """大小写不敏感的关键词匹配，支持 | 分隔多个。"""
    if not pattern or content is None:
        return False
    c_lower = str(content).lower()
    for kw in str(pattern).split('|'):
        kw = kw.strip()
        if kw and kw.lower() in c_lower:
            return True
    return False


def match_autoreply(autoreply_config, content, metas=None):
    """自动回复匹配。

    支持两种配置格式：
    1. 列表规则（推荐）：
       [
         {"pattern": "id|苹果id", "field": "VIP", "op": ">", "value": "0", "reply": "这是私有苹果id: ..."},
         {"pattern": "id|苹果id", "field": "VIP", "op": "==", "value": "0", "reply": "请稍等，id需要人工下发。"},
         {"pattern": "在吗|你好", "field": "*", "op": "*", "value": "", "reply": "欢迎使用客服系统..."}
       ]
    2. 键值对字典（向后兼容）：
       {"在吗|你好": "欢迎使用客服系统，请等待客服回复你~"}
    """
    if not autoreply_config or content is None:
        return False, ''

    # 1. 列表规则格式
    if isinstance(autoreply_config, list):
        for rule in autoreply_config:
            if isinstance(rule, dict):
                pattern = rule.get('pattern') or rule.get('keyword') or ''
                field = rule.get('field') or ''
                op = rule.get('op') or rule.get('operator') or '*'
                val = rule.get('value') if rule.get('value') is not None else rule.get('val')
                reply = rule.get('reply') or ''

                if _match_keyword(pattern, content):
                    if _eval_condition(field, op, val, metas):
                        return True, _render_reply_template(reply, metas)
            elif isinstance(rule, (list, tuple)) and len(rule) >= 2:
                # [pattern, reply]
                pattern, reply = rule[0], rule[1]
                if _match_keyword(pattern, content):
                    return True, _render_reply_template(reply, metas)
        return False, ''

    # 2. 传统字典格式
    if isinstance(autoreply_config, dict):
        for keys, reply in autoreply_config.items():
            if _match_keyword(keys, content):
                return True, _render_reply_template(reply, metas)

    return False, ''


def _profile_lines(data, metas=None):
    """把 Crisp session:data 里的用户资料与 metas 渲染成卡片行（有则显示，无则跳过）。

    兼容两套键名：
      SSPanel 主题推送：VIP / Used / Traffic / VIP_Time / Reg_Time / Money
      v2board 脚本推送：Plan / UsedTraffic / AllTraffic
    """
    lines = []
    data = data or {}
    metas = metas or {}

    # 用户名 / 昵称
    nickname = metas.get('nickname') or data.get('Username') or data.get('user_name') or data.get('nickname') or ''

    profile = []
    if data.get('VIP') not in (None, ''):
        vip_text = f'🪪<b>VIP等级</b>：{escape(data["VIP"])}'
        if nickname:
            vip_text += f'（{escape(nickname)}）'
        profile.append(vip_text)
    elif nickname:
        profile.append(f'👤<b>用户名称</b>：{escape(nickname)}')

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
        lines.extend(_profile_lines(data, metas=metas))
    elif metas.get('nickname'):
        lines.append(f'👤<b>用户名称</b>：{escape(metas.get("nickname"))}')

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
