"""RTM 模式：通过 Crisp WebSocket（socketio）实时接收新消息并推送到 Telegram。"""
import asyncio
import base64
import json
import logging

import requests
import socketio

from core.logbus import bus
from core.runtime import runtime
from core.templates import build_push_text, match_autoreply

log = logging.getLogger('mod.rtm')


class Conf:
    desc = '推送未读新消息'
    method = 'events'


def enabled(config):
    return config['crisp']['msgapi'] == 'rtm'


class CrispRtmBridge:
    """每次引擎启动新建一个实例，持有 socketio 客户端与会话元数据缓存。"""

    def __init__(self, config, client, context):
        self.config = config
        self.client = client
        self.context = context
        self.website_id = config['crisp']['website']
        self.conversationMetasDict = {}  # {session_id: metas}
        self._stopped = False
        self._new_sio()

    def _new_sio(self):
        """每次（重）连接都新建 socketio 客户端并注册事件。"""
        self.sio = socketio.AsyncClient(reconnection_attempts=5)
        self._register_handlers()

    # ---------- socketio 事件 ----------

    def _register_handlers(self):
        self.sio.on('connect', self._on_connect)
        self.sio.on('unauthorized', self._on_unauthorized)
        self.sio.on('connect_error', self._on_connect_error)
        self.sio.on('disconnect', self._on_disconnect)
        self.sio.on('message:send', self._on_message_send)
        self.sio.on('session:set_data', self._on_session_set_data)

    async def _on_connect(self):
        log.info('Crisp RTM 已连接')
        bus.event('system', 'Crisp RTM 已连接')
        await self.sio.emit('authentication', {
            'tier': 'plugin',
            'username': self.config['crisp']['id'],
            'password': self.config['crisp']['key'],
            'events': ['message:send', 'session:set_data'],
        })

    async def _on_unauthorized(self, data):
        log.error('Crisp RTM 认证被拒绝：%s', data)
        bus.event('error', f'Crisp RTM 认证被拒绝：{data}')

    async def _on_connect_error(self):
        log.error('Crisp RTM 连接失败')

    async def _on_disconnect(self):
        log.warning('已与 Crisp RTM 服务器断开')

    async def _on_session_set_data(self, data):
        session_id = data.get('session_id')
        cached = self.conversationMetasDict.get(session_id)
        if cached is None:
            return
        cached.setdefault('data', {}).update(data.get('data') or {})

    async def _on_message_send(self, data):
        session_id = data.get('session_id')
        try:
            if session_id not in self.conversationMetasDict:
                self.storeCrispConversationMetas(session_id)
            message_type = data.get('type')
            if message_type == 'text':
                await self.sendTextMessage(data)
            elif message_type == 'file' and 'image' in str((data.get('content') or {}).get('type', '')):
                await self.sendImageMessage(data)
            else:
                log.info('忽略未处理的消息类型：%s（会话 %s）', message_type, session_id)
        except Exception as err:
            log.exception('处理 Crisp 消息失败（会话 %s）', session_id)
            bus.event('error', f'处理 Crisp 消息失败：{err}', session_id=session_id)

    # ---------- 业务 ----------

    def storeCrispConversationMetas(self, session_id):
        self.conversationMetasDict[session_id] = self.client.website.get_conversation_metas(
            self.website_id, session_id)

    def getCrispConnectEndpoints(self):
        url = 'https://api.crisp.chat/v1/plugin/connect/endpoints'
        authtier = base64.b64encode(
            (self.config['crisp']['id'] + ':' + self.config['crisp']['key']).encode('utf-8')
        ).decode('utf-8')
        headers = {'X-Crisp-Tier': 'plugin', 'Authorization': 'Basic ' + authtier}
        response = requests.request('GET', url, headers=headers, data='')
        endPoint = json.loads(response.text).get('data').get('socket').get('app')
        return endPoint

    async def sendTextMessage(self, message):
        session_id = message['session_id']
        metas = self.conversationMetasDict.get(session_id) or {}

        matched, autoreply = match_autoreply(self.config.get('autoreply'), message['content'])
        text = build_push_text(metas, message['content'], session_id,
                               autoreply=autoreply if matched else '',
                               timestamp=message.get('timestamp'))
        if matched:
            self.client.website.send_message_in_conversation(self.website_id, session_id, {
                'type': 'text',
                'content': autoreply,
                'from': 'operator',
                'origin': 'chat',
            })
            bus.event('autoreply', autoreply, session_id=session_id)
            log.info('会话 %s 命中自动回复', session_id)

        for admin_id in self.config['bot']['admin_id']:
            await self.context.bot.send_message(chat_id=admin_id, text=text, parse_mode='HTML')
        self.mark_messages_read(message)
        log.info('已推送文本消息到 Telegram（会话 %s）', session_id)
        bus.event('message_in', message['content'], session_id=session_id,
                  msg_type='text', status='ok')

    async def sendImageMessage(self, message):
        session_id = message['session_id']
        text = build_push_text({}, '', session_id, image_only=True, timestamp=message.get('timestamp'))
        for admin_id in self.config['bot']['admin_id']:
            await self.context.bot.send_photo(
                chat_id=admin_id,
                photo=message['content']['url'],
                caption=text,
                parse_mode='HTML',
            )
        self.mark_messages_read(message)
        log.info('已推送图片消息到 Telegram（会话 %s）', session_id)
        bus.event('message_in', '[图片]', session_id=session_id, msg_type='image', status='ok')

    def mark_messages_read(self, message):
        self.client.website.mark_messages_read_in_conversation(
            self.website_id,
            message['session_id'],
            {'from': 'user', 'origin': 'chat', 'fingerprints': [message['fingerprint']]},
        )

    # Send all unread message.
    async def sendAllUnread(self):
        conversations = self.client.website.search_conversations(
            self.website_id, 1, filter_unread='1')
        if len(conversations) == 0:
            return
        for conversation in conversations:
            session_id = conversation['session_id']
            messages = self.client.website.get_messages_in_conversation(
                self.website_id, session_id, {})
            if session_id not in self.conversationMetasDict:
                self.storeCrispConversationMetas(session_id)
            for message in messages:
                if len(message['read']) == 0:
                    if message['type'] == 'text':
                        await self.sendTextMessage(message)
                    elif message['type'] == 'file' and 'image' in str(message['content']['type']):
                        await self.sendImageMessage(message)
                    else:
                        log.info('忽略未处理的消息类型：%s（会话 %s）', message['type'], session_id)

    # Connecting to Crisp RTM(WSS) Server，断开后自动重连
    async def start(self):
        while not self._stopped:
            try:
                await self.sio.connect(
                    self.getCrispConnectEndpoints(),
                    transports='websocket',
                    wait_timeout=10,
                )
                await self.sio.wait()
                if self._stopped:
                    break
                log.warning('与 Crisp RTM 服务器断开，30 秒后重连')
                bus.event('system', '与 Crisp RTM 服务器断开，30 秒后重连')
            except asyncio.CancelledError:
                return
            except Exception as err:
                log.error('Crisp RTM 连接失败：%s，30 秒后重试', err)
                bus.event('error', f'Crisp RTM 连接失败：{err}，30 秒后重试')
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return
            if not self._stopped:
                self._new_sio()

    async def stop(self):
        self._stopped = True
        try:
            await self.sio.disconnect()
        except Exception as err:
            log.warning('断开 Crisp RTM 连接时出错：%s', err)


_bridge = None


async def exec(context):
    """事件驱动入口：先排空历史未读，再挂到 RTM 长连接上。"""
    global _bridge
    config = runtime.get_config()
    client = runtime.get_crisp_client()
    if config is None or client is None:
        log.error('运行时未就绪，RTM 模块无法启动')
        return
    _bridge = CrispRtmBridge(config, client, context)
    await _bridge.sendAllUnread()
    await _bridge.start()


async def stop(context=None):
    global _bridge
    if _bridge is not None:
        await _bridge.stop()
        _bridge = None
