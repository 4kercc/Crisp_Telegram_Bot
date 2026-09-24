"""日志总线：环形缓冲的运行日志与结构化消息事件，供 Web 控制台增量拉取。"""
import logging
import re
import threading
import time
from collections import deque

LOG_CAPACITY = 2000
EVENT_CAPACITY = 2000

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


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
    """把所有 python logging 输出捕获进环形缓冲。

    第三方库（PTB/httpx）的长轮询网络抖动会带出几十行异常堆栈，这里把它们
    压成一行告警并去抖，避免控制台运行日志被刷屏，掩盖真正的业务错误。
    """

    # 长轮询被打断属于可自愈的正常现象，命中这些特征时只记一行
    NOISE_LOGGERS = ('telegram.ext._updater', 'telegram.ext._application')
    NOISE_KEYWORDS = ('httpx httperror', 'networkerror', 'connection reset',
                      'readerror', 'timedout', 'brokenresourceerror', 'connecterror',
                      'serverdisconnectederror', 'connection aborted')
    NOISE_WINDOW = 30.0

    def __init__(self, buffer_):
        super().__init__()
        self._buffer = buffer_
        self._noise_seen = {}
        self._noise_lock = threading.Lock()

    @classmethod
    def _is_noise(cls, record):
        if record.name not in cls.NOISE_LOGGERS:
            return False
        parts = [record.getMessage() or '']
        # PTB 的 logger.exception 只写一句固定文案，真实原因在异常链里
        node = record.exc_info[1] if record.exc_info else None
        seen = set()
        while node is not None and id(node) not in seen:
            seen.add(id(node))
            parts.append(type(node).__name__)
            parts.append(str(node))
            node = node.__cause__ or node.__context__
        text = ' '.join(parts).lower()
        return any(keyword in text for keyword in cls.NOISE_KEYWORDS)

    def _noise_suppressed(self, record):
        """同一类抖动在窗口期内只保留一条，超出窗口返回 False 表示应记录。"""
        key = record.name
        now = time.time()
        with self._noise_lock:
            last = self._noise_seen.get(key, 0.0)
            self._noise_seen[key] = now
            return (now - last) < self.NOISE_WINDOW

    def emit(self, record):
        try:
            if self._is_noise(record):
                if self._noise_suppressed(record):
                    return
                message = 'Telegram 长轮询连接中断（网络抖动），SDK 将自动重试，无需干预'
                level = 'WARNING'
            else:
                message = self.format(record)
                level = record.levelname
        except Exception:
            message = record.getMessage()
            level = record.levelname
        self._buffer.append({
            'ts': record.created,
            'level': level,
            'source': record.name,
            'message': ANSI_RE.sub('', message),
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
