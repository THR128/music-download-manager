# -*- coding: utf-8 -*-
"""平台注册表：新增平台只要在这里挂一行。"""
from __future__ import annotations

from .base import BaseAdapter, NotLoggedIn, PlatformError, Track, Unavailable, Unsupported

#: key -> (显示名, 是否支持直接下载)
PLATFORMS: dict[str, tuple[str, bool]] = {
    "netease": ("网易云音乐", True),
    "qqmusic": ("QQ音乐", True),
    "kugou": ("酷狗音乐", True),
    "soda": ("汽水音乐", False),
}

_ALIASES = {
    "163": "netease", "net ease": "netease", "网易": "netease", "网易云": "netease",
    "qq": "qqmusic", "qq音乐": "qqmusic", "tencent": "qqmusic",
    "kugou": "kugou", "酷狗": "kugou",
    "soda": "soda", "qishui": "soda", "汽水": "soda", "汽水音乐": "soda",
}


def resolve(name: str) -> str:
    key = (name or "").strip().lower()
    return _ALIASES.get(key, key)


def _autologin(key: str, config: dict | None) -> dict | None:
    """配置里没有登录态时，尝试从浏览器自动读取（让不懂 cookie 的用户也能用）。"""
    if not isinstance(config, dict) or (config.get("cookie") or "").strip():
        return config
    if key not in ("qqmusic", "kugou"):
        return config
    try:
        from .. import browser_cookies
        src = browser_cookies.best(key)
    except Exception:                                  # noqa: BLE001 - 登录态读取永远不该让主流程挂掉
        return config
    if not src:
        return config
    cfg = dict(config)
    cfg["cookie"] = browser_cookies.to_header(src.cookies)
    if key == "qqmusic" and src.cookies.get("uin"):
        cfg.setdefault("uin", src.cookies["uin"])
    if key == "kugou" and src.cookies.get("kg_dfid"):
        cfg.setdefault("dfid", src.cookies["kg_dfid"])
    return cfg


def create(key: str, config: dict | None = None) -> BaseAdapter:
    """按 key 实例化适配器；模块缺失时抛 Unsupported。"""
    key = resolve(key)
    if key not in PLATFORMS:
        raise Unsupported(f"不认识的平台：{key}（可选：{', '.join(PLATFORMS)}）")
    config = _autologin(key, config)
    try:
        if key == "netease":
            from .netease import NeteaseAdapter
            return NeteaseAdapter(config)
        if key == "qqmusic":
            from .qqmusic import QQMusicAdapter
            return QQMusicAdapter(config)
        if key == "kugou":
            from .kugou import KugouAdapter
            return KugouAdapter(config)
        if key == "soda":
            from .soda import SodaAdapter
            return SodaAdapter(config)
    except ImportError as exc:                        # pragma: no cover - 可选依赖缺失
        raise Unsupported(f"{PLATFORMS[key][0]} 适配器未安装：{exc}") from exc
    raise Unsupported(f"不认识的平台：{key}")


def names() -> list[str]:
    return list(PLATFORMS)


__all__ = [
    "PLATFORMS", "BaseAdapter", "Track", "PlatformError", "NotLoggedIn",
    "Unavailable", "Unsupported", "create", "names", "resolve",
]
