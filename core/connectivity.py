"""连通性测试：Crisp REST 与 Telegram Bot API，返回结构化结果供控制台展示。"""
import logging
import time

import requests

log = logging.getLogger('connectivity')


def _proxies(proxy):
    return {'http': proxy, 'https': proxy} if proxy else None


def test_telegram(token, proxy=''):
    """getMe 验证 Bot Token，返回 {ok, latency_ms, detail|error}。"""
    start = time.time()
    try:
        resp = requests.get(
            f'https://api.telegram.org/bot{token}/getMe',
            timeout=10,
            proxies=_proxies(proxy),
        )
        data = resp.json()
        latency = int((time.time() - start) * 1000)
        if data.get('ok'):
            result = data.get('result') or {}
            name = result.get('username') or result.get('first_name') or '未知'
            return {'ok': True, 'latency_ms': latency, 'detail': f'@{name}（{result.get("first_name") or "Telegram Bot"}）'}
        return {'ok': False, 'latency_ms': latency, 'error': data.get('description') or f'HTTP {resp.status_code}'}
    except Exception as err:
        return {'ok': False, 'latency_ms': int((time.time() - start) * 1000), 'error': str(err)}


def test_crisp(cid, key, website):
    """验证 Crisp Plugin 凭据与 Website ID，返回 {ok, latency_ms, detail|error}。"""
    start = time.time()
    try:
        from crisp_api import Crisp
        from crisp_api.errors.route import RouteError

        client = Crisp()
        client.set_tier('plugin')
        client.authenticate(cid, key)
        account = client.plugin.get_connect_account() or {}
        site = client.website.get_website(website) or {}
        latency = int((time.time() - start) * 1000)
        account_name = account.get('nickname') or account.get('email') or '已连接'
        site_name = site.get('name') or website
        return {
            'ok': True,
            'latency_ms': latency,
            'detail': f'账号：{account_name} ｜ 网站：{site_name}',
        }
    except Exception as err:
        message = str(err)
        if err.__class__.__name__ == 'RouteError' and err.args and isinstance(err.args[0], dict):
            info = err.args[0]
            message = f"HTTP {info.get('code')}：{info.get('message')}"
            detail = (info.get('data') or {}).get('message')
            if detail:
                message += f'（{detail}）'
        return {'ok': False, 'latency_ms': int((time.time() - start) * 1000), 'error': message}
