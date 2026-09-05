"""进程级运行时容器：Modules 通过它取当前配置与 Crisp 客户端（替代旧的 import bot 耦合）。"""
import threading
import time


class Runtime:
    def __init__(self):
        self._lock = threading.RLock()
        self.config_manager = None
        self.engine = None
        self.token_pool = None
        self.version = '3.0'
        self.console_host = '127.0.0.1'
        self.console_port = 8000
        self.process_started_at = time.time()

    def get_config(self):
        """引擎运行中返回引擎启动时的配置快照，否则读当前配置文件。"""
        engine = self.engine
        if engine is not None and engine.config is not None:
            return engine.config
        if self.config_manager is not None:
            return self.config_manager.load()
        return None

    def get_crisp_client(self):
        engine = self.engine
        return engine.crisp_client if engine is not None else None

    def get_token_pool(self):
        return self.token_pool


runtime = Runtime()
