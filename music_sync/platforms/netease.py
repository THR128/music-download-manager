# -*- coding: utf-8 -*-
"""网易云音乐适配器。

两条路取 cookie：
1. 自动：从网易云客户端的内置 Chromium 里解密 cookie（客户端保持登录即可，用户零操作）；
2. 手动：配置里填 ``platforms.netease.cookie`` 字符串，覆盖自动结果。

自动解密原理：客户端 ``LocalPrefs.json`` 的 ``os_crypt.encrypted_key`` 经 DPAPI 解出
32 字节 AES 主密钥，再用 AES-GCM 解 Cookies 库里 ``v10`` 前缀的密文。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import glob
import json
import os
import random
import shutil
import sqlite3
import string
import time

from .base import BaseAdapter, NotLoggedIn, PlatformError, Track, Unavailable
from ..util import make_session

API = "https://music.163.com"
LEVEL = {"lossless": "lossless", "320": "exhigh", "192": "higher", "128": "standard"}


def _dpapi_unprotect(data: bytes) -> bytes:
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
    """找到网易云客户端的 Chromium 数据目录（webappXXXX）。"""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "NetEase", "CloudMusic")
    if not os.path.isdir(base):
        return ""
    cands = [p for p in glob.glob(os.path.join(base, "webapp*")) if os.path.isdir(p)]
    cands.sort(key=os.path.getmtime, reverse=True)
    return cands[0] if cands else ""


def decrypt_client_cookies(client_dir: str) -> dict[str, str]:
    """从客户端 Cookie 库解出 .music.163.com 的 cookie。"""
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
    tmp_dir = os.path.join(os.environ.get("TEMP", "."), "music-sync-cookies")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp = os.path.join(tmp_dir, "Cookies")
    con = None
    try:
        shutil.copy2(src, tmp)
        con = sqlite3.connect(tmp)
    except Exception:
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)

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
        try:
            os.remove(tmp)
        except OSError:
            pass
    return jar


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
    def list_tracks(self, playlist: str) -> list[Track]:
        self.ensure_login()
        pid = self._parse_playlist_id(playlist)
        resp = self.session.get(f"{API}/api/v6/playlist/detail",
                                params={"id": pid, "n": 1000}, timeout=25)
        playlist_obj = (resp.json() or {}).get("playlist") or {}
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
