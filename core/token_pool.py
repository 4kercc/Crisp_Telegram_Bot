"""Crisp 插件令牌池：多枚开发令牌自动调度，规避 500 次/24h 限额。

调度规则（可用「令牌池调度模式」切换轮询 / 主备）：
- 计数：每次 Crisp REST 请求按当前令牌累加当日用量
- 让位：单枚令牌当日用满 rotate_at（默认 300）次后，改用池中用量更少的令牌
- 封顶：达到当日硬上限（默认 400，官方 500 的 8 折）后冻结该令牌，直到次日 0 点
- 重置：每日 0 点（本地时间）自动清零用量并解除全部冻结
- 容错：429 按服务端提示短时冷却（默认 5 分钟）；令牌鉴权失败短退避后重试
- 持久化：用量与冻结状态落盘 .tokens_state.json，进程重启不丢失
"""
import json
import logging
import os
import threading
import time

from core.logbus import bus

log = logging.getLogger('token_pool')

DEFAULT_LIMIT = 400          # 单枚令牌每日硬上限（官方 500，留出缓冲）
DEFAULT_ROTATE_AT = 300      # 用满这么多次就换下一枚
RATE_LIMIT_COOLDOWN = 300    # 收到 429 且服务端未给 Retry-After 时的冷却秒数
ERROR_COOLDOWN = 900         # 令牌鉴权/接口异常时的退避秒数
AUTH_COOLDOWN = 1800         # 令牌被判定失效（401 invalid_session 等）后的隔离秒数
RESET_HOUR = 0               # 每日额度重置时刻（0 点）


def _day_key(ts=None):
    """时间戳所属的自然日（本地时区），用于判断是否跨天。"""
    return time.strftime('%Y-%m-%d', time.localtime(ts if ts else time.time()))


def _next_reset_ts(now=None):
    """下一次额度重置时刻（次日 00:00 本地时间）的时间戳。"""
    lt = time.localtime(now or time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, RESET_HOUR, 0, 0, 0, 0, -1))


class TokenInfo:
    def __init__(self, identifier, key, used=0, day=None, frozen_until=0.0,
                 freeze_reason=None, last_updated=0.0):
        self.identifier = identifier
        self.key = key
        self.used = int(used or 0)
        self.day = day or _day_key()
        self.frozen_until = float(frozen_until or 0.0)
        self.freeze_reason = freeze_reason
        self.last_updated = float(last_updated or time.time())
        self.last_error = None

    # 旧字段名兼容（早期版本用 exhausted_until 表示冻结截止时间）
    @property
    def exhausted_until(self):
        return self.frozen_until

    @exhausted_until.setter
    def exhausted_until(self, value):
        self.frozen_until = float(value or 0.0)

    def rollover_if_new_day(self):
        """跨过 0 点后清零当日用量并解除冻结，返回是否发生了重置。"""
        today = _day_key()
        if self.day == today:
            return False
        if self.used or self.frozen_until:
            log.info('令牌 %s… 已到每日重置时刻（0 点），用量归零（原 %d 次）',
                     self.identifier[:8], self.used)
        self.used = 0
        self.frozen_until = 0.0
        self.freeze_reason = None
        self.last_error = None
        self.day = today
        return True

    def check_daily_reset(self):
        """兼容旧调用名。"""
        return self.rollover_if_new_day()

    def is_frozen(self):
        return time.time() < self.frozen_until

    def exhausted(self):
        """兼容旧调用名。"""
        return self.is_frozen()

    def freeze(self, seconds, reason):
        """冻结（不缩短已有的更长期冻结）。"""
        self.frozen_until = max(self.frozen_until, time.time() + max(0.0, float(seconds)))
        self.freeze_reason = reason


class TokenPool:
    def __init__(self, tokens, limit=DEFAULT_LIMIT, rotate_at=None,
                 rotation='round_robin', state_file=None):
        self._lock = threading.RLock()
        self.state_file = state_file
        self.limit = int(limit) or DEFAULT_LIMIT
        self.rotate_at = max(0, int(rotate_at)) if rotate_at is not None \
            else min(DEFAULT_ROTATE_AT, self.limit)
        self.rotation = str(rotation or 'round_robin').strip().lower()
        self._client = None
        self._last_no_candidate_warn = 0.0

        saved_state = self._load_state()

        self._tokens = []
        for identifier, key in tokens:
            state = self._restore_state(saved_state.get(identifier))
            t_info = TokenInfo(identifier, key, **state)
            t_info.rollover_if_new_day()
            self._tokens.append(t_info)

        # 起始令牌：优先当日用量少、且未冻结的那一枚
        if self._tokens:
            preferred = self._preferred() or self._tokens
            best = min(preferred, key=lambda t: (t.is_frozen(), t.used))
            self._index = self._tokens.index(best)
        else:
            self._index = 0

    # ---------- 持久化 ----------

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

    @staticmethod
    def _restore_state(saved):
        """还原单个令牌的持久化状态，并兼容旧版字段与语义。"""
        st = saved or {}
        now = time.time()
        last_updated = float(st.get('last_updated') or now)
        day = st.get('day') or _day_key(last_updated)

        frozen_until = float(st.get('frozen_until') or 0.0)
        freeze_reason = st.get('freeze_reason')
        if not frozen_until:
            legacy = float(st.get('exhausted_until') or 0.0)
            if legacy > now:
                # 旧版动辄冻结 24 小时，升级后最多保留到下一次 0 点重置
                frozen_until = min(legacy, _next_reset_ts(now))
                freeze_reason = freeze_reason or 'quota'
        if frozen_until and frozen_until <= now:
            frozen_until = 0.0

        return {
            'used': st.get('used', 0),
            'day': day,
            'frozen_until': frozen_until,
            'freeze_reason': freeze_reason if frozen_until else None,
            'last_updated': last_updated,
        }

    def _save_state(self):
        if not self.state_file:
            return
        try:
            state = {}
            for t in self._tokens:
                state[t.identifier] = {
                    'used': t.used,
                    'day': t.day,
                    'frozen_until': t.frozen_until,
                    'freeze_reason': t.freeze_reason,
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

    # ---------- 选择与切换 ----------

    def bind_client(self, client):
        """令牌切换时自动对 Crisp 客户端重新 authenticate。"""
        self._client = client

    def current(self):
        with self._lock:
            return self._tokens[self._index]

    def _alive(self):
        return [t for t in self._tokens if not t.is_frozen()]

    def _preferred(self):
        """优先返回当日用量未达让位阈值的令牌；都已达标时退化为全部存活令牌。"""
        alive = self._alive()
        fresh = [t for t in alive if t.used < self.rotate_at]
        return fresh or alive

    def _switch(self, index, reason=None):
        old = self._tokens[self._index]
        self._index = index
        target = self._tokens[index]
        if self._client is not None:
            try:
                self._client.authenticate(target.identifier, target.key)
            except Exception as err:
                log.error('切换令牌后重新认证失败：%s', err)
        message = f'{reason} → ' if reason else ''
        message += f'已切换到令牌 {target.identifier[:8]}…（原 {old.identifier[:8]}…）'
        log.info(message)
        bus.event('system', message)
        return target

    def _warn_no_candidate(self, reason):
        now = time.time()
        if now - self._last_no_candidate_warn > 1800:
            self._last_no_candidate_warn = now
            log.warning('令牌池内没有其他可用令牌：%s', reason)
            bus.event('error', f'令牌池没有可用备用令牌：{reason}')

    def _select_next_token(self, reason=None):
        """轮询模式：切换到环形顺序里的下一个可用令牌。"""
        if len(self._tokens) <= 1:
            return
        candidates = self._preferred()
        if not candidates:
            self._warn_no_candidate(reason or '当前令牌已冻结，池中没有可用令牌')
            return
        for step in range(1, len(self._tokens) + 1):
            index = (self._index + step) % len(self._tokens)
            if self._tokens[index] in candidates:
                if index != self._index:
                    self._switch(index, reason)
                return

    def _rotate(self, reason):
        """主备 / 冻结场景：切换到池中当日用量最少的可用令牌。"""
        candidates = [t for t in self._preferred() if t is not self._tokens[self._index]]
        if not candidates:
            self._warn_no_candidate(reason)
            return
        target = min(candidates, key=lambda t: t.used)
        self._switch(self._tokens.index(target), reason)

    # ---------- 请求计数 ----------

    def on_request(self):
        """每次 REST 请求调用：累加当日用量，达到阈值就让位，达到硬上限则冻结到次日 0 点。"""
        with self._lock:
            for t in self._tokens:
                t.rollover_if_new_day()

            token = self._tokens[self._index]
            token.used += 1
            token.last_updated = time.time()

            if token.used >= self.limit:
                token.freeze(max(0.0, _next_reset_ts() - time.time()), 'quota')
                token.last_error = f'今日用量已达上限 {self.limit} 次'
                self._save_state()
                self._rotate(f'令牌 {token.identifier[:8]}… 今日用量 {token.used}/{self.limit} 已封顶，冻结到次日 0 点')
                return

            self._save_state()

            if self.rotation == 'round_robin':
                self._select_next_token()
            elif token.used >= self.rotate_at:
                self._rotate(f'令牌 {token.identifier[:8]}… 今日用量 {token.used} 次已达让位阈值 {self.rotate_at}')

    def on_rate_limited(self, retry_after=None):
        """收到 429：按服务端 Retry-After（缺省 5 分钟）冷却当前令牌并切换。"""
        with self._lock:
            token = self._tokens[self._index]
            cooldown = int(retry_after) if retry_after else RATE_LIMIT_COOLDOWN
            cooldown = max(30, min(cooldown, int(max(0.0, _next_reset_ts() - time.time()))))
            token.freeze(cooldown, 'rate_limit')
            token.last_updated = time.time()
            token.last_error = f'触发 Crisp 限流(429)，{cooldown} 秒后重试'
            self._save_state()
            self._rotate(f'令牌 {token.identifier[:8]}… 触发限流(429)，{cooldown} 秒后自动恢复')

    def on_token_error(self, reason, cooldown=ERROR_COOLDOWN):
        """令牌本身不可用（鉴权失败 / 接口异常）：短暂退避后重试，不影响当日计数语义。"""
        with self._lock:
            token = self._tokens[self._index]
            token.freeze(cooldown, 'error')
            token.last_updated = time.time()
            token.last_error = str(reason)
            self._save_state()
            self._rotate(f'令牌 {token.identifier[:8]}… {reason}，{max(1, cooldown // 60)} 分钟后重试')

    def on_auth_failed(self, reason, cooldown=AUTH_COOLDOWN):
        """令牌被 Crisp 判定无效（401 invalid_session / 403）：隔离一段时间并切换。

        典型场景：插件被卸载、令牌被重置，或 4 枚令牌里混入了失效令牌。
        轮询模式下如果不隔离，失效令牌会被反复轮到并持续抛错。
        """
        with self._lock:
            token = self._tokens[self._index]
            token.freeze(cooldown, 'auth')
            token.last_updated = time.time()
            token.last_error = f'鉴权失败：{reason}'
            self._save_state()
            self._rotate(
                f'令牌 {token.identifier[:8]}… 鉴权失败（{reason}），已隔离 {max(1, cooldown // 60)} 分钟')

    def unfreeze_token(self, identifier):
        """手动解除某个令牌的冻结状态。"""
        with self._lock:
            for t in self._tokens:
                if t.identifier == identifier or t.identifier.startswith(identifier):
                    t.frozen_until = 0.0
                    t.freeze_reason = None
                    t.last_error = None
                    self._save_state()
                    log.info('已手动解除令牌 %s… 的冻结状态', t.identifier[:8])
                    return True
            return False

    def reset_token_usage(self, identifier):
        """手动把某个令牌的当日用量清零并解除冻结。"""
        with self._lock:
            for t in self._tokens:
                if t.identifier == identifier or t.identifier.startswith(identifier):
                    t.used = 0
                    t.day = _day_key()
                    t.frozen_until = 0.0
                    t.freeze_reason = None
                    t.last_error = None
                    t.last_updated = time.time()
                    self._save_state()
                    log.info('已手动重置令牌 %s… 的用量计数', t.identifier[:8])
                    return True
            return False

    def update_usage(self, used, limit=None, reset_seconds=None):
        """用服务端反馈（响应头等）校准本地计数。"""
        with self._lock:
            token = self._tokens[self._index]
            if limit:
                self.limit = int(limit)
                self.rotate_at = min(self.rotate_at, self.limit)
            if used is not None:
                token.used = max(token.used, int(used))
            token.last_updated = time.time()
            if reset_seconds:
                token.freeze(min(int(reset_seconds), max(0.0, _next_reset_ts() - time.time())),
                             'rate_limit')
            if token.used >= self.limit:
                token.freeze(max(0.0, _next_reset_ts() - time.time()), 'quota')
            self._save_state()
            if token.used >= self.rotate_at:
                self._rotate(f'令牌 {token.identifier[:8]}… 服务端计数 {token.used}/{self.limit}')

    def snapshot(self):
        with self._lock:
            # 注意用列表推导：any() 会短路，导致后面的令牌漏做跨天重置
            rolled = [t.rollover_if_new_day() for t in self._tokens]
            if any(rolled):
                # 跨天重置每天只发生一次，顺手落盘；不要在此处频繁写文件
                self._save_state()
            now = time.time()
            reset_at = _next_reset_ts(now)
            return {
                'limit': self.limit,
                'rotate_at': self.rotate_at,
                'rotation': self.rotation,
                'current': self._index,
                'reset_at': reset_at,
                'reset_in': max(0, int(reset_at - now)),
                'tokens': [{
                    'identifier': t.identifier,
                    'used': min(t.used, self.limit),
                    'remaining': max(0, self.limit - t.used),
                    'frozen': t.is_frozen(),
                    'freeze_reason': t.freeze_reason if t.is_frozen() else None,
                    'frozen_seconds': max(0, int(t.frozen_until - now)),
                    'rotated': t.used >= self.rotate_at,
                    'last_error': t.last_error,
                    'exhausted': t.is_frozen(),  # 兼容旧字段名
                } for t in self._tokens],
            }


_hook_installed_by = None
_original_request = None


def install_request_hook(pool):
    """把用量统计、限流与鉴权失败处理挂到 crisp_api 的 HTTP 层（幂等，随引擎重启更新池）。

    必须始终包住「最初的」crisp_api.request：引擎每次重启都会重新安装，
    若直接包住上一次的包装函数，层层叠加会导致重复计数、并让旧令牌池
    覆盖新令牌池的认证信息。
    """
    global _hook_installed_by, _original_request
    if _hook_installed_by is pool:
        return
    import crisp_api
    from requests.auth import HTTPBasicAuth

    if _original_request is None:
        _original_request = getattr(crisp_api, '_crisp_tgbot_original_request', None) \
            or crisp_api.request
        # 记录真正的原始实现，便于后续重启时重新包裹
        crisp_api._crisp_tgbot_original_request = _original_request
    original_request = _original_request

    def request(*args, **kwargs):
        pool.on_request()
        token = pool.current()
        kwargs['auth'] = HTTPBasicAuth(token.identifier, token.key)
        resp = original_request(*args, **kwargs)

        if resp.status_code == 429:
            retry_after = resp.headers.get('Retry-After') or resp.headers.get('x-ratelimit-reset')
            pool.on_rate_limited(retry_after=retry_after)
            resp = _retry_with_current(pool, original_request, args, kwargs, token, resp)

        elif resp.status_code in (401, 403):
            # 令牌失效（如 invalid_session）：隔离该令牌并换下一枚重试一次，
            # 否则轮询模式会反复轮到失效令牌并持续抛错
            pool.on_auth_failed(_short_error(resp))
            resp = _retry_with_current(pool, original_request, args, kwargs, token, resp)

        _absorb_rate_limit_headers(pool, resp)
        return resp

    crisp_api.request = request
    _hook_installed_by = pool
    log.info('已安装 Crisp 请求计数与限流轮换钩子')


def _retry_with_current(pool, original_request, args, kwargs, failed_token, fallback_resp):
    """用池中已切换的新令牌重发一次请求；没换到别的令牌就直接返回原响应。"""
    from requests.auth import HTTPBasicAuth

    token = pool.current()
    if token.identifier == failed_token.identifier:
        return fallback_resp

    retry_kwargs = dict(kwargs)
    retry_kwargs['auth'] = HTTPBasicAuth(token.identifier, token.key)
    return original_request(*args, **retry_kwargs)


def _short_error(resp):
    """从 Crisp 错误响应里提取一句人话，用于日志与界面展示。"""
    try:
        payload = resp.json() or {}
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        detail = payload.get('data') or {}
        message = detail.get('message') if isinstance(detail, dict) else None
        message = message or payload.get('message') or payload.get('reason')
        if message:
            return str(message)
    text = (resp.text or '').strip().replace('\n', ' ')
    return text[:120] or f'HTTP {resp.status_code}'


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
