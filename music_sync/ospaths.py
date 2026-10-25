# -*- coding: utf-8 -*-
"""平台差异的唯一出口 —— 其它模块不许自己拼平台相关的路径。

为什么要单独一个模块：配置目录、曲库默认目录、浏览器配置目录、音乐客户端数据目录
在 Windows / macOS / Linux 上各不相同，散落在各处很快就对不齐了。集中到这里之后，
移植新平台只需要改这一个文件。

所有函数都遵守三条约定：

1. **在 Windows 上也能安全调用**：macOS 分支只是不执行，不会去碰 ``/Applications``；
2. **只返回看起来存在的路径**：需要"猜目录"的函数一律 ``os.path.exists`` 过滤，
   调用方拿到空列表就应该走"没找到，请手动指定"的降级分支；
3. **不抛异常**：最差返回空列表 / 空字典，让上层继续跑而不是崩掉。

注意：``config_dir`` 的实现必须与 ``config.py`` 里那一份保持同样的规则
（``MUSIC_SYNC_HOME`` 优先），但本模块不 import ``config``，
避免 ``config`` ←→ ``ospaths`` 的循环依赖。
"""
from __future__ import annotations

import glob
import os
import sys

from .util import get_logger

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# 各平台音乐客户端的 .app 名字（macOS 用；大小写不敏感，允许不带 .app）
MAC_APP_NAMES: dict[str, tuple[str, ...]] = {
    "netease": ("NeteaseMusic", "NeteaseMusicMac", "网易云音乐", "NetEaseMusic"),
    "qqmusic": ("QQMusic", "QQ音乐", "QQMusicMac"),
    "kugou": ("KugouMusic", "KuGou", "酷狗音乐", "Kugou"),
    "soda": ("SodaMusic", "汽水音乐", "QishuiMusic"),
}

# Chrome 系的浏览器配置目录（Windows 用；保持与历史实现完全一致）
_WIN_BROWSER_PROFILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Edge", ("LOCAL", "Microsoft", "Edge", "User Data")),
    ("Chrome", ("LOCAL", "Google", "Chrome", "User Data")),
    ("Chrome Beta", ("LOCAL", "Google", "Chrome Beta", "User Data")),
    ("Chrome SxS", ("LOCAL", "Google", "Chrome SxS", "User Data")),
    ("Brave", ("LOCAL", "BraveSoftware", "Brave-Browser", "User Data")),
    ("Vivaldi", ("LOCAL", "Vivaldi", "User Data")),
    ("Opera", ("ROAMING", "Opera Software", "Opera Stable")),
    ("360极速", ("ROAMING", "360se6", "User Data")),
    ("360安全", ("LOCAL", "360Chrome", "Chrome", "User Data")),
    ("QQ浏览器", ("LOCAL", "Tencent", "QQBrowser", "User Data")),
    ("搜狗高速", ("ROAMING", "SogouExplorer", "User Data")),
)

# Chrome 系的浏览器配置目录（macOS 用；都是各自 User Data 下的 Default）
_MAC_BROWSER_PROFILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Chrome", ("Library", "Application Support", "Google", "Chrome", "Default")),
    ("Edge", ("Library", "Application Support", "Microsoft Edge", "Default")),
    ("Brave", ("Library", "Application Support", "BraveSoftware", "Brave-Browser", "Default")),
    ("Chromium", ("Library", "Application Support", "Chromium", "Default")),
    ("Vivaldi", ("Library", "Application Support", "Vivaldi", "Default")),
    ("Arc", ("Library", "Application Support", "Arc", "User Data", "Default")),
)


def _home() -> str:
    """当前用户主目录（``~`` 展开）。"""
    return os.path.expanduser("~")


def config_dir() -> str:
    """配置目录。

    * 环境变量 ``MUSIC_SYNC_HOME`` 优先（与 ``config.py`` 同一规则）；
    * Windows：``%USERPROFILE%\\.music-sync``（保持历史行为）；
    * macOS：``~/Library/Application Support/music-sync``；
    * 其它（Linux）：``~/.config/music-sync``。
    """
    env = os.environ.get("MUSIC_SYNC_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if IS_MAC:
        return os.path.join(_home(), "Library", "Application Support", "music-sync")
    if IS_WIN:
        return os.path.join(_home(), ".music-sync")
    return os.path.join(_home(), ".config", "music-sync")


def default_library_dir() -> str:
    """整理好的曲库目录的默认值。

    **Windows 不要改**（``F:\\FFOutput``）—— 老用户的曲库就在那儿，
    换默认值等于让程序找不到自己的歌。
    """
    if IS_MAC:
        return os.path.join(_home(), "Music", "本地曲库")
    if IS_WIN:
        return "F:\\FFOutput"
    return os.path.join(_home(), "Music")


def default_client_download_dir() -> str:
    """音乐客户端自己的下载目录（整理时从这里搬歌）。"""
    if IS_MAC:
        return os.path.join(_home(), "Music", "CloudMusic")
    if IS_WIN:
        return "D:\\CloudMusic"
    return os.path.join(_home(), "Music", "CloudMusic")


def browser_profiles() -> list[tuple[str, str]]:
    """返回 [(浏览器名, 配置目录), …]，只包含真实存在的目录。"""
    home = _home()
    local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    roaming = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
    roots = {"LOCAL": local, "ROAMING": roaming, "HOME": home}

    out: list[tuple[str, str]] = []
    if IS_MAC:
        for name, parts in _MAC_BROWSER_PROFILES:
            out.append((name, os.path.join(home, *parts)))
    else:
        for name, parts in _WIN_BROWSER_PROFILES:
            out.append((name, os.path.join(roots[parts[0]], *parts[1:])))
    return [(name, path) for name, path in out if os.path.isdir(path)]


def netease_client_dirs() -> list[str]:
    """网易云客户端的数据目录候选，按"最可能是当前正在用的"排前面。

    * Windows：``%LOCALAPPDATA%\\NetEase\\CloudMusic\\webapp*``（按修改时间倒序）；
    * macOS：两处容器路径 + ``.app`` 本体（后者只用于提示用户"装是装了但没登录过"）。
    """
    if IS_MAC:
        home = _home()
        cands = [
            os.path.join(home, "Library", "Application Support", "com.netease.163music"),
            os.path.join(home, "Library", "Containers", "com.netease.163music",
                         "Data", "Library", "Application Support", "com.netease.163music"),
            "/Applications/NeteaseMusic.app",
        ]
        return [p for p in cands if os.path.isdir(p)]

    home = _home()
    local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    base = os.path.join(local, "NetEase", "CloudMusic")
    found = [p for p in glob.glob(os.path.join(base, "webapp*")) if os.path.isdir(p)]
    try:
        found.sort(key=os.path.getmtime, reverse=True)
    except OSError:
        pass
    return found


def mac_installed_apps() -> dict[str, str]:
    """macOS 上装了哪些音乐客户端：``{平台key: .app 路径}``。

    在 ``/Applications`` 与 ``~/Applications`` 里找，大小写不敏感，
    允许不带 ``.app`` 后缀（有些发行版就是文件夹）。非 macOS 一律返回 ``{}``。
    """
    if not IS_MAC:
        return {}

    found_entries: dict[str, str] = {}
    for base in ("/Applications", os.path.join(_home(), "Applications")):
        if not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError as exc:                     # 权限不足等，降级即可
            get_logger().debug("列不出 %s: %s", base, exc)
            entries = []
        for entry in entries:
            key = entry[:-4] if entry.lower().endswith(".app") else entry
            found_entries.setdefault(key.lower(), os.path.join(base, entry))

    apps: dict[str, str] = {}
    for platform_key, names in MAC_APP_NAMES.items():
        for name in names:
            path = found_entries.get(name.lower())
            if path:
                apps[platform_key] = path
                break
        else:
            # listdir 拿不到的场合（沙箱/权限）再按候选名直接探一次
            for name in names:
                for suffix in (".app", ""):
                    path = os.path.join("/Applications", name + suffix)
                    if os.path.exists(path):
                        apps[platform_key] = path
                        break
                if platform_key in apps:
                    break
    return apps
