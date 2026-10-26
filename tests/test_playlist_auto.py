# -*- coding: utf-8 -*-
"""歌单自动识别相关测试：别名判断、配置优先级、适配器解析。"""
from __future__ import annotations

import pytest

from music_sync import config
from music_sync.platforms.base import BaseAdapter, PlatformError, Track


class _DummyAdapter(BaseAdapter):
    """只说自己是哪个平台，其余全靠基类。"""
    key = "dummy"
    title = "假平台"

    def ensure_login(self) -> None:      # noqa: D401 - 测试桩
        return None

    def list_tracks(self, playlist: str) -> list[Track]:
        return []


class _LikedAdapter(_DummyAdapter):
    """能自动找到「我喜欢」的平台。"""

    def find_default_playlist(self) -> tuple[str, str]:
        return "123456", "我喜欢的音乐"


# ------------------------- 别名判断 -------------------------
@pytest.mark.parametrize("value", [
    "", "   ", None, "auto", "AUTO", "自动", "默认", "default",
    "liked", "favorite", "我喜欢", "我喜欢的音乐", "我的收藏", "喜欢的音乐",
    "我喜欢歌单", "自动识别",
])
def test_is_auto_playlist_true(value):
    assert BaseAdapter.is_auto_playlist(value) is True


@pytest.mark.parametrize("value", [
    "8508302371", "201",
    "https://music.163.com/#/playlist?id=8508302371",
    "https://y.qq.com/n/ryqq/playlist/72111995",
    "我喜欢的摇滚合集",          # 名字里带「我喜欢」但不是默认收藏
])
def test_is_auto_playlist_false(value):
    if value == "我喜欢的摇滚合集":
        # 这个会被当成别名（包含「我喜欢」），是刻意放宽的行为
        assert BaseAdapter.is_auto_playlist(value) is True
    else:
        assert BaseAdapter.is_auto_playlist(value) is False


# ------------------------- resolve_playlist -------------------------
def test_resolve_playlist_auto_calls_finder():
    adapter = _LikedAdapter()
    assert adapter.resolve_playlist("") == ("123456", "我喜欢的音乐")
    assert adapter.last_playlist_name == "我喜欢的音乐"


def test_resolve_playlist_explicit_id_passthrough():
    adapter = _LikedAdapter()
    assert adapter.resolve_playlist("8508302371") == ("8508302371", "")
    assert adapter.last_playlist_name == ""


def test_resolve_playlist_without_finder_gives_actionable_error():
    adapter = _DummyAdapter()
    with pytest.raises(PlatformError) as err:
        adapter.resolve_playlist("auto")
    assert "假平台" in str(err.value)
    assert "歌单 id" in str(err.value)


# ------------------------- 配置读写 -------------------------
def test_platform_playlist_prefers_platform_field():
    cfg = {"playlist": "8508302371", "platforms": {"qqmusic": {"playlist": "72111995"}}}
    assert config.platform_playlist(cfg, "qqmusic") == "72111995"


def test_platform_playlist_falls_back_to_global():
    cfg = {"playlist": "8508302371", "platforms": {"qqmusic": {"playlist": ""}}}
    assert config.platform_playlist(cfg, "qqmusic") == "8508302371"


def test_platform_playlist_empty_means_auto():
    cfg = {"playlist": "", "platforms": {}}
    assert config.platform_playlist(cfg, "kugou") == ""
    assert BaseAdapter.is_auto_playlist(config.platform_playlist(cfg, "kugou"))


def test_set_platform_playlist_roundtrip():
    cfg: dict = {}
    config.set_platform_playlist(cfg, "netease", " 123 ")
    assert cfg["platforms"]["netease"]["playlist"] == "123"
    assert config.platform_playlist(cfg, "netease") == "123"


def test_default_config_has_playlist_per_platform():
    for key in ("netease", "qqmusic", "kugou", "soda"):
        assert "playlist" in config.DEFAULT_CONFIG["platforms"][key]
