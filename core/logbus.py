"""日志总线：环形缓冲的运行日志与结构化消息事件，供 Web 控制台增量拉取。"""
import logging
import threading
import time
from collections import deque

LOG_CAPACITY = 2000
EVENT_CAPACITY = 2000


class RingBuffer:
    """带自增序号的环形缓冲，支持按序号增量读取。"""

    def __init__(self, capacity):
        self._items = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self.total = 0

    def append(self, item):
        with self._lock:
            self._seq += 1
            self.total += 1
            entry = dict(item)
            entry['seq'] = self._seq
            self._items.append(entry)
            return entry

    def since(self, after=0, limit=500):
        with self._lock:
            items = [item for item in self._items if item['seq'] > after]
        return items[-limit:]

    def last_seq(self):
        with self._lock:
            return self._seq


class LogBusHandler(logging.Handler):
    """把所有 python logging 输出捕获进环形缓冲。"""

    def __init__(self, buffer_):
        super().__init__()
        self._buffer = buffer_

    def emit(self, record):
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self._buffer.append({
            'ts': record.created,
            'level': record.levelname,
            'source': record.name,
            'message': message,
        })


class LogBus:
    """运行日志 + 结构化事件（消息转发、自动回复、错误等）。"""

    def __init__(self):
        self.logs = RingBuffer(LOG_CAPACITY)
        self.events = RingBuffer(EVENT_CAPACITY)
        self.counters = {'message_in': 0, 'message_out': 0, 'autoreply': 0, 'error': 0}
        self._lock = threading.Lock()

    def event(self, kind, message='', **meta):
        with self._lock:
            if kind in self.counters:
                self.counters[kind] += 1
        entry = {'ts': time.time(), 'kind': kind, 'message': message}
        entry.update(meta)
        self.events.append(entry)

    def snapshot(self):
        return {
            'counters': dict(self.counters),
            'log_seq': self.logs.last_seq(),
            'event_seq': self.events.last_seq(),
        }


bus = LogBus()


def install_handler():
    """挂载到 root logger，之后所有 logging 输出都会进入日志总线。"""
    handler = LogBusHandler(bus.logs)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logging.getLogger().addHandler(handler)
