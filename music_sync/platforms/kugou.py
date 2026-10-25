# -*- coding: utf-8 -*-
"""酷狗音乐适配器（登录态只认配置里的 cookie，不碰本地客户端）。

接口来源与可信度（重要，别当成"已验证"）：
- 播放地址：``wwwapi.kugou.com/yy/index.php?r=play/getdata``，需要 ``hash`` + ``album_id``
  + cookie 里的 ``kg_mid``（没有就随机生成一个 32 位 mid 塞进 cookie，不写死任何人的账号）；
- 歌单：``m.kugou.com/plist/list/<specialid>/?json=true``，失败再试
  ``wwwapi.kugou.com/yy/index.php?r=playlist&type=1&specialid=...``；
- 搜索：``songsearch.kugou.com/song_search_v2``（**实测可用**），兜底 http 的
  ``mobilecdn.kugou.com``（它的 HTTPS 证书域名对不上，只能走 http）。
- 老 CDN 直链兜底：``trackercdn.kugou.com/i/v2/``，``key = md5(hash + "kgcloudv2")``。
  实测：免费曲目返回 ``status=1`` 才有 ``url``；``status=2`` 是 VIP/付费，``status=0`` 表示
  当前 hash/参数拿不到地址（需要登录态）。所以这条只做兜底，失败照常抛 Unavailable。
- **play/getdata 主链路仍未在真机验证**（没有可用的酷狗登录态）。
  ``play/getdata`` 不接受音质参数，实际能拿到什么码率由账号权限决定；
  本模块的 ``quality_levels`` 只用来给上层界面选文件命名/提示，不保证真能拿到对应码率。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid

from .base import BaseAdapter, NotLoggedIn, PlatformError, Track, Unavailable
from ..util import get_logger, make_session, safe_name

#: 播放地址接口
API = "https://wwwapi.kugou.com/yy/index.php"
#: 歌单接口（手机版，参数最少）
PLIST_API = "https://m.kugou.com/plist/list/{sid}/?json=true"
#: 搜索接口（网页版，实测可用）
SEARCH_API_V2 = "https://songsearch.kugou.com/song_search_v2"
#: 搜索接口（老手机版，换源兜底；HTTPS 证书域名不匹配，只能走 http）
SEARCH_API_HTTP = "http://mobilecdn.kugou.com/api/v3/search/song"
#: 老 CDN 直链接口：key = md5(hash + "kgcloudv2")
CDN_API = "http://trackercdn.kugou.com/i/v2/"
REFERER = "https://www.kugou.com/"

TIMEOUT_API = 20
TIMEOUT_DOWNLOAD = 25
RETRIES = 3

#: 歌单里的时长字段给的是秒，播放接口的 timelength 给的是毫秒
_NAME_SEP = re.compile(r"\s*-\s*")


# --------------------------------------------------------------------------
# 纯函数：解析 / 拼接（单测直接调这些，不联网）
# --------------------------------------------------------------------------
def parse_playlist_id(playlist: str) -> str:
    """从歌单 id / 链接里取出 specialid（数字，或收藏夹的短 hash）。"""
    text = (playlist or "").strip()
    if not text:
        raise PlatformError("歌单标识为空：请在设置里填酷狗歌单 id 或歌单链接")
    for pattern in (
        r"[?&](?:specialid|special_id|id)=([0-9A-Za-z_-]+)",
        r"/plist/list/([0-9A-Za-z_-]+)",
        r"/single/([0-9A-Za-z_-]+)",
        r"/share/playlist/([0-9A-Za-z_-]+)",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    if re.fullmatch(r"[0-9A-Za-z_-]{1,64}", text):
        return text
    raise PlatformError(f"看不懂歌单标识：{playlist}（请填酷狗歌单 id 或歌单链接）")


def new_mid() -> str:
    """生成一个酷狗要求格式的随机 mid（32 位十六进制），不借用任何人的账号。"""
    return uuid.uuid4().hex


def build_play_params(hash_: str, album_id: str = "0", mid: str = "",
                      timestamp: int | None = None) -> dict:
    """构造 ``r=play/getdata`` 的 query 参数（纯数据，方便单测）。"""
    return {
        "r": "play/getdata",
        "hash": str(hash_ or ""),
        "album_id": str(album_id or "0"),
        "dfid": "-",
        "mid": mid,
        "platid": "4",
        "_": str(int(time.time() * 1000) if timestamp is None else int(timestamp)),
    }


def _to_ms(value) -> int:
    """时长统一成毫秒；小于 10000 的当成秒。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if 0 < number < 10_000:
        return number * 1000
    return number


def _split_filename(filename: str) -> tuple[str, str]:
    """``"周杰伦 - 晴天"`` -> ``("周杰伦", "晴天")``；没有分隔符就只给歌名。"""
    text = (filename or "").strip()
    parts = _NAME_SEP.split(text, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].strip(), parts[1].strip()
    return "", text


def _artist_names(value) -> list[str]:
    """把一个歌手字段（字符串或列表）拆成歌手名列表。"""
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[、/;&]", value) if part.strip()]
    names: list[str] = []
    for item in value or []:
        if isinstance(item, dict):
            name = item.get("author_name") or item.get("singer_name") or item.get("name")
            if name:
                names.append(str(name))
        elif isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names


def _extract_items(data) -> list[dict]:
    """从各种形状的歌单响应里挖出歌曲列表。"""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    paths = (
        ("list", "info"),
        ("data", "info"),
        ("data", "songs"),
        ("data", "lists"),
        ("info",),
        ("songs",),
    )
    for path in paths:
        current = data
        for key in path:
            current = current.get(key) if isinstance(current, dict) else None
            if current is None:
                break
        if isinstance(current, list):
            return [item for item in current if isinstance(item, dict)]
    return []


def parse_kugou_playlist_json(data: dict, platform: str = "kugou") -> list[Track]:
    """把歌单响应解析成 ``Track`` 列表（id 存 hash，``extra`` 里带 album_id）。

    兼容 ``{"list": {"info": [...]}}``（m.kugou.com）与 ``{"data": {"info": [...]}}``
    （wwwapi playlist），字段名同时认大小写两套写法。
    """
    tracks: list[Track] = []
    for raw in _extract_items(data):
        info = raw.get("song") or raw.get("songInfo") or raw
        hash_ = info.get("hash") or info.get("FileHash") or info.get("filehash") or ""
        if not hash_:
            continue
        filename = info.get("filename") or info.get("FileName") or info.get("audio_name") or ""
        singer = (info.get("singername") or info.get("SingerName")
                  or info.get("author_name") or info.get("singer_name") or "")
        name = info.get("songname") or info.get("SongName") or ""
        if not name:
            file_singer, file_name = _split_filename(filename)
            name = file_name
            singer = singer or file_singer
        album_id = (info.get("album_id") or info.get("AlbumID") or info.get("albumid") or "0")
        tracks.append(Track(
            platform=platform,
            id=str(hash_),
            name=name or filename,
            artists=_artist_names(singer),
            album=info.get("album_name") or info.get("AlbumName") or "",
            duration_ms=_to_ms(info.get("duration") or info.get("Duration")
                               or info.get("timelength") or info.get("Timelength")),
            extra={"album_id": str(album_id)},
        ))
    return tracks


def parse_kugou_play_json(data: dict) -> dict:
    """解析 ``r=play/getdata``：拿到 play_url 就返回元信息，拿不到就抛 Unavailable。

    返回的 dict：``url`` / ``name`` / ``artists`` / ``album`` / ``duration_ms`` /
    ``hash`` / ``album_id``。
    """
    if not isinstance(data, dict):
        raise PlatformError("酷狗播放接口返回了非 JSON 对象")
    info = data.get("data") or {}
    url = (info.get("play_url") or "").strip()
    if not url:
        raise Unavailable("酷狗音乐没有返回播放地址（可能是 VIP 专属 / 无版权，或登录状态过期）")
    name = info.get("audio_name") or info.get("song_name") or ""
    artists = _artist_names(info.get("authors") or info.get("author_name") or "")
    album = info.get("album_name") or ""
    if not name:
        album_artist, song = _split_filename(info.get("filename") or "")
        name = song
        artists = artists or _artist_names(album_artist)
    return {
        "url": url,
        "name": name,
        "artists": artists,
        "album": album,
        "duration_ms": _to_ms(info.get("timelength") or info.get("duration")),
        "hash": str(info.get("hash") or ""),
        "album_id": str(info.get("album_id") or "0"),
    }


def parse_kugou_search_json(data: dict, limit: int = 5) -> list[Track]:
    """解析搜索响应（``mobilecdn`` 的 ``data.info``，换源时用）。"""
    if not isinstance(data, dict):
        return []
    info = data.get("data") or {}
    items = info.get("info") or info.get("lists") or []
    tracks: list[Track] = []
    for raw in items[:limit]:
        if not isinstance(raw, dict):
            continue
        hash_ = raw.get("hash") or raw.get("FileHash") or ""
        if not hash_:
            continue
        filename = raw.get("filename") or raw.get("FileName") or ""
        name = raw.get("songname") or raw.get("SongName") or ""
        singer = raw.get("singername") or raw.get("SingerName") or ""
        if not name:
            file_singer, file_name = _split_filename(filename)
            name = file_name
            singer = singer or file_singer
        tracks.append(Track(
            platform="kugou",
            id=str(hash_),
            name=name or filename,
            artists=_artist_names(singer),
            album=raw.get("album_name") or raw.get("AlbumName") or "",
            duration_ms=_to_ms(raw.get("duration") or raw.get("Duration")),
            extra={"album_id": str(raw.get("album_id") or raw.get("AlbumID") or "0")},
        ))
    return tracks


# --------------------------------------------------------------------------
# 适配器
# --------------------------------------------------------------------------
class KugouAdapter(BaseAdapter):
    """酷狗音乐：cookie 登录 -> 拉歌单 -> play/getdata 拿地址下载。"""

    key = "kugou"
    title = "酷狗音乐"
    supports_download = True
    #: 酷狗播放接口不区分音质，实际码率由账号权限决定；这里只是给界面选档位用
    quality_levels = ("320", "128")

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._session = None
        self._jar: dict[str, str] | None = None

    # ------------------------- 登录 -------------------------
    def cookies(self) -> dict[str, str]:
        if self._jar is not None:
            return self._jar
        raw = ((self.config or {}).get("cookie") or "").strip()
        jar: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                name, _, value = part.partition("=")
                name = name.strip()
                if name:
                    jar[name] = value.strip()
        self._jar = jar
        return jar

    @property
    def mid(self) -> str:
        """设备 mid：优先用 cookie 里的 kg_mid，没有就随机造一个（不借用别人的账号）。"""
        jar = self.cookies()
        value = (jar.get("kg_mid") or "").strip()
        if not value:
            value = new_mid()
            jar["kg_mid"] = value
        return value

    def ensure_login(self) -> None:
        """只做本地校验（不发网络请求）：没 cookie 就提示去设置里填。"""
        jar = self.cookies()
        if not jar:
            raise NotLoggedIn("酷狗音乐还没登录：先用浏览器打开 kugou.com 登录一次，再在设置里点「自动获取」")

    @property
    def session(self):
        if self._session is None:
            raw = ((self.config or {}).get("cookie") or "").strip()
            self._session = make_session(raw, REFERER)
            self._session.cookies.set("kg_mid", self.mid)
        return self._session

    # ------------------------- 请求 -------------------------
    def _request_json(self, url: str, *, params: dict | None = None,
                      timeout: int = TIMEOUT_API) -> dict:
        """GET 一段 JSON，失败重试 ``RETRIES`` 次，最终统一抛 PlatformError。"""
        last_error: Exception | None = None
        for attempt in range(RETRIES):
            try:
                resp = self.session.get(url, params=params, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise PlatformError(f"接口返回的不是 JSON 对象：{url}")
                return data
            except Exception as exc:                     # noqa: BLE001 - 网络抖动统一重试
                last_error = exc
                get_logger().debug("酷狗请求失败（第 %d/%d 次）：%s", attempt + 1, RETRIES, exc)
                if attempt + 1 < RETRIES:
                    time.sleep(0.6 * (attempt + 1))
        raise PlatformError(f"酷狗接口请求失败：{last_error}")

    # ------------------------- 歌单 -------------------------
    def list_tracks(self, playlist: str) -> list[Track]:
        """拉歌单：先手机版 plist，再试 wwwapi playlist。"""
        self.ensure_login()
        sid = parse_playlist_id(playlist)
        tracks: list[Track] = []

        try:
            tracks = parse_kugou_playlist_json(
                self._request_json(PLIST_API.format(sid=sid)))
        except PlatformError as exc:
            get_logger().debug("酷狗 plist 接口失败，改用 wwwapi：%s", exc)

        if not tracks:
            params = {"r": "playlist", "type": "1", "specialid": sid,
                      "page": "1", "mid": self.mid, "_": str(int(time.time() * 1000))}
            tracks = parse_kugou_playlist_json(self._request_json(API, params=params))

        if not tracks:
            raise PlatformError("酷狗歌单拉取失败：cookie 可能过期，或歌单不公开")
        return tracks

    # ------------------------- 下载 -------------------------
    def download(self, track: Track, dest_dir: str, quality: str = "320") -> str:
        """用 play/getdata 返回的 play_url 下载。

        ``quality`` 只影响提示：酷狗接口不区分音质，码率由账号权限决定。
        文件名按项目约定统一用 ``safe_name(str(track)) + ".mp3"``（sync 用它判断是否已下载）。
        """
        self.ensure_login()
        album_id = str((track.extra or {}).get("album_id") or "0")
        url = ""
        try:
            params = build_play_params(track.id, album_id, self.mid)
            info = parse_kugou_play_json(self._request_json(API, params=params))
            url = info["url"]
        except (PlatformError, Unavailable) as exc:
            get_logger().debug("酷狗 play/getdata 失败，改用 trackercdn 兜底：%s", exc)
            url = self._cdn_url(track.id)
        if not url:
            raise Unavailable("酷狗音乐没有返回播放地址（可能是 VIP 专属 / 无版权，或登录状态失效）")

        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, safe_name(str(track)) + ".mp3")
        last_error: Exception | None = None
        for attempt in range(RETRIES):
            try:
                with self.session.get(url, stream=True, timeout=TIMEOUT_DOWNLOAD) as resp:
                    resp.raise_for_status()
                    return self._write(resp.iter_content(256 * 1024), path)
            except Exception as exc:                     # noqa: BLE001 - 下载中断重试
                last_error = exc
                get_logger().debug("酷狗下载失败（第 %d/%d 次）：%s", attempt + 1, RETRIES, exc)
                if attempt + 1 < RETRIES:
                    time.sleep(1.0 * (attempt + 1))
        raise PlatformError(f"酷狗下载失败：{last_error}")

    def _cdn_url(self, hash_: str) -> str:
        """老 CDN 直链兜底：免费曲目能拿到，VIP/付费拿不到（返回空串）。"""
        h = (hash_ or "").lower()
        if not h:
            return ""
        key = hashlib.md5((h + "kgcloudv2").encode()).hexdigest()
        for br in ("320", "hq", "flac", "128"):
            try:
                data = self._request_json(CDN_API, params={
                    "key": key, "hash": h, "br": br, "appid": "1005",
                    "pid": 2, "cmd": "25", "behavior": "play"})
            except PlatformError:
                continue
            if data.get("status") == 1:
                urls = data.get("url") or []
                if urls:
                    return urls[0]
        return ""

    # ------------------------- 搜索（换源用） -------------------------
    def search(self, keyword: str, limit: int = 5) -> list[Track]:
        """按关键词搜歌，``sync`` 换源时调用。

        先走 songsearch（网页版搜索，实测可用），失败再退到 mobilecdn
        （注意 mobilecdn 的 HTTPS 证书域名不匹配，只能用 http）。
        """
        size = str(max(1, int(limit)))
        v2_params = {
            "keyword": keyword, "page": "1", "pagesize": size,
            "platform": "WebFilter", "userid": "-1", "clientver": "2000",
            "iscorrection": 1, "privilege_filter": 0, "filter": 10,
            "token": "", "appid": "1014",
        }
        try:
            tracks = parse_kugou_search_json(
                self._request_json(SEARCH_API_V2, params=v2_params), limit=limit)
            if tracks:
                return tracks
        except PlatformError as exc:
            get_logger().debug("酷狗 songsearch 失败，改用 mobilecdn：%s", exc)

        params = {"format": "json", "keyword": keyword, "page": "1",
                  "pagesize": size, "showtype": "1"}
        return parse_kugou_search_json(self._request_json(SEARCH_API_HTTP, params=params), limit=limit)
