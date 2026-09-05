"""控制台认证：强制密码（首次进入设置向导）、session 会话、登录失败限速。"""
import threading
import time
from functools import wraps

from flask import jsonify, session

from core.runtime import runtime

MAX_ATTEMPTS = 5
LOCK_SECONDS = 60

_state = {'count': 0, 'locked_until': 0.0}
_lock = threading.Lock()


def auth_state():
    cm = runtime.config_manager
    return {
        'setup_required': not cm.has_password(),
        'authed': bool(session.get('authed')),
    }


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        cm = runtime.config_manager
        if not cm.has_password():
            return jsonify(ok=False, error='尚未设置控制台密码', code='setup_required'), 401
        if not session.get('authed'):
            return jsonify(ok=False, error='未登录或会话已过期', code='unauthorized'), 401
        return view(*args, **kwargs)
    return wrapper


def locked_seconds():
    with _lock:
        remaining = _state['locked_until'] - time.time()
        return int(remaining) if remaining > 0 else 0


def register_failure():
    with _lock:
        _state['count'] += 1
        if _state['count'] >= MAX_ATTEMPTS:
            _state['locked_until'] = time.time() + LOCK_SECONDS
            _state['count'] = 0


def register_success():
    with _lock:
        _state['count'] = 0
        _state['locked_until'] = 0.0
