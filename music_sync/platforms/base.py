# -*- coding: utf-8 -*-
"""平台适配器协议：所有音乐平台都实现这一组接口，上层 sync 不关心平台差异。"""
from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from typing import Any


class PlatformError(Exception):
    """平台通用异常基类。"""


class NotLoggedIn(PlatformError):
    """没有登录态 / cookie 失效。"""


class Unavailable(PlatformError):
    """歌曲无版权、下架、需要付费会员等『拿不到音频地址』的情况。"""


class Unsupported(PlatformError):
    """该平台不支持这个功能（例如汽水音乐无公开 API）。"""


@dataclass
class Track:
    """平台无关的曲目信息。"""
    platform: str
    id: str
    name: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def artist_text(self) -> str:
        return "; ".join(self.artists) if self.artists else ""

    def __str__(self) -> str:
        return f"{self.artist_text} - {self.name}" if self.artists else self.name


class BaseAdapter(abc.ABC):
    """平台适配器基类。

    子类必须声明 ``key`` / ``title``，实现 ``check_login`` 与 ``list_tracks``；
    支持下载的平台再实现 ``download`` 并把 ``supports_download`` 设为 True。
    """

    key: str = ""
    title: str = ""
    supports_download: bool = True
    #: 音质档位 -> 平台内部的等级名，由各适配器自行解释
    quality_levels: tuple[str, ...] = ("320", "192", "128")

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    # ------------------------- 会话 -------------------------
    def check_login(self) -> tuple[bool, str]:
        """返回 (是否已登录, 说明文字)。不抛异常。"""
        try:
            self.ensure_login()
            return True, "已登录"
        except PlatformError as exc:
            return False, str(exc)

    def ensure_login(self) -> None:
        """未登录时抛 NotLoggedIn。"""
        raise NotImplementedError

    # ------------------------- 歌单 -------------------------
    @abc.abstractmethod
    def list_tracks(self, playlist: str) -> list[Track]:
        """拉取歌单全部曲目。playlist 可以是 id、URL 或本地歌单名。"""

    # ------------------------- 下载 -------------------------
    def download(self, track: Track, dest_dir: str, quality: str = "320") -> str:
        """把曲目下载到 dest_dir，返回最终文件路径。

        失败时抛 :class:`Unavailable`（无版权/付费）或 :class:`PlatformError`。
        """
        raise Unsupported(f"{self.title} 暂不支持直接下载")

    # ------------------------- 工具 -------------------------
    def describe(self) -> dict:
        ok, msg = self.check_login()
        return {
            "key": self.key,
            "title": self.title,
            "supports_download": self.supports_download,
            "quality_levels": list(self.quality_levels),
            "logged_in": ok,
            "status": msg,
        }

    @staticmethod
    def _write(chunks, path: str) -> str:
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            for chunk in chunks:
                if chunk:
                    fh.write(chunk)
        os.replace(tmp, path)
        return path
