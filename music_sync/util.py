# -*- coding: utf-8 -*-
"""零散工具：日志、文件名清洗、HTTP 会话、人类可读大小。"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime

LOG_NAME = "music-sync.log"
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_configured = False


def setup_logging(log_path: str | None = None, verbose: bool = False) -> logging.Logger:
    """控制台 + 文件双写；单文件超过 2MB 自动滚动一次。"""
    global _configured
    logger = logging.getLogger("music_sync")
    if _configured:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_path:
        try:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            if os.path.exists(log_path) and os.path.getsize(log_path) > 2_000_000:
                os.replace(log_path, log_path + ".1")
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            pass
    _configured = True
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("music_sync")


def safe_name(text: str, limit: int = 120) -> str:
    """把「歌手 - 歌名」清洗成 Windows 合法文件名。"""
    name = _BAD.sub("_", text or "").strip().strip(".")[:limit]
    return name or "unknown"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def infer_platform(path: str) -> str:
    """从下载目录（如 D:\\CloudMusic、D:\\QQMusic）猜平台，供归置时打标签。"""
    low = (path or "").lower()
    for key, words in (
        ("netease", ("cloudmusic", "netease", "网易云")),
        ("qqmusic", ("qqmusic", "qq音乐")),
        ("kugou", ("kugou", "kuwo", "酷狗")),
        ("soda", ("soda", "qishui", "汽水")),
    ):
        if any(w in low for w in words):
            return key
    return ""


def make_session(cookie: str = "", referer: str = ""):
    """统一的 requests.Session；cookie 传原始 "k=v; k2=v2" 字符串。"""
    import requests

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if referer:
        session.headers["Referer"] = referer
    if cookie:
        for part in cookie.split(";"):
            if "=" in part:
                key, _, value = part.partition("=")
                session.cookies.set(key.strip(), value.strip())
    return session
