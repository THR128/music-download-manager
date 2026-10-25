# -*- coding: utf-8 -*-
"""MusicSync —— 多平台音乐歌单自动补齐工具。

模块地图：
- ``config``     配置读写（config.json）
- ``matching``   本地曲库扫描与歌名对账
- ``platforms``  平台适配器（网易云 / QQ音乐 / 酷狗 / 汽水占位）
- ``sync``       同步编排
- ``selfcheck``  跨终端环境自检
- ``ui``         设置界面与右下角提示窗
"""
__version__ = "1.1.0"
__all__ = ["__version__"]
