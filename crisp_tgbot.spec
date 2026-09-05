# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置：单文件 Linux 可执行程序
# 用法（在 Linux 上）：python3 -m PyInstaller crisp_tgbot.spec --noconfirm --clean
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    # Modules/ 插件为动态导入，必须显式声明
    'Modules.getUnread',
    'Modules.crispEventsHandler',
] + collect_submodules('engineio')   # socketio 异步驱动为动态选择

a = Analysis(
    ['bot.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 控制台静态资源（CSS/JS/HTML）
        ('core/webapp/static', 'core/webapp/static'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='crisp_tgbot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
