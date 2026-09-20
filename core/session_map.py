"""推送消息与 Crisp 会话的映射：管理员回复 Telegram 推送消息时，
通过 (chat_id, message_id) 反查会话 ID，卡片上无需再展示 Session 文本。"""
import threading
from collections import OrderedDict

CAPACITY = 5000

_lock = threading.Lock()
_map = OrderedDict()


def record(chat_id, message_id, session_id):
    if chat_id is None or message_id is None or not session_id:
        return
    with _lock:
        _map[(chat_id, message_id)] = session_id
        _map.move_to_end((chat_id, message_id))
        while len(_map) > CAPACITY:
            _map.popitem(last=False)


def lookup(chat_id, message_id):
    with _lock:
        return _map.get((chat_id, message_id))


# ---------- 会话 → 用户邮箱（供控制台消息记录展示） ----------

CAPACITY_EMAIL = 2000
_email_map = OrderedDict()


def record_email(session_id, email):
    if not session_id or not email:
        return
    with _lock:
        _email_map[session_id] = email
        _email_map.move_to_end(session_id)
        while len(_email_map) > CAPACITY_EMAIL:
            _email_map.popitem(last=False)


def lookup_email(session_id):
    with _lock:
        return _email_map.get(session_id)


# ---------- 会话首次欢迎语追踪（支持按小时冷却与防重复打扰） ----------

CAPACITY_WELCOME = 5000
_welcome_map = OrderedDict()  # session_id -> timestamp (float)


def is_welcome_needed(session_id, ttl_hours=24):
    """检查指定会话是否需要发送欢迎语（若在 ttl_hours 内已发送过则返回 False）。"""
    if not session_id:
        return False
    import time
    now = time.time()
    ttl_seconds = max(0.1, float(ttl_hours or 24)) * 3600.0
    with _lock:
        last_time = _welcome_map.get(session_id)
        if last_time is None or (now - last_time) >= ttl_seconds:
            return True
        return False


def mark_welcomed(session_id):
    """标记会话已发送欢迎语（记录当前时间戳）。"""
    if not session_id:
        return
    import time
    now = time.time()
    with _lock:
        _welcome_map[session_id] = now
        _welcome_map.move_to_end(session_id)
        while len(_welcome_map) > CAPACITY_WELCOME:
            _welcome_map.popitem(last=False)


# ---------- Telegram 话题（Topics / Forum）双向映射 ----------

CAPACITY_TOPIC = 5000
_topic_map = OrderedDict()        # (chat_id, session_id) -> topic_id (int)
_topic_reverse_map = OrderedDict() # (chat_id, topic_id) -> session_id (str)


def record_topic(chat_id, session_id, topic_id):
    """记录 (chat_id, session_id) 与 Telegram Topic ID 的对应关系。"""
    if chat_id is None or not session_id or topic_id is None:
        return
    with _lock:
        _topic_map[(chat_id, session_id)] = int(topic_id)
        _topic_map.move_to_end((chat_id, session_id))
        while len(_topic_map) > CAPACITY_TOPIC:
            _topic_map.popitem(last=False)

        _topic_reverse_map[(chat_id, int(topic_id))] = session_id
        _topic_reverse_map.move_to_end((chat_id, int(topic_id)))
        while len(_topic_reverse_map) > CAPACITY_TOPIC:
            _topic_reverse_map.popitem(last=False)


def lookup_topic(chat_id, session_id):
    """查询指定会话在群组中已存在的话题 ID（不存在则返回 None）。"""
    with _lock:
        return _topic_map.get((chat_id, session_id))


def lookup_session_by_topic(chat_id, topic_id):
    """根据群组 ID 和 Topic ID 反查对应的 Crisp session_id。"""
    if chat_id is None or topic_id is None:
        return None
    with _lock:
        return _topic_reverse_map.get((chat_id, int(topic_id)))

