"""config.yml 读写、校验与默认值合并；控制台密码哈希与 session 密钥管理。"""
import os
import secrets
import threading

import yaml
from werkzeug.security import check_password_hash, generate_password_hash

DEFAULT_AUTOREPLY = {'在吗|你好': '欢迎使用客服系统，请等待客服回复你~'}

# 这些字段在 API 返回时脱敏；提交时留空表示保持原值
SECRET_FIELDS = (('bot', 'token'), ('crisp', 'key'))


class ConfigError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__('；'.join(self.problems))


def default_config():
    return {
        'bot': {'token': '', 'admin_id': [], 'proxy': ''},
        'crisp': {'id': '', 'key': '', 'website': '', 'msgapi': 'rtm', 'poll_interval': 60},
        'autoreply': dict(DEFAULT_AUTOREPLY),
        'console': {},
    }


def _merge_defaults(data):
    merged = default_config()
    for section in ('bot', 'crisp', 'autoreply', 'console'):
        value = data.get(section)
        if isinstance(value, dict):
            merged[section].update(value)
        elif value is not None:
            merged[section] = value
    return merged


def mask_value(value):
    text = str(value or '')
    if not text:
        return ''
    if len(text) <= 8:
        return '••••••'
    return text[:4] + '••••' + text[-4:]


class ConfigManager:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()

    def exists(self):
        return os.path.exists(self.path)

    # ---------- 读写 ----------

    def _read_raw(self):
        if not self.exists():
            return {}
        with open(self.path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}

    def _write_raw(self, data):
        with open(self.path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)

    def load(self):
        """读取并合并默认值，返回完整配置（含 console 段）。"""
        with self._lock:
            return _merge_defaults(self._read_raw())

    def save(self, data):
        """校验并保存 bot/crisp/autoreply 段，原样保留其余段（console 等）。"""
        problems = self.validate(data)
        if problems:
            raise ConfigError(problems)
        with self._lock:
            raw = self._read_raw()
            raw['bot'] = data.get('bot') or {}
            raw['crisp'] = data.get('crisp') or {}
            raw['autoreply'] = data.get('autoreply') or {}
            self._write_raw(raw)

    @staticmethod
    def validate(data):
        problems = []
        bot = data.get('bot') or {}

        token = str(bot.get('token') or '').strip()
        if not token:
            problems.append('Telegram Bot Token 不能为空')
        elif ':' not in token:
            problems.append('Telegram Bot Token 格式不正确（应为 数字:密串）')

        admin = bot.get('admin_id')
        if not isinstance(admin, list) or len(admin) == 0:
            problems.append('admin_id（推送目标会话）不能为空')
        else:
            for item in admin:
                try:
                    int(str(item).strip())
                except (TypeError, ValueError):
                    problems.append(f'admin_id 中存在无效值：{item}')
                    break

        crisp = data.get('crisp') or {}
        for key, label in (('id', 'Crisp Plugin ID'), ('key', 'Crisp Plugin Key'),
                           ('website', 'Crisp Website ID')):
            if not str(crisp.get(key) or '').strip():
                problems.append(f'{label} 不能为空')
        if crisp.get('msgapi') not in ('rtm', 'rest'):
            problems.append('消息模式（msgapi）只能是 rtm 或 rest')
        try:
            if int(crisp.get('poll_interval') or 60) < 5:
                problems.append('REST 轮询间隔不能小于 5 秒')
        except (TypeError, ValueError):
            problems.append('REST 轮询间隔必须是数字')

        autoreply = data.get('autoreply')
        if autoreply is not None and not isinstance(autoreply, dict):
            problems.append('自动回复必须为「关键词 → 回复内容」的键值对')
        return problems

    def is_complete(self):
        return not self.validate(self.load())

    # ---------- 控制台认证 ----------

    def get_password_hash(self):
        return (self.load().get('console') or {}).get('password_hash')

    def has_password(self):
        return bool(self.get_password_hash())

    def set_password(self, plaintext):
        with self._lock:
            data = self._read_raw()
            console = data.get('console') or {}
            console['password_hash'] = generate_password_hash(plaintext)
            data['console'] = console
            self._write_raw(data)

    def verify_password(self, plaintext):
        password_hash = self.get_password_hash()
        return bool(password_hash) and check_password_hash(password_hash, plaintext)

    def get_secret_key(self):
        """Flask session 密钥，首次调用时自动生成并持久化。"""
        with self._lock:
            data = self._read_raw()
            console = data.get('console') or {}
            key = console.get('secret_key')
            if not key:
                key = secrets.token_hex(32)
                console['secret_key'] = key
                data['console'] = console
                self._write_raw(data)
            return key
