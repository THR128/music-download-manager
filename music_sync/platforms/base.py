# -*- coding: utf-8 -*-
"""平台适配器协议：所有音乐平台都实现这一组接口，上层 sync 不关心平台差异。"""
from __future__ import annotations

import abc
import os
import re
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
    #: 各平台「默认收藏」歌单的常见叫法：留空时按这些名字去找
    LIKED_ALIASES: tuple[str, ...] = (
        "我喜欢", "我喜欢的音乐", "喜欢的音乐", "我的喜欢", "我最喜欢",
        "我的收藏", "默认收藏", "收藏夹", "喜欢的歌曲", "我喜欢的歌",
    )
    #: 可以直接当成「自动识别」的写法
    AUTO_WORDS: frozenset[str] = frozenset({
        "", "auto", "自动", "自动识别", "默认", "default", "liked", "like",
        "favorite", "favorites",
    })

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        #: 最近一次解析出的歌单名（自动识别时由平台返回，供日志/提示窗显示）
        self.last_playlist_name: str = ""

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
        """拉取歌单全部曲目。playlist 可以是 id、URL 或歌单名（含「我喜欢」这类别名）。"""

    @classmethod
    def is_auto_playlist(cls, playlist: str | None) -> bool:
        """判断这个歌单标识是不是「留空 / auto / 我喜欢」这类自动识别写法。"""
        text = (playlist or "").strip().lower()
        if text in cls.AUTO_WORDS:
            return True
        squeezed = re.sub(r"[\s_\-·。.]+", "", text)
        if squeezed in cls.AUTO_WORDS:
            return True
        if squeezed.endswith("歌单") and squeezed[:-2] in cls.AUTO_WORDS:
            return True
        return any(alias in text for alias in cls.LIKED_ALIASES)

    def find_default_playlist(self) -> tuple[str, str]:
        """找当前账号的「默认收藏（我喜欢）」歌单，返回 ``(歌单 id, 歌单名)``。

        找不到就抛 :class:`PlatformError`，让上层提示用户手动填歌单。
        """
        raise PlatformError(
            f"{self.title} 还不能自动识别「我喜欢」，请在设置里填歌单 id 或歌单链接")

    def resolve_playlist(self, playlist: str | None) -> tuple[str, str]:
        """把配置里的歌单标识解析成 ``(可以直接拉取的 id, 展示用歌单名)``。

        留空或写「我喜欢」这类别名时，转调 :meth:`find_default_playlist`。
        """
        if self.is_auto_playlist(playlist):
            pid, name = self.find_default_playlist()
            self.last_playlist_name = name or self.last_playlist_name
            return pid, name
        text = (playlist or "").strip()
        return text, ""

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
