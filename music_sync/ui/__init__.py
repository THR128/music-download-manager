# -*- coding: utf-8 -*-
"""图形界面包。

目前只有一个设置窗口 :mod:`music_sync.ui.settings`，入口函数 ``run(config_path)``
由命令行 ``python -m music_sync settings`` 调用。界面只依赖标准库 tkinter，
不引入任何第三方 UI 框架，保证「装完 Python 双击就能用」。
"""
from __future__ import annotations

__all__ = ["run"]


def run(config_path: str | None = None) -> int:
    """懒加载入口：只有真正要用界面时才 import tkinter。"""
    from .settings import run as _run

    return _run(config_path)
