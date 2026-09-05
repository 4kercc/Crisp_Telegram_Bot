"""Flask Web 控制台应用工厂。"""
import logging
import os
import sys
from datetime import timedelta

from flask import Flask, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException

from core.runtime import runtime
from core.webapp.api import bp

log = logging.getLogger('webapp')


def _static_folder():
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包时静态资源解包到 _MEIPASS/core/webapp/static
        return os.path.join(sys._MEIPASS, 'core', 'webapp', 'static')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')


def create_app():
    cm = runtime.config_manager
    app = Flask(__name__, static_folder=_static_folder(), static_url_path='/static')
    app.config.update(
        SECRET_KEY=cm.get_secret_key(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    app.json.ensure_ascii = False
    app.register_blueprint(bp)

    @app.get('/')
    def index():
        return send_from_directory(app.static_folder, 'index.html')

    @app.errorhandler(Exception)
    def on_error(err):
        if isinstance(err, HTTPException):
            return err
        log.exception('控制台内部错误')
        return jsonify(ok=False, error=f'服务器内部错误：{err}'), 500

    return app
