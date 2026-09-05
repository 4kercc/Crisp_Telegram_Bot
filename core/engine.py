"""机器人引擎：在独立线程的 asyncio loop 中驱动 python-telegram-bot，
支持从 Web 控制台随时启动/停止/重启（热应用新配置），控制台进程本身不退出。"""
import asyncio
import logging
import re
import threading
import time

from telegram.ext import Application, MessageHandler, filters

from core import session_map
from core.logbus import bus

log = logging.getLogger('engine')

SESSION_ID_RE = re.compile(r'session_\w{8}(-\w{4}){3}-\w{12}')

STOPPED = 'stopped'
STARTING = 'starting'
RUNNING = 'running'
STOPPING = 'stopping'
ERROR = 'error'


class EngineError(Exception):
    pass


class BotEngine:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self._lock = threading.RLock()
        self.state = STOPPED
        self.last_error = None
        self.started_at = None
        self.config = None
        self.crisp_client = None
        self.mode = None
        self.tg_username = None
        self.crisp_website_name = None
        self._app = None
        self._loop = None
        self._thread = None
        self._modules = []
        self._state_event = threading.Event()

    # ---------- 对外接口（Flask 线程调用） ----------

    def start(self, wait_timeout=30):
        with self._lock:
            if self.state in (STARTING, RUNNING, STOPPING):
                raise EngineError(f'引擎当前状态为 {self.state}，无法启动')
            config = self.config_manager.load()
            problems = self.config_manager.validate(config)
            if problems:
                message = '配置无效：' + '；'.join(problems)
                self._set_error(message)
                raise EngineError(message)

            self.state = STARTING
            self.last_error = None
            self._state_event.clear()

        crisp_client = self._build_crisp_client(config)
        with self._lock:
            self.config = config
            self.crisp_client = crisp_client
            self.mode = config['crisp']['msgapi']
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._thread_main, name='bot-engine', daemon=True)
            self._thread.start()

        # 等待初始化完成（Telegram initialize 需要联网），超时则交给状态轮询
        self._state_event.wait(wait_timeout)
        return self.status()

    def stop(self, timeout=25):
        with self._lock:
            if self.state == STOPPED:
                return self.status()
            if self.state == STOPPING:
                return self.status()
            self.state = STOPPING

        deadline = time.time() + 15
        while self._thread is not None and self._thread.is_alive():
            if self._loop is not None and self._loop.is_running():
                break
            if self.state == STARTING and time.time() < deadline:
                time.sleep(0.2)
                continue
            break

        try:
            if self._loop is not None and self._loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)
                try:
                    future.result(timeout=timeout)
                except Exception as err:
                    log.error('停止引擎时出错：%s', err)
        finally:
            self._cleanup()

        return self.status()

    def restart(self, wait_timeout=30):
        try:
            self.stop()
        except Exception as err:
            log.warning('重启时停止旧引擎出错：%s', err)
        return self.start(wait_timeout=wait_timeout)

    def status(self):
        with self._lock:
            return {
                'state': self.state,
                'mode': self.mode,
                'last_error': self.last_error,
                'started_at': self.started_at,
                'uptime': int(time.time() - self.started_at) if self.started_at and self.state == RUNNING else 0,
                'tg_username': self.tg_username,
                'crisp_website_name': self.crisp_website_name,
            }

    # ---------- 内部实现 ----------

    def _build_crisp_client(self, config):
        from crisp_api import Crisp
        from crisp_api.errors.route import RouteError

        crisp = config['crisp']
        try:
            client = Crisp()
            client.set_tier('plugin')
            client.authenticate(crisp['id'], crisp['key'])
            client.plugin.get_connect_account()
            site = client.website.get_website(crisp['website']) or {}
            self.crisp_website_name = site.get('name')
            return client
        except Exception as err:
            message = str(err)
            if err.__class__.__name__ == 'RouteError' and err.args and isinstance(err.args[0], dict):
                info = err.args[0]
                message = f"HTTP {info.get('code')}：{info.get('message')}"
            self._set_error(f'Crisp 连接失败：{message}')
            raise EngineError(f'Crisp 连接失败，请确认 Crisp 配置项是否正确（{message}）')

    def _thread_main(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_start())
        except Exception as err:
            log.exception('引擎启动失败')
            self._set_error(str(err))
            return
        with self._lock:
            self.state = RUNNING
            self.started_at = time.time()
        self._state_event.set()
        log.info('机器人引擎已启动（模式：%s）', self.mode)
        bus.event('system', f'机器人引擎已启动（模式：{self.mode}）')
        self._loop.run_forever()

    async def _async_start(self):
        config = self.config
        builder = Application.builder().token(config['bot']['token'])
        proxy = str(config['bot'].get('proxy') or '').strip()
        if proxy:
            builder = builder.proxy_url(proxy).get_updates_proxy_url(proxy)
            log.info('已启用代理：%s', proxy)
        app = builder.build()
        self._app = app
        app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, self._on_reply))

        import Modules
        self._modules = Modules.load(config)
        for name, module in self._modules:
            conf = module.Conf
            if conf.method == 'repeating':
                interval = conf.interval
                get_interval = getattr(module, 'get_interval', None)
                if get_interval:
                    interval = get_interval(config)
                app.job_queue.run_repeating(module.exec, interval=interval, name=name)
                log.info('已加载模块 %s（定时轮询，间隔 %ss）：%s', name, interval, conf.desc)
            elif conf.method == 'events':
                app.job_queue.run_once(module.exec, 5, name=name)
                log.info('已加载模块 %s（事件驱动）：%s', name, conf.desc)

        await app.initialize()
        try:
            self.tg_username = app.bot.username
        except Exception:
            self.tg_username = None
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

    async def _async_stop(self):
        for name, module in self._modules:
            stop = getattr(module, 'stop', None)
            if stop is None:
                continue
            try:
                result = stop()
                if asyncio.iscoroutine(result):
                    await result
                log.info('模块 %s 已停止', name)
            except Exception as err:
                log.warning('停止模块 %s 出错：%s', name, err)
        self._modules = []

        app = self._app
        if app is not None:
            try:
                if app.updater is not None and app.updater.running:
                    await app.updater.stop()
            except Exception as err:
                log.warning('停止 updater 出错：%s', err)
            try:
                if app.running:
                    await app.stop()
                await app.shutdown()
            except Exception as err:
                log.warning('关闭 Application 出错：%s', err)
        log.info('机器人引擎已停止')
        bus.event('system', '机器人引擎已停止')

    def _cleanup(self):
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                try:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                except RuntimeError:
                    pass
            if self._thread is not None:
                self._thread.join(timeout=10)
            self._loop = None
            self._thread = None
            self._app = None
            self.crisp_client = None
            self.mode = None
            self.tg_username = None
            self.started_at = None
            self.state = STOPPED

    def _set_error(self, message):
        with self._lock:
            self.state = ERROR
            self.last_error = message
            self.started_at = None
        self._state_event.set()

    # ---------- Telegram 回复 → Crisp ----------

    async def _on_reply(self, update, context):
        message = update.effective_message
        replied = message.reply_to_message if message else None
        if replied is None:
            return
        # 优先查推送时记录的 (chat_id, message_id) 映射，兼容旧版卡片里内嵌 Session 文本
        session_id = session_map.lookup(update.effective_chat.id, replied.message_id)
        if session_id is None:
            source_text = replied.text or replied.caption or ''
            match = SESSION_ID_RE.search(source_text)
            if match is None:
                log.warning('无法定位回复目标对应的会话（请回复机器人推送的消息）')
                bus.event('error', '无法定位回复目标对应的会话（请直接回复机器人推送的消息）')
                return
            session_id = match.group()
        config = self.config
        query = {
            'type': 'text',
            'content': message.text,
            'from': 'operator',
            'origin': 'chat',
        }
        try:
            self.crisp_client.website.send_message_in_conversation(
                config['crisp']['website'], session_id, query)
            log.info('已把 Telegram 回复转发到 Crisp 会话 %s', session_id)
            bus.event('message_out', message.text, session_id=session_id, status='ok')
        except Exception as err:
            log.exception('转发回复到 Crisp 失败')
            bus.event('error', f'转发回复到 Crisp 失败：{err}', session_id=session_id)
