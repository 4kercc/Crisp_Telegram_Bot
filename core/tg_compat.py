"""Telegram Bot API HTTP 请求与 Topics 兼容封装。

由于当前打包环境的 python-telegram-bot (v20.0a4) 早期版本尚未在 ExtBot.send_message / ExtBot.send_photo / create_forum_topic 中封装 message_thread_id，
本模块使用直接调用 Telegram Bot HTTP API (requests / aiohttp) 的方式实现完整的 Topics 话题管理与直发能力。
"""
import asyncio
import logging
import requests

log = logging.getLogger('tg_compat')


def _proxies(proxy):
    return {'http': proxy, 'https': proxy} if proxy else None


def tg_api_post(token, method, payload, proxy=''):
    """同步调用 Telegram Bot API。"""
    url = f'https://api.telegram.org/bot{token}/{method}'
    try:
        resp = requests.post(url, json=payload, timeout=15, proxies=_proxies(proxy))
        data = resp.json()
        if not data.get('ok'):
            log.warning('Telegram API %s 返回错误：%s (payload: %s)', method, data.get('description'), payload)
        return data
    except Exception as err:
        log.error('Telegram API %s 请求异常：%s', method, err)
        return {'ok': False, 'description': str(err)}


async def create_forum_topic(token, chat_id, name, icon_color=None, proxy=''):
    """在超级群中创建论坛话题（Forum Topic），返回 message_thread_id (int) 或 None。"""
    payload = {
        'chat_id': chat_id,
        'name': name[:100],
    }
    if icon_color:
        payload['icon_color'] = icon_color

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, tg_api_post, token, 'createForumTopic', payload, proxy)
    if data.get('ok'):
        result = data.get('result') or {}
        thread_id = result.get('message_thread_id')
        log.info('成功为群 %s 创建话题 %s (ID: %s)', chat_id, name, thread_id)
        return thread_id
    else:
        err_msg = data.get('description') or '未知错误'
        log.warning('为群 %s 创建话题 %s 失败：%s', chat_id, name, err_msg)
        raise RuntimeError(f'创建话题失败：{err_msg}')


async def send_message(token, chat_id, text, parse_mode='HTML', message_thread_id=None, proxy=''):
    """发送文本消息，支持 message_thread_id。返回 message_id 或 None。"""
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode,
    }
    if message_thread_id is not None:
        payload['message_thread_id'] = int(message_thread_id)

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, tg_api_post, token, 'sendMessage', payload, proxy)
    if data.get('ok'):
        return (data.get('result') or {}).get('message_id')

    # 若带 message_thread_id 发送失败（可能话题已关闭或群组不支持），降级为普通无话题发送
    if message_thread_id is not None:
        log.warning('带话题 ID %s 发送失败：%s，尝试普通直发', message_thread_id, data.get('description'))
        payload.pop('message_thread_id', None)
        fallback_data = await loop.run_in_executor(None, tg_api_post, token, 'sendMessage', payload, proxy)
        if fallback_data.get('ok'):
            return (fallback_data.get('result') or {}).get('message_id')
        raise RuntimeError(fallback_data.get('description') or '普通发送失败')

    raise RuntimeError(data.get('description') or '发送失败')


def _download_image(url, proxy=''):
    """把 Crisp 图片下载到本地（供 Telegram URL 拉取失败后改为文件上传）。"""
    try:
        resp = requests.get(url, timeout=20, proxies=_proxies(proxy))
        if resp.ok and resp.content:
            ctype = resp.headers.get('Content-Type') or 'image/jpeg'
            return resp.content, ctype.split(';')[0].strip()
    except Exception as err:
        log.warning('下载 Crisp 图片失败：%s', err)
    return None, None


def tg_api_post_multipart(token, method, fields, file_field, filename, content, ctype, proxy=''):
    """以 multipart/form-data 调用 Telegram Bot API（用于上传图片二进制）。"""
    url = f'https://api.telegram.org/bot{token}/{method}'
    try:
        resp = requests.post(url, data=fields,
                             files={file_field: (filename, content, ctype)},
                             timeout=60, proxies=_proxies(proxy))
        data = resp.json()
        if not data.get('ok'):
            log.warning('Telegram API %s 上传返回错误：%s', method, data.get('description'))
        return data
    except Exception as err:
        log.error('Telegram API %s 上传异常：%s', method, err)
        return {'ok': False, 'description': str(err)}


async def send_photo(token, chat_id, photo_url, caption='', parse_mode='HTML', message_thread_id=None, proxy=''):
    """发送图片消息，支持 message_thread_id。返回 message_id 或 None。

    优先让 Telegram 按 URL 拉取；若拉取失败（链接过期/无法访问），
    自动把图片下载到本地后改为 multipart 文件上传重试。
    """
    def _payload(with_thread=True):
        payload = {'chat_id': chat_id, 'photo': photo_url}
        if caption:
            payload['caption'] = caption
            payload['parse_mode'] = parse_mode
        if with_thread and message_thread_id is not None:
            payload['message_thread_id'] = int(message_thread_id)
        return payload

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, tg_api_post, token, 'sendPhoto', _payload(), proxy)
    if data.get('ok'):
        return (data.get('result') or {}).get('message_id')

    # 话题发送失败时先降级为不带话题直发
    if message_thread_id is not None:
        log.warning('带话题 ID %s 发送图片失败：%s，尝试普通直发', message_thread_id, data.get('description'))
        data = await loop.run_in_executor(None, tg_api_post, token, 'sendPhoto', _payload(False), proxy)
        if data.get('ok'):
            return (data.get('result') or {}).get('message_id')

    # Telegram 拉取不到图片 URL 时：下载后以文件上传重试
    if photo_url.startswith('http'):
        log.warning('Telegram 拉取图片 URL 失败（%s），尝试下载后直接上传：%s',
                    data.get('description'), photo_url)
        content, ctype = await loop.run_in_executor(None, _download_image, photo_url, proxy)
        if content:
            fields = {k: v for k, v in _payload(False).items() if k != 'photo'}
            data = await loop.run_in_executor(
                None, tg_api_post_multipart,
                token, 'sendPhoto', fields, 'photo', 'image', content, ctype, proxy)
            if data.get('ok'):
                return (data.get('result') or {}).get('message_id')

    raise RuntimeError(data.get('description') or '发送图片失败')
