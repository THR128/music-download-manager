# -*- coding: utf-8 -*-
"""QQ 音乐适配器（登录态只认配置里的 cookie，不碰本地客户端）。

接口来源与可信度（重要，别当成"已验证"）：
- 按 y.qq.com 网页版现行的 ``u.y.qq.com/cgi-bin/musicu.fcg``（``music.vkey.GetVkey``）
  与 ``c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg``（歌单）实现；
  **这是按 2024 年前后的接口格式写的，未在真机验证**——本仓库没有可用的 QQ 音乐登录态。
- 拿直链、解析歌单、解析搜索结果都抽成了纯函数（``parse_playlist_json`` /
  ``parse_vkey_json`` / ``parse_search_json`` 等），接口一变只需要改这里，并且有单测兜底。
- cookie 只从配置 ``platforms.qqmusic.cookie`` 读取（浏览器登录 y.qq.com 后整段复制），
  里面至少要有 ``uin`` 与 ``qm_keyst`` / ``qqmusic_key`` 之一；``check_login`` 只做本地校验，
  不发网络请求，所以没有 cookie 时能立刻给出"去设置里填 cookie"的提示。
"""
from __future__ import annotations

import json
import os
import random
import re
import time

from .base import BaseAdapter, NotLoggedIn, PlatformError, Track, Unavailable
from ..util import get_logger, make_session, safe_name

#: 主接口（musicu.fcg，POST 一段 JSON）
API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
#: 歌单接口（老 fcg 接口，参数简单、返回完整歌曲信息）
PLAYLIST_API = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
#: 搜索接口（网页版搜索，换源时用）
SEARCH_API = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
REFERER = "https://y.qq.com/"

#: 接口超时（秒）；所有请求都必须带上超时
TIMEOUT_API = 20
TIMEOUT_DOWNLOAD = 25
#: 每个请求最多试几次（网络抖动就重试）
RETRIES = 3

#: 音质档位 -> (文件名前缀, 后缀)。M800=320k mp3，M500=128k mp3，F000=flac
QUALITY = {
    "flac": ("F000", ".flac"),
    "320": ("M800", ".mp3"),
    "192": ("M500", ".mp3"),
    "128": ("M500", ".mp3"),
}
DEFAULT_QUALITY = "320"

_LOGIN_KEYS = ("uin", "qqmusic_key", "qm_keyst")


# --------------------------------------------------------------------------
# 纯函数：解析 / 拼接（单测直接调这些，不联网）
# --------------------------------------------------------------------------
def parse_playlist_id(playlist: str) -> str:
    """从歌单 id / 链接里取出 disstid（纯数字）。"""
    text = (playlist or "").strip()
    if not text:
        raise PlatformError("歌单标识为空：请在设置里填 QQ 音乐歌单 id 或歌单链接")
    for pattern in (r"[?&](?:disstid|id)=(\d+)", r"/playlist/(\d+)", r"/taoge/detail/(\d+)"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    if text.isdigit():
        return text
    raise PlatformError(f"看不懂歌单标识：{playlist}（请填 QQ 音乐歌单 id 或歌单链接）")


def quality_to_file(quality: str) -> tuple[str, str]:
    """音质档位 -> (文件名前缀, 扩展名)；不认识的档位按 320k 处理。"""
    return QUALITY.get((quality or "").strip().lower(), QUALITY[DEFAULT_QUALITY])


def quality_chain(quality: str) -> list[str]:
    """按音质档位给出尝试顺序（高 -> 低）。

    flac 是无损会员档，只试 F000：没权限就老老实实抛 Unavailable，
    不偷偷降级成有损（用户要无损就该知道拿不到）。
    """
    key = (quality or "").strip().lower()
    if key == "flac":
        return ["flac"]
    if key == "320":
        return ["320", "128"]
    if key == "192":
        return ["192", "128"]
    return ["128"]


def build_vkey_filename(songmid: str, media_mid: str | None, quality: str) -> str:
    """拼 GetVkey 的 filename 参数：``前缀 + media_mid + 后缀``。"""
    prefix, ext = quality_to_file(quality)
    mid = (media_mid or songmid or "").strip()
    return f"{prefix}{mid}{ext}"


def build_vkey_payload(songmid: str, filename: str, guid: str, uin: str = "0") -> dict:
    """构造 ``music.vkey.GetVkey`` 的 musicu.fcg 请求体（纯数据，方便单测）。"""
    return {
        "comm": {"uin": uin, "format": "json", "ct": 24, "cv": 0},
        "req_0": {
            "module": "music.vkey.GetVkey",
            "method": "UrlGetVkey",
            "param": {
                "guid": guid,
                "songmid": [songmid],
                "songtype": [0],
                "uin": uin,
                "loginflag": 1,
                "platform": "20",
                "filename": [filename],
            },
        },
    }


def parse_vkey_json(data: dict) -> str:
    """把 GetVkey 的响应拼成可以直接 GET 的直链。

    ``sip`` 是 CDN 前缀列表，``purl`` 是相对路径，拼起来才是完整 URL。
    没有 purl 说明没版权 / 需要会员 / cookie 失效，统一抛 ``Unavailable``。
    """
    req = (data or {}).get("req_0") or {}
    info = req.get("data") or {}
    purl = ""
    for item in info.get("midurlinfo") or []:
        purl = (item.get("purl") or "").strip()
        if purl:
            break
    if not purl:
        raise Unavailable("QQ 音乐没有返回音频地址（无版权 / 会员专属 / 登录状态过期）")
    if purl.startswith(("http://", "https://")):
        return purl
    sip = info.get("sip") or []
    host = (sip[0] if sip else "https://isure.stream.qqmusic.qq.com/").strip()
    return f"{host.rstrip('/')}/{purl.lstrip('/')}"


def _to_ms(value) -> int:
    """时长统一成毫秒；小于 10000 的当成秒（歌单接口给的是秒）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if 0 < number < 10_000:
        return number * 1000
    return number


def _artist_names(value) -> list[str]:
    """从 singer 列表里取名字，兼容 ``[{"name": ...}]`` 与字符串。"""
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[、/;&]", value) if part.strip()]
    names: list[str] = []
    for item in value or []:
        if isinstance(item, dict):
            name = item.get("name") or item.get("title")
            if name:
                names.append(name)
        elif isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names


def _extract_songs(data) -> list[dict]:
    """从各种形状的歌单响应里挖出歌曲列表。"""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    paths = (
        ("cdlist", 0, "songlist"),
        ("cdlist", 0, "song_list"),
        ("req_0", "data", "dirinfo", "songlist"),
        ("req_0", "data", "dirinfo", "songs"),
        ("req_0", "data", "songlist"),
        ("req_0", "data", "songs"),
        ("data", "songlist"),
        ("data", "songs"),
        ("songlist",),
        ("songs",),
    )
    for path in paths:
        current = data
        for key in path:
            if isinstance(key, int):
                current = current[key] if isinstance(current, list) and len(current) > key else None
            elif isinstance(current, dict):
                current = current.get(key)
            else:
                current = None
            if current is None:
                break
        if isinstance(current, list):
            return current
    return []


def parse_playlist_json(data: dict, platform: str = "qqmusic") -> list[Track]:
    """把歌单响应解析成 ``Track`` 列表。

    支持两种形状：``fcg_ucc_getcdinfo_byids_cp`` 的 ``cdlist[0].songlist``，
    以及 musicu.fcg ``music.srfDissInfo.aiDissInfo`` 的 ``req_0.data.dirinfo.songlist``；
    也兼容 list 里面再套一层 ``songInfo`` 的写法。
    """
    tracks: list[Track] = []
    for raw in _extract_songs(data):
        if not isinstance(raw, dict):
            continue
        info = raw.get("songInfo") or raw.get("song_info") or raw
        mid = info.get("songmid") or info.get("mid") or info.get("songMid") or ""
        if not mid:
            continue
        album = info.get("album") or {}
        media_mid = (info.get("media_mid") or (info.get("file") or {}).get("media_mid")
                     or (info.get("album") or {}).get("mid") or mid)
        tracks.append(Track(
            platform=platform,
            id=str(mid),
            name=info.get("songname") or info.get("name") or info.get("title") or "",
            artists=_artist_names(info.get("singer") or info.get("singers")),
            album=(info.get("albumname") or album.get("name") or info.get("album_name") or ""),
            duration_ms=_to_ms(info.get("interval") or info.get("duration")),
            extra={"media_mid": str(media_mid)},
        ))
    return tracks


def parse_song_detail_json(data: dict) -> dict:
    """解析 ``music.pf_song_detail_svr.get_song_detail``，用来补歌名 / 歌手。"""
    info = ((data or {}).get("req_0") or {}).get("data") or {}
    track_info = info.get("track_info") or info.get("songinfo") or info
    album = track_info.get("album") or {}
    return {
        "name": track_info.get("name") or track_info.get("title") or "",
        "artists": _artist_names(track_info.get("singer")),
        "album": album.get("name") or "",
        "duration_ms": _to_ms(track_info.get("interval")),
    }


def parse_search_json(data: dict, limit: int = 5) -> list[Track]:
    """解析搜索结果（兼容 ``client_search_cp`` 与 musicu 搜索两种形状）。"""
    body = ((data or {}).get("req_0") or {}).get("data") or {}
    song = ((body.get("body") or {}).get("song") or body.get("song") or {})
    if not song:
        song = ((data or {}).get("data") or {}).get("song") or {}
    items = song.get("list") or []
    tracks: list[Track] = []
    for raw in items[:limit]:
        info = raw.get("songInfo") or raw
        mid = info.get("songmid") or info.get("mid") or ""
        if not mid:
            continue
        album = info.get("album") or {}
        tracks.append(Track(
            platform="qqmusic",
            id=str(mid),
            name=info.get("songname") or info.get("name") or "",
            artists=_artist_names(info.get("singer")),
            album=album.get("name") or info.get("albumname") or "",
            duration_ms=_to_ms(info.get("interval") or info.get("duration")),
            extra={"media_mid": str(info.get("media_mid") or (info.get("file") or {}).get("media_mid") or mid)},
        ))
    return tracks


# --------------------------------------------------------------------------
# 适配器
# --------------------------------------------------------------------------
class QQMusicAdapter(BaseAdapter):
    """QQ 音乐：cookie 登录 -> 拉歌单 -> vkey 拿直链下载。"""

    key = "qqmusic"
    title = "QQ音乐"
    supports_download = True
    quality_levels = ("flac", "320", "192", "128")

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self._session = None
        self._jar: dict[str, str] | None = None
        self._guid = str(random.randint(10 ** 9, 10 ** 10 - 1))

    # ------------------------- 登录 -------------------------
    def cookies(self) -> dict[str, str]:
        """配置里的 cookie 字符串 -> dict。"""
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

    def ensure_login(self) -> None:
        """只做本地校验（不发网络请求），没 cookie 时给出明确提示。"""
        jar = self.cookies()
        if not jar:
            raise NotLoggedIn("QQ音乐还没登录：先用浏览器打开 y.qq.com 登录一次，再在设置里点「自动获取」")
        if not any(jar.get(name) for name in _LOGIN_KEYS):
            raise NotLoggedIn("QQ音乐的登录信息不完整：请用浏览器重新登录 y.qq.com，再点「自动获取」")

    def _uin(self) -> str:
        """从 cookie 里取纯数字 uin，取不到就当游客 0。"""
        raw = self.cookies().get("uin") or ""
        digits = re.sub(r"\D", "", raw)
        return digits or "0"

    @property
    def session(self):
        if self._session is None:
            raw = ((self.config or {}).get("cookie") or "").strip()
            self._session = make_session(raw, REFERER)
            self._session.headers["Origin"] = "https://y.qq.com"
        return self._session

    # ------------------------- 请求 -------------------------
    def _request_json(self, url: str, *, params: dict | None = None,
                      json_body: dict | None = None, timeout: int = TIMEOUT_API) -> dict:
        """GET / POST 一段 JSON，失败重试 ``RETRIES`` 次，最终统一抛 PlatformError。"""
        last_error: Exception | None = None
        for attempt in range(RETRIES):
            try:
                if json_body is not None:
                    resp = self.session.post(
                        url,
                        data=json.dumps(json_body, ensure_ascii=False),
                        headers={"Content-Type": "application/json"},
                        timeout=timeout,
                    )
                else:
                    resp = self.session.get(url, params=params, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise PlatformError(f"接口返回的不是 JSON 对象：{url}")
                return data
            except Exception as exc:                     # noqa: BLE001 - 网络抖动统一重试
                last_error = exc
                get_logger().debug("QQ音乐请求失败（第 %d/%d 次）：%s", attempt + 1, RETRIES, exc)
                if attempt + 1 < RETRIES:
                    time.sleep(0.6 * (attempt + 1))
        raise PlatformError(f"QQ音乐接口请求失败：{last_error}")

    # ------------------------- 歌单 -------------------------
    def find_default_playlist(self) -> tuple[str, str]:
        """QQ音乐的「我喜欢」是每个账号固定的 disstid=201，先验证能拉到再认。"""
        self.ensure_login()
        candidates = ("201", "202")
        last_error = ""
        for pid in candidates:
            try:
                tracks = self._list_by_id(pid)
            except PlatformError as exc:
                last_error = str(exc)
                continue
            if tracks:
                name = "我喜欢" if pid == "201" else "默认收藏"
                return pid, name
        raise PlatformError(
            "没法自动识别 QQ 音乐的「我喜欢」"
            + (f"：{last_error}" if last_error else "")
            + "。请在设置里填歌单 id 或歌单链接")

    def list_tracks(self, playlist: str) -> list[Track]:
        """拉歌单：先走 fcg 歌单接口，失败再走 musicu 的 dissinfo，最后按 songmid 补歌名。"""
        self.ensure_login()
        pid, name = self.resolve_playlist(playlist)
        pid = parse_playlist_id(pid)
        self.last_playlist_name = name or self.last_playlist_name
        tracks = self._list_by_id(pid)
        self._fill_songinfo(tracks)
        return tracks

    def _list_by_id(self, pid: str) -> list[Track]:
        """按歌单 id 拉曲目（不带补歌名，find_default_playlist 复用）。"""
        tracks: list[Track] = []

        params = {
            "format": "json", "inCharset": "utf8", "outCharset": "utf-8",
            "notice": "0", "platform": "yqq.json", "needNewCode": "0",
            "type": "1", "json": "1", "utf8": "1", "onlysong": "0",
            "disstid": pid, "g_tk": "5381", "loginUin": self._uin(),
            "hostUin": "0", "uin": self._uin(),
        }
        try:
            tracks = parse_playlist_json(self._request_json(PLAYLIST_API, params=params))
        except PlatformError as exc:
            get_logger().debug("QQ音乐歌单接口失败，改用 musicu：%s", exc)

        if not tracks:
            payload = {
                "comm": {"ct": 20, "cv": 1859, "uin": self._uin()},
                "req_0": {
                    "module": "music.srfDissInfo.aiDissInfo",
                    "method": "uniform_get_Dissinfo",
                    "param": {
                        "disstid": int(pid) if pid.isdigit() else pid,
                        "enc_host_uin": "", "tag": 1, "userinfo": 1,
                        "song_begin": 0, "song_num": 1000, "order": 1,
                    },
                },
            }
            tracks = parse_playlist_json(self._request_json(API, json_body=payload))

        if not tracks:
            raise PlatformError("QQ音乐歌单拉取失败：cookie 可能过期，或歌单不是公开歌单")
        return tracks

    def _fill_songinfo(self, tracks: list[Track]) -> None:
        """歌单接口只给 songmid 时，用 songinfo 接口补 name / singer（最多补 30 首，别刷接口）。"""
        filled = 0
        for track in tracks:
            if filled >= 30:
                break
            if track.name and track.artists:
                continue
            payload = {
                "comm": {"ct": 24, "cv": 0, "uin": self._uin()},
                "req_0": {
                    "module": "music.pf_song_detail_svr",
                    "method": "get_song_detail",
                    "param": {"song_mid": track.id},
                },
            }
            try:
                detail = parse_song_detail_json(self._request_json(API, json_body=payload))
            except PlatformError as exc:
                get_logger().debug("QQ音乐补歌名失败 %s：%s", track.id, exc)
                continue
            track.name = track.name or detail["name"]
            if not track.artists:
                track.artists = detail["artists"]
            track.album = track.album or detail["album"]
            track.duration_ms = track.duration_ms or detail["duration_ms"]
            filled += 1

    # ------------------------- 下载 -------------------------
    def download(self, track: Track, dest_dir: str, quality: str = "320") -> str:
        """拿 vkey 直链并落盘。

        文件名按项目约定 ``safe_name(str(track)) + ".mp3"``（sync 用这个路径判断是否已下载，
        所以无损档也统一存成 .mp3 后缀，实际内容由 vkey 返回的格式决定）。
        """
        self.ensure_login()
        media_mid = str((track.extra or {}).get("media_mid") or track.id)
        url = ""
        last_error: Exception | None = None
        unavailable = False
        for level in quality_chain(quality):
            filename = build_vkey_filename(track.id, media_mid, level)
            payload = build_vkey_payload(track.id, filename, self._guid, self._uin())
            try:
                url = parse_vkey_json(self._request_json(API, json_body=payload))
                break
            except Unavailable as exc:
                unavailable = True
                last_error = exc
            except PlatformError as exc:
                last_error = exc

        if not url:
            if unavailable or last_error is None:
                raise Unavailable(f"{track.name} 在 QQ音乐拿不到音源（无版权 / 会员专属）：{last_error}")
            raise PlatformError(f"QQ音乐取直链失败：{last_error}")

        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, safe_name(str(track)) + ".mp3")
        last_error = None
        for attempt in range(RETRIES):
            try:
                with self.session.get(url, stream=True, timeout=TIMEOUT_DOWNLOAD) as resp:
                    resp.raise_for_status()
                    return self._write(resp.iter_content(256 * 1024), path)
            except Exception as exc:                     # noqa: BLE001 - 下载中断重试
                last_error = exc
                get_logger().debug("QQ音乐下载失败（第 %d/%d 次）：%s", attempt + 1, RETRIES, exc)
                if attempt + 1 < RETRIES:
                    time.sleep(1.0 * (attempt + 1))
        raise PlatformError(f"QQ音乐下载失败：{last_error}")

    # ------------------------- 搜索（换源用） -------------------------
    def search(self, keyword: str, limit: int = 5) -> list[Track]:
        """按关键词搜歌，换源时由 ``sync`` 调用。"""
        params = {
            "format": "json", "inCharset": "utf8", "outCharset": "utf-8",
            "notice": "0", "platform": "yqq.json", "needNewCode": "0",
            "ct": "24", "qqmusic_ver": "1298", "new_json": "1", "remoteplace": "txt.yqq.song",
            "t": "0", "aggr": "1", "cr": "1", "catZhida": "1", "lossless": "0",
            "flag_qc": "0", "p": "1", "n": str(max(1, int(limit))),
            "w": keyword, "g_tk": "5381", "loginUin": self._uin(), "hostUin": "0",
        }
        return parse_search_json(self._request_json(SEARCH_API, params=params), limit=limit)
