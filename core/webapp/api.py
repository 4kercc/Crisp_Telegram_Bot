"""控制台 REST API。"""
import logging

from flask import Blueprint, jsonify, request, session

from core import connectivity
from core.config_manager import ConfigError, mask_value
from core.engine import EngineError
from core.logbus import bus
from core.runtime import runtime
from core.webapp.auth import (auth_state, locked_seconds, login_required,
                              register_failure, register_success)

bp = Blueprint('api', __name__)
log = logging.getLogger('webapp.api')

MASK_MARK = '••••'


def _engine():
    return runtime.engine


# ---------- 认证 ----------

@bp.get('/api/auth/state')
def auth_state_route():
    return jsonify(ok=True, **auth_state())


@bp.post('/api/auth/setup')
def auth_setup():
    cm = runtime.config_manager
    if cm.has_password():
        return jsonify(ok=False, error='控制台密码已设置，请直接登录'), 403
    body = request.get_json(silent=True) or {}
    password = str(body.get('password') or '')
    if len(password) < 6:
        return jsonify(ok=False, error='密码长度至少 6 位'), 400
    cm.set_password(password)
    session['authed'] = True
    session.permanent = True
    register_success()
    log.info('控制台密码已初始化')
    return jsonify(ok=True, **auth_state())


@bp.post('/api/auth/login')
def auth_login():
    cm = runtime.config_manager
    if not cm.has_password():
        return jsonify(ok=False, error='尚未设置控制台密码', code='setup_required'), 401
    remaining = locked_seconds()
    if remaining > 0:
        return jsonify(ok=False, error=f'失败次数过多，请 {remaining} 秒后再试'), 429
    body = request.get_json(silent=True) or {}
    password = str(body.get('password') or '')
    if cm.verify_password(password):
        session['authed'] = True
        session.permanent = True
        register_success()
        log.info('控制台登录成功')
        return jsonify(ok=True, **auth_state())
    register_failure()
    log.warning('控制台登录失败')
    return jsonify(ok=False, error='密码错误'), 401


@bp.post('/api/auth/logout')
def auth_logout():
    session.clear()
    return jsonify(ok=True)


# ---------- 状态 ----------

@bp.get('/api/status')
def status():
    cm = runtime.config_manager
    state = auth_state()
    if state['setup_required'] or not state['authed']:
        return jsonify(ok=True, **state)
    engine = _engine()
    snap = bus.snapshot()
    payload = dict(state)
    payload.update({
        'version': runtime.version,
        'console_host': runtime.console_host,
        'console_port': runtime.console_port,
        'engine': engine.status() if engine is not None else None,
        'counters': snap['counters'],
        'config_ready': cm.is_complete(),
    })
    return jsonify(ok=True, **payload)


# ---------- 配置 ----------

def _keep_secret(new_value, old_value):
    """脱敏字段：留空或仍是掩码样式时保持原值。"""
    new_value = str(new_value or '').strip()
    if not new_value or MASK_MARK in new_value:
        return str(old_value or '')
    return new_value


@bp.get('/api/config')
@login_required
def get_config():
    config = runtime.config_manager.load()
    return jsonify(ok=True, config={
        'bot': {
            'token': mask_value(config['bot'].get('token')),
            'admin_id': config['bot'].get('admin_id') or [],
            'proxy': config['bot'].get('proxy') or '',
            'token_set': bool(config['bot'].get('token')),
        },
        'crisp': {
            'id': config['crisp'].get('id') or '',
            'key': mask_value(config['crisp'].get('key')),
            'website': config['crisp'].get('website') or '',
            'msgapi': config['crisp'].get('msgapi') or 'rtm',
            'poll_interval': config['crisp'].get('poll_interval') or 60,
            'key_set': bool(config['crisp'].get('key')),
        },
        'autoreply': config.get('autoreply') or {},
    })


@bp.post('/api/config')
@login_required
def save_config():
    cm = runtime.config_manager
    body = request.get_json(silent=True) or {}
    current = cm.load()

    bot_in = body.get('bot') or {}
    admin_in = bot_in.get('admin_id')
    if isinstance(admin_in, str):
        admin_in = admin_in.replace('，', ',').replace('\n', ',').split(',')
    if not isinstance(admin_in, list):
        admin_in = []
    admin_list = []
    for item in admin_in:
        text = str(item).strip()
        if text:
            try:
                admin_list.append(int(text))
            except ValueError:
                admin_list.append(text)

    bot = {
        'token': _keep_secret(bot_in.get('token'), current['bot'].get('token')),
        'admin_id': admin_list,
        'proxy': str(bot_in.get('proxy') or '').strip(),
    }

    crisp_in = body.get('crisp') or {}
    crisp = {
        'id': str(crisp_in.get('id') or '').strip(),
        'key': _keep_secret(crisp_in.get('key'), current['crisp'].get('key')),
        'website': str(crisp_in.get('website') or '').strip(),
        'msgapi': crisp_in.get('msgapi') if crisp_in.get('msgapi') in ('rtm', 'rest')
                  else current['crisp'].get('msgapi') or 'rtm',
        'poll_interval': crisp_in.get('poll_interval') or current['crisp'].get('poll_interval') or 60,
    }

    autoreply_in = body.get('autoreply')
    if isinstance(autoreply_in, dict):
        autoreply = {str(k): str(v) for k, v in autoreply_in.items() if str(k).strip()}
    else:
        autoreply = current.get('autoreply') or {}

    merged = {'bot': bot, 'crisp': crisp, 'autoreply': autoreply}
    try:
        cm.save(merged)
    except ConfigError as err:
        return jsonify(ok=False, error='配置校验失败', problems=err.problems), 400

    log.info('配置已保存')
    bus.event('system', '配置已保存')

    engine_status = None
    restart_error = None
    engine = _engine()
    if engine is not None and engine.state in ('running', 'starting', 'error'):
        try:
            engine_status = engine.restart()
            log.info('引擎已应用新配置并重启')
        except EngineError as err:
            restart_error = str(err)
            engine_status = engine.status()

    return jsonify(ok=True, engine=engine_status, restart_error=restart_error)


# ---------- 引擎控制 ----------

@bp.post('/api/engine/<action>')
@login_required
def engine_action(action):
    engine = _engine()
    if engine is None:
        return jsonify(ok=False, error='引擎未初始化'), 500
    try:
        if action == 'start':
            engine_status = engine.start()
        elif action == 'stop':
            engine_status = engine.stop()
        elif action == 'restart':
            engine_status = engine.restart()
        else:
            return jsonify(ok=False, error='未知操作'), 404
        return jsonify(ok=True, engine=engine_status)
    except EngineError as err:
        return jsonify(ok=False, error=str(err), engine=engine.status()), 400


# ---------- 连通性测试 ----------

@bp.post('/api/test/crisp')
@login_required
def test_crisp_route():
    body = request.get_json(silent=True) or {}
    current = runtime.config_manager.load()['crisp']
    cid = str(body.get('id') or '').strip() or str(current.get('id') or '')
    key = str(body.get('key') or '').strip()
    if not key or MASK_MARK in key:
        key = str(current.get('key') or '')
    website = str(body.get('website') or '').strip() or str(current.get('website') or '')
    if not (cid and key and website):
        return jsonify(ok=False, error='请先填写 Crisp ID / Key / Website ID'), 400
    result = connectivity.test_crisp(cid, key, website)
    summary = result.get('detail') or result.get('error')
    log.info('Crisp 连通性测试%s：%s', '通过' if result['ok'] else '失败', summary)
    bus.event('system', f"Crisp 连通性测试{'通过' if result['ok'] else '失败'}：{summary}")
    return jsonify(result)


@bp.post('/api/test/telegram')
@login_required
def test_telegram_route():
    body = request.get_json(silent=True) or {}
    current = runtime.config_manager.load()['bot']
    token = str(body.get('token') or '').strip()
    if not token or MASK_MARK in token:
        token = str(current.get('token') or '')
    if not token:
        return jsonify(ok=False, error='请先填写 Telegram Bot Token'), 400
    result = connectivity.test_telegram(token, str(current.get('proxy') or ''))
    summary = result.get('detail') or result.get('error')
    log.info('Telegram 连通性测试%s：%s', '通过' if result['ok'] else '失败', summary)
    bus.event('system', f"Telegram 连通性测试{'通过' if result['ok'] else '失败'}：{summary}")
    return jsonify(result)


# ---------- 实时记录 ----------

@bp.get('/api/logs')
@login_required
def get_logs():
    after = request.args.get('after', default=0, type=int)
    return jsonify(ok=True, items=bus.logs.since(after), last=bus.logs.last_seq())


@bp.get('/api/events')
@login_required
def get_events():
    after = request.args.get('after', default=0, type=int)
    return jsonify(ok=True, items=bus.events.since(after), last=bus.events.last_seq())
