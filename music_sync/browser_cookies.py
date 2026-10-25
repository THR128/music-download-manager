# -*- coding: utf-8 -*-
"""从用户自己的浏览器里读取登录态 —— 让不懂 cookie 的人也能用。

思路：用户只要在浏览器里正常登录一次（点"用微信/QQ 登录"），
浏览器的 cookies 里就有登录态了，这里的代码把它读出来转成请求头。
普通用户全程不需要看到 "cookie" 这个词，设置界面只需要一个「自动获取登录状态」按钮。

Chromium 系（Chrome / Edge / Brave / 360极速 …）的 cookies 用 DPAPI + AES-GCM 加密，
本模块复用与网易云客户端相同的解密思路（见 ``platforms/netease.py``）：

* ``Local State`` 里的 ``os_crypt.encrypted_key`` 是 base64("DPAPI" + AES 密钥)，
  DPAPI 用当前 Windows 用户身份解开即可；
* cookies 表里 ``encrypted_value`` 形如 ``b"v10" + nonce(12) + 密文``；
* Chrome 127+ 在部分机器上用 ``v20``（App-Bound 加密），普通进程解不开，
  遇到就跳过并给出中文提示，不报错崩溃。
"""
from __future__ import annotations

import base64
import ctypes
import glob
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass

from .util import get_logger

# 不同平台的 cookie 域名过滤
DOMAINS = {
    "netease": ("music.163.com", "163.com"),
    "qqmusic": ("y.qq.com", "qq.com"),
    "kugou": ("kugou.com",),
    "soda": ("douyin.com", "soda.com"),
}

# 重要的 cookie 名（用于判断"到底算不算登录了"）
KEY_COOKIES = {
    "netease": ("MUSIC_U",),
    "qqmusic": ("qm_keyst", "qqmusic_key", "uin", "wxuin"),
    "kugou": ("kg_mid", "KuGoo", "kg_token"),
    "soda": (),
}


@dataclass
class CookieSource:
    browser: str
    path: str
    cookies: dict[str, str]

    @property
    def count(self) -> int:
        return len(self.cookies)

    @property
    def logged_in(self) -> bool:
        return bool(self.cookies)


def _dpapi_unprotect(data: bytes) -> bytes:
    """用当前 Windows 用户的 DPAPI 解密。"""

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf_in = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                             ctypes.POINTER(ctypes.c_char)))
    buf_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(buf_in), None, None, None, None, 0, ctypes.byref(buf_out)):
        raise OSError("DPAPI 解密失败（换个 Windows 用户或权限不足）")
    try:
        return ctypes.string_at(buf_out.pbData, buf_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(buf_out.pbData)


def _aes_gcm_key(browser_dir: str) -> bytes | None:
    local_state = os.path.join(browser_dir, "Local State")
    if not os.path.exists(local_state):
        return None
    import json
    with open(local_state, "r", encoding="utf-8", errors="ignore") as fh:
        state = json.load(fh)
    enc = (state.get("os_crypt") or {}).get("encrypted_key")
    if not enc:
        return None
    raw = base64.b64decode(enc)
    if raw[:5] == b"DPAPI":
        raw = raw[5:]
    return _dpapi_unprotect(raw)


def _decrypt_value(blob: bytes, key: bytes | None) -> str:
    if not blob:
        return ""
    if blob[:3] in (b"v10", b"v11"):
        if not key:
            return ""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce, payload = blob[3:15], blob[15:]
        try:
            return AESGCM(key).decrypt(nonce, payload, None).decode("utf-8", "ignore")
        except Exception:
            return ""
    if blob[:3] == b"v20":
        return ""      # App-Bound 加密，普通进程无解
    try:
        return blob.decode("utf-8", "ignore")
    except Exception:
        return ""


def chromium_profiles() -> list[tuple[str, str]]:
    """返回 [(浏览器名, 用户数据目录), …]。"""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    roaming = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
    candidates = [
        ("Edge", os.path.join(local, "Microsoft", "Edge", "User Data")),
        ("Chrome", os.path.join(local, "Google", "Chrome", "User Data")),
        ("Chrome Beta", os.path.join(local, "Google", "Chrome Beta", "User Data")),
        ("Chrome SxS", os.path.join(local, "Google", "Chrome SxS", "User Data")),
        ("Brave", os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data")),
        ("Vivaldi", os.path.join(local, "Vivaldi", "User Data")),
        ("Opera", os.path.join(roaming, "Opera Software", "Opera Stable")),
        ("360极速", os.path.join(roaming, "360se6", "User Data")),
        ("360安全", os.path.join(local, "360Chrome", "Chrome", "User Data")),
        ("QQ浏览器", os.path.join(local, "Tencent", "QQBrowser", "User Data")),
        ("搜狗高速", os.path.join(roaming, "SogouExplorer", "User Data")),
    ]
    return [(name, path) for name, path in candidates if os.path.isdir(path)]


def _cookie_db_paths(profile_dir: str) -> list[str]:
    found: list[str] = []
    for rel in ("Network/Cookies", "Cookies", "Default/Network/Cookies", "Default/Cookies"):
        path = os.path.join(profile_dir, rel)
        if os.path.exists(path):
            found.append(path)
    # User Data 目录下的 Profile */Network/Cookies
    for path in glob.glob(os.path.join(profile_dir, "Profile *", "Network", "Cookies")):
        found.append(path)
    return found


def read_browser(profile_dir: str) -> dict[str, str]:
    """读一个 Chromium 配置目录里的全部 cookie（名字 -> 值）。"""
    key = None
    merged: dict[str, str] = {}
    for db in _cookie_db_paths(profile_dir):
        if key is None:
            # Local State 在 User Data 根目录或上一层
            for base in (os.path.dirname(os.path.dirname(db)), os.path.dirname(os.path.dirname(os.path.dirname(db)))):
                try:
                    key = _aes_gcm_key(base)
                except Exception:
                    key = None
                if key:
                    break
        tmp = os.path.join(tempfile.gettempdir(), "music_sync_cookies_%d.db" % os.getpid())
        try:
            shutil.copy2(db, tmp)     # 浏览器运行中也复制一份再读
        except Exception:
            tmp = db
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % tmp, uri=True)
            try:
                rows = conn.execute("select host_key, name, value, encrypted_value from cookies").fetchall()
            finally:
                conn.close()
            for host, name, plain, enc in rows:
                value = plain or _decrypt_value(enc or b"", key)
                if value and name not in merged:
                    merged[name] = value
        except Exception as exc:      # 库被占用等
            get_logger().debug("读取浏览器 cookie 失败 %s: %s", db, exc)
        finally:
            if tmp != db and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return merged


def _filter(cookies: dict[str, str], platform: str) -> dict[str, str]:
    # 简化处理：Chromium 的 cookie 已按 host 存库，我们这里靠名字白名单 + 平台常见名
    names = set()
    for key in KEY_COOKIES.get(platform, ()):
        names.add(key)
    wanted = {
        "netease": {"MUSIC_U", "MUSIC_A_T", "MUSIC_R_U", "__csrf", "NMTID", "MUSIC_A", "os", "appver", "channel", "deviceId"},
        "qqmusic": {"qm_keyst", "qqmusic_key", "uin", "wxuin", "p_uin", "p_skey", "skey", "psrf_qqunionid",
                    "psrf_access_token_expiresAt", "psrf_musickey_createtime", "psrf_qqrefresh_token"},
        "kugou": {"kg_mid", "kg_dfid", "KuGoo", "kg_token", "userid", "KugooID", "t", "dfid"},
        "soda": set(),
    }.get(platform, set())
    names |= wanted
    if not names:      # 未知平台：全给
        return dict(cookies)
    return {k: v for k, v in cookies.items() if k in names}


def collect(platform: str) -> list[CookieSource]:
    """扫描本机所有浏览器，返回能拿到该平台登录态的来源列表。"""
    if sys.platform != "win32":
        return []
    out: list[CookieSource] = []
    for name, profile_dir in chromium_profiles():
        try:
            cookies = _filter(read_browser(profile_dir), platform)
        except Exception as exc:
            get_logger().debug("扫描 %s 失败: %s", name, exc)
            continue
        if cookies:
            out.append(CookieSource(browser=name, path=profile_dir, cookies=cookies))
    return out


def to_header(cookies: dict[str, str]) -> str:
    return "; ".join("%s=%s" % (k, v) for k, v in cookies.items())


def best(platform: str) -> CookieSource | None:
    """挑一个"看起来已登录"的来源：优先关键 cookie 齐全的。"""
    found = collect(platform)
    if not found:
        return None
    keys = KEY_COOKIES.get(platform, ())
    found.sort(key=lambda src: (-sum(1 for k in keys if k in src.cookies), -src.count))
    return found[0]
