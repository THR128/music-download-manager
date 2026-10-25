# -*- coding: utf-8 -*-
"""三个平台适配器的离线单测：不联网、不需要 cookie。

测的是「解析响应 JSON -> Track / 直链」这些纯函数，以及适配器的契约
（key、supports_download、空 cookie 的提示）。下载路径用假 session 验证落盘和命名，
所以整个测试文件跑起来不会碰网络。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:                              # 支持 python tests/test_adapters.py
    sys.path.insert(0, ROOT)

from music_sync.platforms import create                    # noqa: E402
from music_sync.platforms.base import (                    # noqa: E402
    BaseAdapter, NotLoggedIn, PlatformError, Track, Unavailable, Unsupported,
)
from music_sync.platforms.kugou import (                   # noqa: E402
    KugouAdapter, build_play_params, parse_kugou_play_json,
    parse_kugou_playlist_json, parse_kugou_search_json, parse_playlist_id as kugou_playlist_id,
)
from music_sync.platforms.qqmusic import (                 # noqa: E402
    QQMusicAdapter, build_vkey_filename, build_vkey_payload, parse_playlist_id as qq_playlist_id,
    parse_playlist_json, parse_search_json, parse_song_detail_json, parse_vkey_json,
    quality_chain, quality_to_file,
)
from music_sync.platforms.soda import SodaAdapter          # noqa: E402
from music_sync.util import safe_name                      # noqa: E402

# --------------------------------------------------------------------------
# 样例数据（照真实接口形状手写，不含任何人的账号信息）
# --------------------------------------------------------------------------
QQ_PLAYLIST_FCG = {
    "code": 0,
    "cdlist": [{
        "disstid": 123456,
        "dissname": "测试歌单",
        "songlist": [
            {
                "songmid": "0039MnYb0qxYhV",
                "songname": "晴天",
                "singer": [{"name": "周杰伦"}],
                "interval": 269,
                "album": {"name": "叶惠美", "mid": "001albummid"},
                "media_mid": "0039MnYb0qxYhV",
            },
            {
                "songInfo": {
                    "songmid": "001Qu4I30eVFYb",
                    "name": "菊花台",
                    "singer": [{"name": "周杰伦"}],
                    "album": {"name": "依然范特西"},
                    "interval": 263000,
                }
            },
            {"songmid": "", "songname": "没有 mid 的脏数据"},
        ],
    }],
}

QQ_PLAYLIST_MUSICU = {
    "code": 0,
    "req_0": {
        "code": 0,
        "data": {
            "dirinfo": {
                "songlist": [{
                    "mid": "002midXiaoxiang",
                    "name": "稻香",
                    "singer": [{"name": "周杰伦"}],
                    "album": {"name": "魔杰座"},
                    "interval": 223,
                }]
            }
        },
    },
}

QQ_SONG_DETAIL = {
    "code": 0,
    "req_0": {
        "code": 0,
        "data": {
            "track_info": {
                "name": "稻香",
                "singer": [{"name": "周杰伦"}],
                "album": {"name": "魔杰座"},
                "interval": 223,
            }
        },
    },
}

QQ_SEARCH = {
    "code": 0,
    "data": {
        "song": {
            "list": [{
                "mid": "003searchMid",
                "name": "七里香",
                "singer": [{"name": "周杰伦"}],
                "album": {"name": "七里香"},
                "interval": 299,
            }]
        }
    },
}

VKEY_OK = {
    "code": 0,
    "req_0": {
        "code": 0,
        "data": {
            "retcode": 0,
            "midurlinfo": [{"songmid": "0039MnYb0qxYhV",
                            "purl": "M8000039MnYb0qxYhV.mp3",
                            "filename": "M8000039MnYb0qxYhV.mp3"}],
            "sip": ["https://isure.stream.qqmusic.qq.com/"],
        },
    },
}

VKEY_NO_SLASH = {
    "req_0": {
        "data": {
            "midurlinfo": [{"purl": "/M5000039MnYb0qxYhV.mp3"}],
            "sip": ["http://ws.stream.qqmusic.qq.com"],
        }
    }
}

VKEY_ABSOLUTE = {
    "req_0": {"data": {"midurlinfo": [{"purl": "https://dl.stream.qqmusic.qq.com/a.mp3"}],
                       "sip": ["https://isure.stream.qqmusic.qq.com/"]}}
}

VKEY_EMPTY = {"req_0": {"data": {"midurlinfo": [{"songmid": "x", "purl": ""}],
                                 "sip": ["https://isure.stream.qqmusic.qq.com/"]}}}

KUGOU_PLIST = {
    "status": 1,
    "list": {
        "info": [
            {
                "hash": "ABC123HASH",
                "filename": "周杰伦 - 晴天",
                "singername": "周杰伦",
                "album_name": "叶惠美",
                "album_id": "777",
                "duration": 269,
            },
            {
                "hash": "DEF456HASH",
                "filename": "周杰伦、费玉清 - 千里之外",
                "album_name": "依然范特西",
                "album_id": 778,
                "duration": 235,
            },
        ]
    },
}

KUGOU_PLAYLIST_WWWAPI = {
    "status": 1,
    "err_code": 0,
    "data": {
        "info": [{
            "hash": "XYZ789HASH",
            "filename": "夜曲 - 周杰伦",
            "album_id": "9",
            "timelength": 226000,
        }]
    },
}

KUGOU_PLAY = {
    "status": 1,
    "err_code": 0,
    "data": {
        "play_url": "http://trackercdn.kugou.com/ABC123HASH.mp3",
        "audio_name": "周杰伦 - 晴天",
        "authors": [{"author_name": "周杰伦"}],
        "album_name": "叶惠美",
        "timelength": 269000,
        "hash": "ABC123HASH",
        "album_id": "777",
    },
}

KUGOU_PLAY_EMPTY = {"status": 1, "data": {"play_url": "", "hash": "ABC123HASH"}}

KUGOU_SEARCH = {
    "data": {
        "info": [{
            "hash": "SEARCHHASH",
            "filename": "周杰伦 - 晴天",
            "singername": "周杰伦",
            "album_name": "叶惠美",
            "duration": 269,
        }]
    }
}


class _FakeResponse:
    """假的 requests.Response：只实现下载路径需要的几个方法。"""

    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1024):
        for chunk in self._chunks:
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeSession:
    """假的 session：GET 直接返回预先准备好的字节流。"""

    def __init__(self, chunks):
        self._chunks = chunks
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        assert kwargs.get("timeout"), "下载请求必须带超时"
        return _FakeResponse(self._chunks)


def _cookie(**pairs) -> str:
    return "; ".join(f"{key}={value}" for key, value in pairs.items())


class TestQQParse(unittest.TestCase):
    """QQ 音乐纯函数。"""

    def test_parse_playlist_id(self):
        self.assertEqual(qq_playlist_id("123456"), "123456")
        self.assertEqual(qq_playlist_id("https://y.qq.com/n/ryqq/playlist/8508302371"), "8508302371")
        self.assertEqual(qq_playlist_id("https://y.qq.com/n/ryqq/playlist?id=42&x=1"), "42")
        with self.assertRaises(PlatformError):
            qq_playlist_id("https://y.qq.com/n/ryqq/album/123")

    def test_parse_playlist_fcg(self):
        tracks = parse_playlist_json(QQ_PLAYLIST_FCG)
        self.assertEqual(len(tracks), 2)                   # 没 mid 的那条被丢掉
        first, second = tracks
        self.assertEqual(first.platform, "qqmusic")
        self.assertEqual(first.id, "0039MnYb0qxYhV")
        self.assertEqual(first.name, "晴天")
        self.assertEqual(first.artists, ["周杰伦"])
        self.assertEqual(first.album, "叶惠美")
        self.assertEqual(first.duration_ms, 269000)        # 秒 -> 毫秒
        self.assertEqual(first.extra["media_mid"], "0039MnYb0qxYhV")
        # 套了一层 songInfo 的写法
        self.assertEqual(second.id, "001Qu4I30eVFYb")
        self.assertEqual(second.name, "菊花台")
        self.assertEqual(second.duration_ms, 263000)       # 已经是毫秒就不动
        self.assertEqual(second.extra["media_mid"], "001Qu4I30eVFYb")

    def test_parse_playlist_musicu(self):
        tracks = parse_playlist_json(QQ_PLAYLIST_MUSICU)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].id, "002midXiaoxiang")
        self.assertEqual(tracks[0].artists, ["周杰伦"])
        self.assertEqual(tracks[0].album, "魔杰座")

    def test_parse_song_detail(self):
        detail = parse_song_detail_json(QQ_SONG_DETAIL)
        self.assertEqual(detail["name"], "稻香")
        self.assertEqual(detail["artists"], ["周杰伦"])
        self.assertEqual(detail["album"], "魔杰座")
        self.assertEqual(detail["duration_ms"], 223000)

    def test_parse_search(self):
        tracks = parse_search_json(QQ_SEARCH)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].id, "003searchMid")
        self.assertEqual(tracks[0].artists, ["周杰伦"])

    def test_vkey_url_join(self):
        self.assertEqual(parse_vkey_json(VKEY_OK),
                         "https://isure.stream.qqmusic.qq.com/M8000039MnYb0qxYhV.mp3")
        self.assertEqual(parse_vkey_json(VKEY_NO_SLASH),
                         "http://ws.stream.qqmusic.qq.com/M5000039MnYb0qxYhV.mp3")
        self.assertEqual(parse_vkey_json(VKEY_ABSOLUTE),
                         "https://dl.stream.qqmusic.qq.com/a.mp3")

    def test_vkey_unavailable(self):
        with self.assertRaises(Unavailable) as ctx:
            parse_vkey_json(VKEY_EMPTY)
        self.assertIn("没有返回音频地址", str(ctx.exception))

    def test_quality_mapping(self):
        self.assertEqual(quality_to_file("320"), ("M800", ".mp3"))
        self.assertEqual(quality_to_file("192"), ("M500", ".mp3"))
        self.assertEqual(quality_to_file("128"), ("M500", ".mp3"))
        self.assertEqual(quality_to_file("flac"), ("F000", ".flac"))
        self.assertEqual(quality_to_file("瞎写"), ("M800", ".mp3"))
        self.assertEqual(quality_chain("320"), ["320", "128"])
        self.assertEqual(quality_chain("192"), ["192", "128"])
        self.assertEqual(quality_chain("128"), ["128"])
        self.assertEqual(quality_chain("flac"), ["flac"])   # 无损不偷偷降级

    def test_vkey_filename_uses_media_mid(self):
        self.assertEqual(build_vkey_filename("songmid", "mediamid", "320"), "M800mediamid.mp3")
        self.assertEqual(build_vkey_filename("songmid", "", "192"), "M500songmid.mp3")
        self.assertEqual(build_vkey_filename("songmid", None, "flac"), "F000songmid.flac")

    def test_vkey_payload_shape(self):
        payload = build_vkey_payload("songmid", "M800songmid.mp3", "1234567890", "10086")
        req = payload["req_0"]
        self.assertEqual(req["module"], "music.vkey.GetVkey")
        self.assertEqual(req["method"], "UrlGetVkey")
        self.assertEqual(req["param"]["songmid"], ["songmid"])
        self.assertEqual(req["param"]["filename"], ["M800songmid.mp3"])
        self.assertEqual(req["param"]["guid"], "1234567890")
        self.assertEqual(req["param"]["uin"], "10086")


class TestKugouParse(unittest.TestCase):
    """酷狗纯函数。"""

    def test_parse_playlist_id(self):
        self.assertEqual(kugou_playlist_id("123456"), "123456")
        self.assertEqual(kugou_playlist_id("https://m.kugou.com/plist/list/123456/?json=true"),
                         "123456")
        self.assertEqual(kugou_playlist_id("https://www.kugou.com/yy/special/single/777.html"),
                         "777")
        self.assertEqual(kugou_playlist_id("https://www.kugou.com/yy/special/single/123.html?specialid=888"),
                         "888")
        with self.assertRaises(PlatformError):
            kugou_playlist_id("https://www.kugou.com/")

    def test_parse_plist_info(self):
        tracks = parse_kugou_playlist_json(KUGOU_PLIST)
        self.assertEqual(len(tracks), 2)
        first, second = tracks
        self.assertEqual(first.platform, "kugou")
        self.assertEqual(first.id, "ABC123HASH")                  # id 就是 hash
        self.assertEqual(first.name, "晴天")                      # 从 filename 拆出来
        self.assertEqual(first.artists, ["周杰伦"])               # 从 singername 取
        self.assertEqual(first.album, "叶惠美")
        self.assertEqual(first.duration_ms, 269000)
        self.assertEqual(first.extra["album_id"], "777")          # 下载时要带上
        self.assertEqual(second.id, "DEF456HASH")
        self.assertEqual(second.name, "千里之外")
        self.assertEqual(second.artists, ["周杰伦", "费玉清"])    # 顿号拆开
        self.assertEqual(second.extra["album_id"], "778")

    def test_parse_wwwapi_shape(self):
        tracks = parse_kugou_playlist_json(KUGOU_PLAYLIST_WWWAPI)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].id, "XYZ789HASH")
        self.assertEqual(tracks[0].name, "周杰伦")               # filename 是「歌名 - 歌手」
        self.assertEqual(tracks[0].duration_ms, 226000)

    def test_parse_play_json(self):
        info = parse_kugou_play_json(KUGOU_PLAY)
        self.assertEqual(info["url"], "http://trackercdn.kugou.com/ABC123HASH.mp3")
        self.assertEqual(info["hash"], "ABC123HASH")
        self.assertEqual(info["album_id"], "777")
        self.assertEqual(info["artists"], ["周杰伦"])
        self.assertEqual(info["album"], "叶惠美")
        self.assertEqual(info["duration_ms"], 269000)

    def test_play_json_unavailable(self):
        with self.assertRaises(Unavailable) as ctx:
            parse_kugou_play_json(KUGOU_PLAY_EMPTY)
        self.assertIn("没有返回播放地址", str(ctx.exception))

    def test_build_play_params(self):
        params = build_play_params("ABC123HASH", "777", "mid-xyz", timestamp=1700000000000)
        self.assertEqual(params["r"], "play/getdata")
        self.assertEqual(params["hash"], "ABC123HASH")
        self.assertEqual(params["album_id"], "777")
        self.assertEqual(params["mid"], "mid-xyz")
        self.assertEqual(params["platid"], "4")
        self.assertEqual(params["_"], "1700000000000")
        # album_id 缺省要兜底成 "0"，不能让请求带空值
        self.assertEqual(build_play_params("h", "", "m", timestamp=1)["album_id"], "0")

    def test_parse_search(self):
        tracks = parse_kugou_search_json(KUGOU_SEARCH)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].id, "SEARCHHASH")
        self.assertEqual(tracks[0].artists, ["周杰伦"])


class TestAdapterContract(unittest.TestCase):
    """三个适配器的对外契约（不联网）。"""

    def test_keys_and_titles(self):
        self.assertEqual(QQMusicAdapter.key, "qqmusic")
        self.assertEqual(QQMusicAdapter.title, "QQ音乐")
        self.assertEqual(KugouAdapter.key, "kugou")
        self.assertEqual(KugouAdapter.title, "酷狗音乐")
        self.assertEqual(SodaAdapter.key, "soda")
        self.assertEqual(SodaAdapter.title, "汽水音乐")
        for cls in (QQMusicAdapter, KugouAdapter, SodaAdapter):
            self.assertTrue(issubclass(cls, BaseAdapter))

    def test_supports_download(self):
        self.assertIs(QQMusicAdapter().supports_download, True)
        self.assertIs(KugouAdapter().supports_download, True)
        self.assertIs(SodaAdapter().supports_download, False)

    def test_empty_cookie_says_fill_cookie(self):
        for adapter in (QQMusicAdapter({}), QQMusicAdapter({"cookie": "  "}),
                        KugouAdapter({}), KugouAdapter(None), SodaAdapter({})):
            ok, message = adapter.check_login()
            self.assertIs(ok, False)
            self.assertIn("登录", message)

    def test_qq_and_kugou_login_local_check(self):
        qq = QQMusicAdapter({"cookie": _cookie(uin="o0012345", qm_keyst="k")})
        self.assertEqual(qq.check_login(), (True, "已登录"))
        kugou = KugouAdapter({"cookie": _cookie(kg_mid="a" * 32)})
        self.assertEqual(kugou.check_login(), (True, "已登录"))
        # 有 cookie 但没有 uin/密钥，应该明确提示，而不是假装已登录
        ok, message = QQMusicAdapter({"cookie": _cookie(foo="bar")}).check_login()
        self.assertIs(ok, False)
        self.assertIn("登录信息", message)

    def test_soda_is_honest(self):
        soda = SodaAdapter({})
        with self.assertRaises(Unsupported) as list_ctx:
            soda.list_tracks("https://music.douyin.com/qishui/playlist/123")
        self.assertIn("没有公开 API", str(list_ctx.exception))
        with self.assertRaises(Unsupported):
            soda.download(Track(platform="soda", id="1", name="x"), tempfile.gettempdir())
        with self.assertRaises(Unsupported):
            soda.search("晴天")
        ok, message = SodaAdapter({"cookie": "a=b"}).check_login()
        self.assertIs(ok, False)
        self.assertIn("没有公开 API", message)
        self.assertEqual(SodaAdapter.quality_levels, ())

    def test_create_factory(self):
        self.assertIsInstance(create("qqmusic", {"cookie": ""}), QQMusicAdapter)
        self.assertIsInstance(create("kugou", {"cookie": ""}), KugouAdapter)
        self.assertIsInstance(create("soda", {"cookie": ""}), SodaAdapter)

    def test_describe(self):
        info = KugouAdapter({}).describe()
        self.assertEqual(info["key"], "kugou")
        self.assertIs(info["logged_in"], False)
        self.assertTrue(info["quality_levels"])


class TestDownloadOffline(unittest.TestCase):
    """用假 session 验证下载落盘 + 文件命名，全程不联网。"""

    def test_qq_download_writes_mp3(self):
        track = parse_playlist_json(QQ_PLAYLIST_FCG)[0]
        adapter = QQMusicAdapter({"cookie": _cookie(uin="o0012345", qm_keyst="k")})
        adapter._session = _FakeSession([b"ID3", b"audio-bytes"])
        adapter._request_json = lambda *args, **kwargs: VKEY_OK
        with tempfile.TemporaryDirectory() as tmp:
            written = adapter.download(track, tmp, "320")
            expected = os.path.join(tmp, safe_name(str(track)) + ".mp3")
            self.assertEqual(written, expected)
            self.assertTrue(os.path.isfile(expected))
            with open(expected, "rb") as fh:
                self.assertEqual(fh.read(), b"ID3audio-bytes")
            self.assertEqual(len(adapter._session.requests), 1)

    def test_qq_flac_without_permission_is_unavailable(self):
        track = parse_playlist_json(QQ_PLAYLIST_FCG)[0]
        adapter = QQMusicAdapter({"cookie": _cookie(uin="1", qm_keyst="k")})
        adapter._session = _FakeSession([b"x"])
        adapter._request_json = lambda *args, **kwargs: VKEY_EMPTY
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Unavailable):
                adapter.download(track, tmp, "flac")
            self.assertEqual(os.listdir(tmp), [])              # 失败不能留下残渣

    def test_kugou_download_writes_mp3(self):
        track = parse_kugou_playlist_json(KUGOU_PLIST)[0]
        adapter = KugouAdapter({"cookie": _cookie(kg_mid="b" * 32)})
        adapter._session = _FakeSession([b"MP3DATA"])
        adapter._request_json = lambda *args, **kwargs: KUGOU_PLAY
        with tempfile.TemporaryDirectory() as tmp:
            written = adapter.download(track, tmp, "320")
            expected = os.path.join(tmp, safe_name(str(track)) + ".mp3")
            self.assertEqual(written, expected)
            with open(expected, "rb") as fh:
                self.assertEqual(fh.read(), b"MP3DATA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
