"""Crisp 插件令牌池：多枚开发令牌自动轮换，规避 500 次/24h 限额。

原理：Crisp 官方未提供配额查询接口，这里在 crisp_api 的 HTTP 层挂钩——
每次 REST 请求按当前令牌计数；用量逼近限额或收到 429 时，自动切换到
用量最少且未耗尽的令牌并重新 authenticate，对上层模块完全透明。
"""
import logging
import threading
import time

from core.logbus import bus

log = logging.getLogger('token_pool')

DEFAULT_LIMIT = 500
DEFAULT_THRESHOLD = 20
EXHAUSTED_HOURS = 24


class TokenInfo:
    def __init__(self, identifier, key):
        self.identifier = identifier
        self.key = key
        self.used = 0
        self.exhausted_until = 0.0
        self.last_error = None

    def exhausted(self):
        return time.time() < self.exhausted_until


class TokenPool:
    def __init__(self, tokens, limit=DEFAULT_LIMIT, threshold=DEFAULT_THRESHOLD):
        self._lock = threading.RLock()
        self._tokens = [TokenInfo(i, k) for i, k in tokens]
        self._index = 0
        self.limit = int(limit) or DEFAULT_LIMIT
        self.threshold = max(0, int(threshold) if threshold is not None else DEFAULT_THRESHOLD)
        self._client = None
        self._last_no_candidate_warn = 0.0

    def bind_client(self, client):
        """令牌切换时自动对 Crisp 客户端重新 authenticate。"""
        self._client = client

    def current(self):
        with self._lock:
            return self._tokens[self._index]

    def on_request(self):
        """每次 REST 请求调用：计数并在逼近限额时提前切换。"""
        with self._lock:
            token = self._tokens[self._index]
            token.used += 1
            if token.used >= self.limit:
                token.exhausted_until = max(token.exhausted_until,
                                             time.time() + EXHAUSTED_HOURS * 3600)
            if token.used >= self.limit - self.threshold:
                self._rotate(f'令牌 {token.identifier[:8]}… 用量已达 {token.used}/{self.limit}（切换阈值 {self.threshold}）')

    def on_rate_limited(self):
        """收到 429：标记当前令牌 24 小时后恢复并切换。"""
        with self._lock:
            token = self._tokens[self._index]
            token.exhausted_until = time.time() + EXHAUSTED_HOURS * 3600
            token.last_error = '触发 Crisp 限流(429)'
            self._rotate(f'令牌 {token.identifier[:8]}… 触发限流(429)，{EXHAUSTED_HOURS} 小时后恢复')

    def update_usage(self, used, limit=None, reset_seconds=None):
        """用服务端反馈（响应头等）校准本地计数。"""
        with self._lock:
            token = self._tokens[self._index]
            if limit:
                self.limit = int(limit)
            if used is not None:
                token.used = max(token.used, int(used))
            if reset_seconds:
                token.exhausted_until = max(token.exhausted_until, time.time() + int(reset_seconds))
            if token.used >= self.limit:
                token.exhausted_until = max(token.exhausted_until,
                                             time.time() + EXHAUSTED_HOURS * 3600)
            if token.used >= self.limit - self.threshold:
                self._rotate(f'令牌 {token.identifier[:8]}… 服务端用量 {token.used}/{self.limit}')

    def _rotate(self, reason):
        now = time.time()
        candidates = [t for t in self._tokens
                      if t is not self._tokens[self._index] and not t.exhausted()]
        if not candidates:
            # 告警去抖：半小时最多提示一次，避免每次请求刷日志
            if now - self._last_no_candidate_warn > 1800:
                self._last_no_candidate_warn = now
                log.warning('令牌池内没有其他可用令牌：%s', reason)
                bus.event('error', f'令牌池没有可用备用令牌：{reason}')
            return
        target = min(candidates, key=lambda t: t.used)
        old = self._tokens[self._index]
        self._index = self._tokens.index(target)
        if self._client is not None:
            try:
                self._client.authenticate(target.identifier, target.key)
            except Exception as err:
                log.error('切换令牌后重新认证失败：%s', err)
        message = f'{reason} → 已切换到令牌 {target.identifier[:8]}…'
        log.info(message)
        bus.event('system', message)

    def snapshot(self):
        with self._lock:
            now = time.time()
            return {
                'limit': self.limit,
                'threshold': self.threshold,
                'current': self._index,
                'tokens': [{
                    'identifier': t.identifier,
                    'used': min(t.used, self.limit),
                    'remaining': max(0, self.limit - t.used),
                    'exhausted': t.exhausted(),
                    'last_error': t.last_error,
                } for t in self._tokens],
            }


_hook_installed_by = None


def install_request_hook(pool):
    """把用量统计与限流轮换挂到 crisp_api 的 HTTP 层（幂等，随引擎重启更新池）。"""
    global _hook_installed_by
    if _hook_installed_by is pool:
        return
    import crisp_api
    from requests.auth import HTTPBasicAuth

    original_request = crisp_api.request

    def request(*args, **kwargs):
        pool.on_request()
        token = pool.current()
        kwargs['auth'] = HTTPBasicAuth(token.identifier, token.key)
        resp = original_request(*args, **kwargs)
        if resp.status_code == 429:
            # 先标记当前令牌耗尽并切换，再对新令牌重试一次
            pool.on_rate_limited()
            token = pool.current()
            kwargs['auth'] = HTTPBasicAuth(token.identifier, token.key)
            resp = original_request(*args, **kwargs)
        _absorb_rate_limit_headers(pool, resp)
        return resp

    crisp_api.request = request
    _hook_installed_by = pool
    log.info('已安装 Crisp 请求计数与限流轮换钩子')


def _absorb_rate_limit_headers(pool, resp):
    """如果 Crisp 响应带限流头，用服务端数值校准本地计数。"""
    try:
        headers = resp.headers or {}
        remaining = limit = reset = None
        for name, value in headers.items():
            lname = name.lower()
            if 'x-ratelimit-remaining' in lname:
                remaining = int(value)
            elif 'x-ratelimit-limit' in lname:
                limit = int(value)
            elif 'x-ratelimit-reset' in lname:
                reset = int(value)
        if remaining is not None:
            pool.update_usage(used=pool.limit - remaining if limit is None else limit - remaining,
                              limit=limit, reset_seconds=reset)
    except Exception:
        pass
