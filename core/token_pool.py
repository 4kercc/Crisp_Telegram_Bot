"""Crisp 插件令牌池：多枚开发令牌自动轮换，规避 400 次/24h 限额（预留缓冲）。

原理：Crisp 官方未提供配额查询接口，这里在 crisp_api 的 HTTP 层挂钩——
每次 REST 请求按当前令牌计数；用量逼近限额或收到 429 时，自动切换到
用量最少且未耗尽的令牌并重新 authenticate，对上层模块完全透明。
支持本地持久化记录用量与 24 小时过期重置，防止服务重启后计数归零。
"""
import json
import logging
import os
import threading
import time

from core.logbus import bus

log = logging.getLogger('token_pool')

DEFAULT_LIMIT = 400
DEFAULT_THRESHOLD = 20
EXHAUSTED_HOURS = 24


class TokenInfo:
    def __init__(self, identifier, key, used=0, exhausted_until=0.0, last_updated=0.0):
        self.identifier = identifier
        self.key = key
        self.used = int(used or 0)
        self.exhausted_until = float(exhausted_until or 0.0)
        self.last_updated = float(last_updated or time.time())
        self.last_error = None

    def check_daily_reset(self):
        """如果距离上次使用超过 24 小时，且未处于 429 冻结期，则自动重置本地用量。"""
        now = time.time()
        if now >= self.exhausted_until and (now - self.last_updated) >= EXHAUSTED_HOURS * 3600:
            if self.used > 0:
                log.info('令牌 %s… 距离上次使用已满 24 小时，自动重置用量 (原: %d)',
                         self.identifier[:8], self.used)
                self.used = 0
                self.exhausted_until = 0.0
                self.last_updated = now

    def exhausted(self):
        self.check_daily_reset()
        return time.time() < self.exhausted_until


class TokenPool:
    def __init__(self, tokens, limit=DEFAULT_LIMIT, threshold=DEFAULT_THRESHOLD,
                 rotation='round_robin', state_file=None):
        self._lock = threading.RLock()
        self.state_file = state_file
        self.limit = int(limit) or DEFAULT_LIMIT
        self.threshold = max(0, int(threshold) if threshold is not None else DEFAULT_THRESHOLD)
        self.rotation = str(rotation or 'round_robin').strip().lower()
        self._client = None
        self._last_no_candidate_warn = 0.0

        # 从持久化文件读取已保存的用量状态
        saved_state = self._load_state()

        self._tokens = []
        now = time.time()
        for i, k in tokens:
            st = saved_state.get(i) or {}
            used = st.get('used', 0)
            ex_until = st.get('exhausted_until', 0.0)
            last_up = st.get('last_updated', now)
            t_info = TokenInfo(i, k, used=used, exhausted_until=ex_until, last_updated=last_up)
            t_info.check_daily_reset()
            self._tokens.append(t_info)

        # 优先选择用量最少且未耗尽的令牌作为起始
        if self._tokens:
            avail = [t for t in self._tokens if not t.exhausted()]
            if avail:
                best = min(avail, key=lambda t: t.used)
                self._index = self._tokens.index(best)
            else:
                self._index = 0
        else:
            self._index = 0

    def _load_state(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as err:
            log.warning('读取令牌用量持久化文件失败：%s', err)
            return {}

    def _save_state(self):
        if not self.state_file:
            return
        try:
            state = {}
            for t in self._tokens:
                state[t.identifier] = {
                    'used': t.used,
                    'exhausted_until': t.exhausted_until,
                    'last_updated': t.last_updated,
                }
            tmp = self.state_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
            if os.path.exists(self.state_file):
                os.replace(tmp, self.state_file)
            else:
                os.rename(tmp, self.state_file)
        except Exception as err:
            log.warning('保存令牌用量持久化文件失败：%s', err)

    def bind_client(self, client):
        """令牌切换时自动对 Crisp 客户端重新 authenticate。"""
        self._client = client

    def current(self):
        with self._lock:
            return self._tokens[self._index]

    def _select_next_token(self):
        """根据轮换策略选出下一个目标令牌：
        - round_robin (轮询): 每次请求顺次轮转到下一个未耗尽令牌，多个令牌均匀分摊请求
        - failover (主备故障转移): 固定使用当前令牌，直到达到 380 次熔断或收到 429 后再切换
        """
        if len(self._tokens) <= 1:
            return

        now = time.time()
        # 排除已耗尽(380次或429)的令牌
        avail_indices = [idx for idx, t in enumerate(self._tokens) if not t.exhausted()]
        if not avail_indices:
            return

        if self.rotation == 'round_robin':
            # 找到下一个可用索引
            for step in range(1, len(self._tokens) + 1):
                next_idx = (self._index + step) % len(self._tokens)
                if next_idx in avail_indices:
                    if next_idx != self._index:
                        self._index = next_idx
                        if self._client is not None:
                            try:
                                t = self._tokens[self._index]
                                self._client.authenticate(t.identifier, t.key)
                            except Exception as err:
                                log.error('轮询切换令牌认证失败：%s', err)
                    break

    def on_request(self):
        """每次 REST 请求调用：计数并在达到阈值或轮询模式下切换。"""
        with self._lock:
            token = self._tokens[self._index]
            token.check_daily_reset()
            token.used += 1
            token.last_updated = time.time()

            # 达到或超过 380 次（limit - threshold）自动标记耗尽 24 小时
            if token.used >= (self.limit - self.threshold):
                if token.exhausted_until <= time.time():
                    token.exhausted_until = time.time() + EXHAUSTED_HOURS * 3600
                    log.warning('令牌 %s… 用量已达 %d/%d，触发安全保护禁用 24 小时',
                                token.identifier[:8], token.used, self.limit)

            self._save_state()

            # 当前令牌已达上限，强制熔断切换
            if token.used >= (self.limit - self.threshold):
                self._rotate(f'令牌 {token.identifier[:8]}… 用量已达 {token.used}/{self.limit}（已达安全阈值 {self.limit - self.threshold}）')
            elif self.rotation == 'round_robin':
                # 轮询模式：请求完成后顺延至下一个令牌，准备下一次请求
                self._select_next_token()

    def on_rate_limited(self):
        """收到 429：标记当前令牌 24 小时后恢复并切换。"""
        with self._lock:
            token = self._tokens[self._index]
            token.exhausted_until = time.time() + EXHAUSTED_HOURS * 3600
            token.last_updated = time.time()
            token.last_error = '触发 Crisp 限流(429)'
            self._save_state()
            self._rotate(f'令牌 {token.identifier[:8]}… 触发限流(429)，{EXHAUSTED_HOURS} 小时后恢复')

    def update_usage(self, used, limit=None, reset_seconds=None):
        """用服务端反馈（响应头等）校准本地计数。"""
        with self._lock:
            token = self._tokens[self._index]
            if limit:
                self.limit = int(limit)
            if used is not None:
                token.used = max(token.used, int(used))
            token.last_updated = time.time()
            if reset_seconds:
                token.exhausted_until = max(token.exhausted_until, time.time() + int(reset_seconds))
            if token.used >= self.limit - self.threshold:
                token.exhausted_until = max(token.exhausted_until,
                                             time.time() + EXHAUSTED_HOURS * 3600)
            self._save_state()
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
            for t in self._tokens:
                t.check_daily_reset()
            return {
                'limit': self.limit,
                'threshold': self.threshold,
                'rotation': self.rotation,
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
