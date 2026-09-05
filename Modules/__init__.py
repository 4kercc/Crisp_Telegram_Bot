"""插件加载器：每次引擎启动时（重新）加载 Modules/ 下的模块，
返回启用且符合契约的模块列表 [(name, module), ...]。

模块契约：
  class Conf:        desc / method ('repeating'|'events') / interval（repeating 用）
  def enabled(config) -> bool          是否随当前配置启用
  async def exec(context)              入口（repeating 为定时回调，events 为启动后 5s 一次性回调）
  async def stop()                     可选；引擎停止时调用，用于释放资源
"""
import importlib
import os
import pkgutil
import sys

PKG_NAME = 'Modules'

# PyInstaller 打包后没有源码文件可扫描，按此清单加载（需与 hiddenimports 保持一致）
FROZEN_MODULES = ('getUnread', 'crispEventsHandler')


def load(config=None):
    if config is None:
        from core.runtime import runtime
        config = runtime.get_config() or {}

    if getattr(sys, 'frozen', False):
        names = list(FROZEN_MODULES)
    else:
        pkgpath = os.path.dirname(__file__)
        names = [file for _, file, _ in pkgutil.iter_modules([pkgpath])]

    loaded = []
    for file in names:
        module = importlib.import_module(PKG_NAME + '.' + file)
        if not (hasattr(module, 'Conf') and hasattr(module, 'exec')):
            continue
        enabled = getattr(module, 'enabled', None)
        if enabled is None or enabled(config):
            loaded.append((file, module))
    return loaded
