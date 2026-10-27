# -*- coding: utf-8 -*-
"""配置读写：config.json 是全工具的单一事实来源，设置界面/命令行/计划任务共用。

设计原则：
1. 每个字段都有安全默认值，缺字段也能跑；
2. 路径支持 ``~`` 与环境变量展开；
3. 保存时只写用户改过的键（保留未知键，方便回退版本）。
"""
from __future__ import annotations

import copy
import json
import os

DEFAULT_CONFIG: dict = {
    "platform": "netease",
    "playlist": "8508302371",
    "library_dir": "F:\\FFOutput",
    "client_download_dir": "D:\\CloudMusic",
    "quality": "320",
    "auto_move": True,
    "schedule": {
        "enabled": True,
        "mode": "weekly",          # weekly | daily | hours
        "weekday": "sunday",       # weekly 时生效
        "time": "21:00",           # weekly/daily 时生效
        "hours": 168,              # mode=hours 时的间隔小时
        "task_name": "MusicSync",
    },
    "notify": {
        "toast": True,
        "timeout": 40,
    },
    "download": {
        "ext": "mp3",
        "retries": 3,
        "sleep": 0.4,
        "overwrite": False,
    },
    "ignore_ids": [],
    "unlock": {
        "enabled": True,          # 自动解锁客户端下载的加密音频（.ncm / .qmc* / .kgm …）
        "scan_library": False,    # 除客户端下载目录外，也扫曲库目录里的加密文件
        "remove_source": False,   # 解锁成功后删掉原加密文件
    },
    "platforms": {
        "netease": {"cookie": "", "client_app_dir": "", "playlist": ""},
        "qqmusic": {"cookie": "", "playlist": ""},
        "kugou": {"cookie": "", "playlist": ""},
        "soda": {"cookie": "", "playlist": ""},
    },
}

CONFIG_NAME = "config.json"


def config_dir() -> str:
    """配置目录：优先 ``MUSIC_SYNC_HOME``，否则用户目录下的 .music-sync。"""
    env = os.environ.get("MUSIC_SYNC_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".music-sync")


def config_path(explicit: str | None = None) -> str:
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return os.path.join(config_dir(), CONFIG_NAME)


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def expand(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path or "")))


def load(path: str | None = None) -> dict:
    """读取配置；文件不存在时返回默认值（不落盘）。"""
    target = config_path(path)
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.isfile(target):
        with open(target, encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"配置文件格式错误 {target}: {exc}")
        cfg = _merge(cfg, data)
    for key in ("library_dir", "client_download_dir"):
        if cfg.get(key):
            cfg[key] = expand(cfg[key]) if os.path.isabs(os.path.expanduser(cfg[key])) else cfg[key]
    return cfg


def save(cfg: dict, path: str | None = None) -> str:
    target = config_path(path)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return target


def ensure_exists(path: str | None = None) -> str:
    """确保配置文件存在（不存在就写一份默认的），返回路径。"""
    target = config_path(path)
    if not os.path.isfile(target):
        save(copy.deepcopy(DEFAULT_CONFIG), target)
    return target


# ------------------------- 各平台歌单 -------------------------
def platform_playlist(cfg: dict, platform: str) -> str:
    """取某个平台的歌单标识。

    优先级：``platforms.<平台>.playlist`` > 旧的全局 ``playlist``。
    都为空时返回空串，交给对应适配器自动识别「我喜欢」。
    """
    entry = ((cfg or {}).get("platforms") or {}).get(platform) or {}
    value = str(entry.get("playlist") or "").strip()
    if value:
        return value
    return str((cfg or {}).get("playlist") or "").strip()


def set_platform_playlist(cfg: dict, platform: str, value: str) -> dict:
    """把歌单标识写到 ``platforms.<平台>.playlist``（空串 = 自动识别）。"""
    platforms = cfg.setdefault("platforms", {})
    entry = platforms.setdefault(platform, {})
    entry["playlist"] = (value or "").strip()
    return cfg
