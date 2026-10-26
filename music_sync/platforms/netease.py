# -*- coding: utf-8 -*-
"""网易云音乐适配器。

两条路取登录状态：
1. 自动：从网易云客户端的内置浏览器数据里解出来（客户端保持登录即可，用户零操作）；
2. 手动：配置里填 ``platforms.netease.cookie`` 字符串，覆盖自动结果。

自动解密原理随平台不同：

* **Windows**：客户端 ``LocalPrefs.json`` 的 ``os_crypt.encrypted_key`` 经 DPAPI 解出
  32 字节 AES 主密钥，再用 AES-GCM 解数据库里 ``v10`` 前缀的密文；
* **macOS**：客户端是内置 CEF，数据目录换成 ``~/Library/Application Support/com.netease.163music``，
  加密方式与 macOS 版 Chromium 一致（AES-128-CBC + 钥匙串密码），
  但很多 CEF 应用根本不加密——所以顺序是「先试钥匙串，再当明文读」，
  两条路都不通就返回空表，由上层给出"请在客户端里重新登录"的人话提示。

所有平台相关的路径都从 :mod:`music_sync.ospaths` 拿，本模块不自己拼平台路径。
"""
from __future__ import annotations

import base64
import glob
import json
import os
import random
import shutil
import sqlite3
import string
import tempfile
import time

from .base import BaseAdapter, NotLoggedIn, PlatformError, Track, Unavailable
from .. import ospaths
from ..util import get_logger, make_session

API = "https://music.163.com"
LEVEL = {"lossless": "lossless", "320": "exhigh", "192": "higher", "128": "standard"}

# macOS 上网易云客户端可能的钥匙串服务名，按可能性从高到低试
_MAC_SAFE_STORAGE_SERVICES = (
    "NeteaseMusic Safe Storage",
    "NetEase Music Safe Storage",
    "网易云音乐 Safe Storage",
    "Chrome Safe Storage",
)


def _dpapi_unprotect(data: bytes) -> bytes:
    # 只 import 在函数里：ctypes.wintypes 在 macOS/Linux 上根本 import 不了
    import ctypes
    import ctypes.wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise PlatformError("CryptUnprotectData 失败（换个用户跑？）")
    return ctypes.string_at(blob_out.pbData, blob_out.cbData)


def find_client_dir() -> str:
    """找到网易云客户端内置浏览器的数据目录。

    * Windows：``%LOCALAPPDATA%\\NetEase\\CloudMusic\\webapp*``（挑最新的那个）；
    * macOS：``com.netease.163music`` 的两处容器目录，只认里面真有数据库的那个；
    * 找不到返回空串，让调用方走"请手动指定"的降级分支。
    """
    for path in ospaths.netease_client_dirs():
        if not os.path.isdir(path):
            continue
        if "webapp" in os.path.basename(path).lower():
            return path
        # macOS 的候选里有 .app 本体，它没有数据库，跳过
        if not ospaths.IS_MAC or _cookie_db_in(path):
            return path
    return ""


def _cookie_db_in(client_dir: str) -> str:
    """在客户端数据目录里找登录状态数据库，找不到返回空串。"""
    for rel in ("Cookies", os.path.join("Network", "Cookies")):
        path = os.path.join(client_dir, rel)
        if os.path.isfile(path):
            return path
    try:
        for path in glob.glob(os.path.join(client_dir, "**", "Cookies"), recursive=True):
            if os.path.isfile(path):
                return path
    except OSError:
        pass
    return ""


def _open_cookie_db(src: str) -> sqlite3.Connection:
    """复制一份再打开（客户端正在跑的时候原库可能被锁）。"""
    tmp_dir = os.path.join(tempfile.gettempdir(), "music-sync-cookies")
    try:
        os.makedirs(tmp_dir, exist_ok=True)
        tmp = os.path.join(tmp_dir, "Cookies")
        shutil.copy2(src, tmp)
        return sqlite3.connect(tmp)
    except Exception:
        return sqlite3.connect(f"file:{src}?mode=ro", uri=True)


def decrypt_client_cookies(client_dir: str) -> dict[str, str]:
    """从客户端的数据目录里取出 music.163.com 的登录状态（公开 API，签名不变）。"""
    if ospaths.IS_MAC:
        return _decrypt_client_mac(client_dir)
    if not ospaths.IS_WIN:
        return _decrypt_client_plain(client_dir)
    return _decrypt_client_windows(client_dir)


def _decrypt_client_windows(client_dir: str) -> dict[str, str]:
    """Windows：LocalPrefs.json + DPAPI 主密钥 + AES-GCM。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    prefs_path = os.path.join(client_dir, "LocalPrefs.json")
    if not os.path.isfile(prefs_path):
        raise PlatformError(f"找不到 {prefs_path}（客户端是否登录过？）")
    with open(prefs_path, encoding="utf-8") as fh:
        prefs = json.load(fh)
    key = base64.b64decode(prefs["os_crypt"]["encrypted_key"])
    master = _dpapi_unprotect(key[5:] if key[:5] == b"DPAPI" else key)

    src = os.path.join(client_dir, "Cookies")
    if not os.path.isfile(src):
        raise PlatformError(f"找不到 {src}")
    con = _open_cookie_db(src)
    jar: dict[str, str] = {}
    try:
        rows = con.execute("SELECT host_key,name,encrypted_value,value FROM cookies")
        for host, name, enc, plain in rows:
            if "music.163.com" not in (host or ""):
                continue
            value = plain
            if not value and enc and enc[:3] in (b"v10", b"v11"):
                try:
                    value = AESGCM(master).decrypt(enc[3:15], enc[15:], None).decode()
                except Exception:
                    continue
            elif not value and enc:
                value = enc.decode("utf-8", "ignore")
            if value:
                jar[name] = value
    finally:
        con.close()
        _cleanup_temp_db()
    return jar


def _decrypt_client_mac(client_dir: str) -> dict[str, str]:
    """macOS：先试钥匙串（CEF 里可能复用 Chromium 的加密），再当明文读。"""
    src = _cookie_db_in(client_dir)
    if not src:
        return {}                       # 没数据库 = 还没登录过，交给上层提示

    key = None
    try:
        from ..browser_cookies import _mac_key
        for service in _MAC_SAFE_STORAGE_SERVICES:
            key = _mac_key(service)
            if key:
                break
    except Exception as exc:            # noqa: BLE001 - 钥匙串任何问题都只是读不到
        get_logger().debug("macOS 客户端取钥匙串失败（当明文读）: %s", exc)
        key = None

    con = _open_cookie_db(src)
    jar: dict[str, str] = {}
    try:
        rows = con.execute("SELECT host_key,name,encrypted_value,value FROM cookies")
        for host, name, enc, plain in rows:
            if "music.163.com" not in (host or ""):
                continue
            value = plain or ""
            if not value and enc:
                value = _decrypt_mac_value(enc, key)
            if value:
                jar[name] = value
    except Exception:
        jar = {}
    finally:
        con.close()
        _cleanup_temp_db()
    return jar


def _decrypt_mac_value(enc: bytes, key: bytes | None) -> str:
    """单个密文字段：先按 Chromium(CBC) 解，失败就按明文读。"""
    from ..browser_cookies import _decrypt_mac

    value = _decrypt_mac(enc, key)
    if value:
        return value
    try:
        text = enc.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return text if text.isprintable() else ""


def _decrypt_client_plain(client_dir: str) -> dict[str, str]:
    """非 Windows/macOS（例如 Linux）：只读明文，读不到就给空表。"""
    src = _cookie_db_in(client_dir)
    if not src:
        return {}
    con = _open_cookie_db(src)
    jar: dict[str, str] = {}
    try:
        rows = con.execute("SELECT host_key,name,encrypted_value,value FROM cookies")
        for host, name, enc, plain in rows:
            if "music.163.com" not in (host or ""):
                continue
            value = plain or (enc or b"").decode("utf-8", "ignore")
            if value:
                jar[name] = value
    except Exception:
        jar = {}
    finally:
        con.close()
        _cleanup_temp_db()
    return jar


def _cleanup_temp_db() -> None:
    tmp = os.path.join(tempfile.gettempdir(), "music-sync-cookies", "Cookies")
    try:
        os.remove(tmp)
    except OSError:
        pass


class NeteaseAdapter(BaseAdapter):
    key = "netease"
    title = "网易云音乐"
    quality_levels = ("lossless", "320", "192", "128")

    def __init__(self, config=None):
        super().__init__(config)
        self._jar: dict[str, str] = {}
        self._session = None

    # ---------------- 登录 ----------------
    def cookies(self) -> dict[str, str]:
        if self._jar:
            return self._jar
        manual = ((self.config or {}).get("cookie") or "").strip()
        if manual:
            jar = {}
            for part in manual.split(";"):
                if "=" in part:
                    k, _, v = part.partition("=")
                    jar[k.strip()] = v.strip()
            self._jar = jar
            return jar
        client_dir = ((self.config or {}).get("client_app_dir") or "").strip() or find_client_dir()
        if not client_dir:
            raise NotLoggedIn("没找到网易云客户端数据目录，请先安装并登录客户端，或在设置里手填 cookie")
        self._jar = decrypt_client_cookies(client_dir)
        return self._jar

    def ensure_login(self) -> None:
        jar = self.cookies()
        if "MUSIC_U" not in jar:
            raise NotLoggedIn("cookie 里没有 MUSIC_U，请在网易云客户端重新登录一次")
        self.session.get(f"{API}/api/nuser/account/get", timeout=15)

    @property
    def session(self):
        if self._session is None:
            jar = self.cookies()
            self._session = make_session("", "https://music.163.com/")
            self._session.headers["User-Agent"] = "Mozilla/5.0 NeteaseMusicDesktop/3.1.41"
            for k, v in jar.items():
                self._session.cookies.set(k, v, domain=".music.163.com")
        return self._session

    # ---------------- 歌单 ----------------
    def uid(self) -> str:
        """当前登录账号的 uid（拿不到返回空串）。"""
        try:
            data = self.session.get(f"{API}/api/nuser/account/get", timeout=15).json() or {}
        except Exception:
            return ""
        profile = data.get("profile") or data.get("account") or {}
        return str(profile.get("userId") or profile.get("id") or "")

    def find_default_playlist(self) -> tuple[str, str]:
        """在「我的歌单」里找默认收藏：优先 specialType == 5，退化按名字匹配。"""
        self.ensure_login()
        uid = self.uid()
        if not uid:
            raise PlatformError("拿不到网易云账号信息，没法自动识别「我喜欢」，请在设置里填歌单 id")
        try:
            data = self.session.get(
                f"{API}/api/user/playlist",
                params={"uid": uid, "limit": 1000, "offset": 0}, timeout=25).json() or {}
        except Exception as exc:
            raise PlatformError(f"拉取「我的歌单」失败：{exc}") from exc
        items = data.get("playlist") or []
        for item in items:                     # 网易云的「我喜欢」就是 specialType=5
            if item.get("specialType") == 5:
                return str(item.get("id")), item.get("name") or "我喜欢的音乐"
        for item in items:
            name = (item.get("name") or "")
            if any(alias in name for alias in self.LIKED_ALIASES):
                return str(item.get("id")), name
        raise PlatformError("在你的网易云歌单里没找到「我喜欢」，请在设置里手动填歌单 id")

    def list_tracks(self, playlist: str) -> list[Track]:
        self.ensure_login()
        pid, name = self.resolve_playlist(playlist)
        pid = self._parse_playlist_id(pid)
        self.last_playlist_name = name or self.last_playlist_name
        resp = self.session.get(f"{API}/api/v6/playlist/detail",
                                params={"id": pid, "n": 1000}, timeout=25)
        playlist_obj = (resp.json() or {}).get("playlist") or {}
        self.last_playlist_name = self.last_playlist_name or (playlist_obj.get("name") or "")
        ids = [t["id"] for t in (playlist_obj.get("trackIds") or playlist_obj.get("tracks") or [])]
        if not ids:
            raise PlatformError("歌单拉取失败：cookie 可能过期，或歌单不是自己的")
        tracks: list[Track] = []
        for start in range(0, len(ids), 100):      # 一次 100 个，多了 URL 会被截断
            chunk = ids[start:start + 100]
            resp = self.session.get(f"{API}/api/song/detail",
                                    params={"ids": json.dumps(chunk)}, timeout=25)
            for song in (resp.json() or {}).get("songs", []):
                tracks.append(Track(
                    platform=self.key,
                    id=str(song["id"]),
                    name=song.get("name") or "",
                    artists=[a["name"] for a in song.get("artists") or []],
                    album=(song.get("album") or {}).get("name", ""),
                    duration_ms=song.get("duration") or 0,
                ))
        return tracks

    @staticmethod
    def _parse_playlist_id(playlist: str) -> str:
        text = (playlist or "").strip()
        if "id=" in text:
            text = text.split("id=", 1)[1]
        text = text.split("&")[0].strip("/")
        if not text.isdigit():
            raise PlatformError(f"看不懂歌单标识：{playlist}（请填歌单 id 或歌单链接）")
        return text

    # ---------------- 下载 ----------------
    def download(self, track: Track, dest_dir: str, quality: str = "320") -> str:
        from ..util import safe_name

        self.ensure_login()
        levels = [LEVEL.get(quality, "exhigh")] + [lv for lv in
                                                   ("exhigh", "higher", "standard") if lv != LEVEL.get(quality)]
        info = None
        for level in levels:
            try:
                resp = self.session.get(
                    f"{API}/api/song/enhance/player/url/v1",
                    params={"ids": json.dumps([int(track.id)]), "level": level, "encodeType": "mp3"},
                    timeout=25)
                data = (resp.json().get("data") or [{}])[0]
            except Exception:
                time.sleep(1)
                continue
            if data.get("url"):
                info = data
                break
        if not info:
            raise Unavailable(f"{track.name} 在网易云无版权/已下架")

        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, safe_name(str(track)) + ".mp3")
        last_error = None
        for attempt in range(3):
            try:
                with self.session.get(info["url"], stream=True, timeout=90) as resp:
                    resp.raise_for_status()
                    return self._write(resp.iter_content(256 * 1024), path)
            except Exception as exc:                     # noqa: BLE001 - 网络抖动重试
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
        raise PlatformError(f"下载失败：{last_error}")

    # ---------------- 搜索（换源用） ----------------
    def search(self, keyword: str, limit: int = 5) -> list[Track]:
        resp = self.session.get(f"{API}/api/search/get",
                                params={"s": keyword, "type": 1, "limit": limit}, timeout=20)
        songs = ((resp.json() or {}).get("result") or {}).get("songs") or []
        return [Track(platform=self.key, id=str(s["id"]), name=s.get("name") or "",
                      artists=[a["name"] for a in s.get("artists") or []],
                      album=(s.get("album") or {}).get("name", ""),
                      duration_ms=s.get("duration") or 0) for s in songs]


def random_device_id(length: int = 32) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(length))
