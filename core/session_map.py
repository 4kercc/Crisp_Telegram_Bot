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
