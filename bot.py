"""Crisp Telegram Bot —— 带 Web 控制台的入口。

用法：
    python3 bot.py                  # 控制台默认监听 127.0.0.1:8000
    python3 bot.py --host 0.0.0.0 --port 9000
    # 也可用环境变量 CONSOLE_HOST / CONSOLE_PORT 控制，CONSOLE_PASSWORD 用于
    # Docker 等场景首次启动时直接初始化控制台密码。
"""
import argparse
import logging
import os
import sys
import threading

if getattr(sys, 'frozen', False):
    # PyInstaller 打包后：config.yml 等可变文件放在可执行文件同目录
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

from core.config_manager import ConfigManager               # noqa: E402
from core.engine import BotEngine, EngineError              # noqa: E402
from core.logbus import bus, install_handler                # noqa: E402
from core.runtime import runtime                            # noqa: E402

log = logging.getLogger('main')


def parse_args():
    parser = argparse.ArgumentParser(description='Crisp Telegram Bot（Web 控制台版）')
    parser.add_argument('--host', default=None, help='控制台监听地址（默认 127.0.0.1）')
    parser.add_argument('--port', type=int, default=None, help='控制台监听端口（默认 8000）')
    return parser.parse_args()


def auto_start_engine(engine):
    """配置完整时后台自动启动机器人引擎，失败不阻断控制台。"""
    try:
        status = engine.start()
        if status['state'] == 'error':
            log.error('机器人引擎自动启动失败：%s', status.get('last_error'))
        elif status['state'] == 'running':
            log.info('机器人引擎自动启动完成')
        else:
            log.info('机器人引擎正在后台启动（当前状态：%s）', status['state'])
    except EngineError as err:
        log.error('机器人引擎自动启动失败：%s（可在控制台修改配置后重试）', err)


def main():
    args = parse_args()
    host = args.host or os.environ.get('CONSOLE_HOST') or '127.0.0.1'
    port = args.port or int(os.environ.get('CONSOLE_PORT') or 8000)

    install_handler()
    runtime.console_host = host
    runtime.console_port = port

    cm = ConfigManager(os.path.join(BASE_DIR, 'config.yml'))
    runtime.config_manager = cm

    env_password = os.environ.get('CONSOLE_PASSWORD')
    if env_password and not cm.has_password():
        cm.set_password(env_password)
        log.info('已通过环境变量 CONSOLE_PASSWORD 初始化控制台密码')

    engine = BotEngine(cm)
    runtime.engine = engine

    if cm.exists():
        if cm.is_complete():
            threading.Thread(
                target=auto_start_engine, args=(engine,),
                name='engine-autostart', daemon=True,
            ).start()
        else:
            log.warning('config.yml 配置不完整，机器人未启动，请在控制台完成配置')
    else:
        log.info('尚未创建 config.yml，请在控制台完成首次配置（将自动生成）')

    from core.webapp import create_app
    app = create_app()

    from werkzeug.serving import make_server
    server = make_server(host, port, app, threaded=True)
    log.info('Web 控制台已启动：http://%s:%s', host if host != '0.0.0.0' else '127.0.0.1', port)
    if host in ('0.0.0.0', '::'):
        log.warning('控制台正监听所有网卡（%s），请确保已设置控制台密码或配置防火墙！', host)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('收到退出信号，正在停止机器人引擎…')
        try:
            engine.stop()
        except Exception:
            pass


if __name__ == '__main__':
    main()
