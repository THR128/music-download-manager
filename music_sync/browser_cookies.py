# -*- coding: utf-8 -*-
"""从用户自己的浏览器里读取登录态 —— 让不懂 cookie 的人也能用。

思路：用户只要在浏览器里正常登录一次（点"用微信/QQ 登录"），
浏览器的数据库里就有登录态了，这里的代码把它读出来转成请求头。
普通用户全程不需要看到 "cookie" 这个词，设置界面只需要一个「自动获取登录状态」按钮。

**Windows**：Chromium 系（Chrome / Edge / Brave / 360极速 …）用 DPAPI + AES-GCM：

* ``Local State`` 里的 ``os_crypt.encrypted_key`` 是 base64("DPAPI" + AES 密钥)，
  DPAPI 用当前 Windows 用户身份解开即可；
* 数据库里 ``encrypted_value`` 形如 ``b"v10" + nonce(12) + 密文``；
* Chrome 127+ 在部分机器上用 ``v20``（App-Bound 加密），普通进程解不开，
  遇到就跳过并给出中文提示，不报错崩溃。

**macOS**：Chromium 系的加密算法完全不同（AES-128-CBC + 钥匙串密码）：

* 密钥 = 钥匙串里 ``"<浏览器> Safe Storage"`` 的密码，经
  ``PBKDF2-HMAC-SHA1(password, salt=b"saltysalt", iterations=1003, dklen=16)``
  派生 16 字节 AES 密钥；
* IV 固定为 16 个空格，PKCS7 补位；老版本密文带 ``v10`` 前缀（跳过 3 字节）；
* 取密码要靠 ``security`` 命令，**失败/超时/无输出一律降级成 None 并缓存**，
  绝不在同一次运行里反复弹钥匙串——那会把用户吓一跳。

**Safari**（仅 macOS，尽力而为）：``~/Library/Cookies/Cookies.binarycookies``
是 Apple 的私有二进制格式，解析器写得很保守，结构对不上就当没读到。
"""
from __future__ import annotations

import base64
import ctypes
import glob
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import pbkdf2_hmac

from . import ospaths
from .util import get_logger

# 平台常量：放在模块级别，测试可以 monkeypatch 它们来伪造别的系统
IS_WIN = ospaths.IS_WIN
IS_MAC = ospaths.IS_MAC

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

# macOS：各浏览器在钥匙串里存 Safe Storage 密码用的服务名
_MAC_SAFE_STORAGE = {
    "chrome": "Chrome Safe Storage",
    "chrome beta": "Chrome Safe Storage",
    "chrome canary": "Chrome Safe Storage",
    "chrome sxs": "Chrome Safe Storage",
    "chromium": "Chromium Safe Storage",
    "edge": "Microsoft Edge Safe Storage",
    "brave": "Brave Safe Storage",
    "vivaldi": "Vivaldi Safe Storage",
    "arc": "Arc Safe Storage",
    "opera": "Opera Safe Storage",
}

# macOS：从配置目录路径反推是哪个浏览器（关键字 -> _MAC_SAFE_STORAGE 的键）
_MAC_PATH_SIGNATURES = (
    ("microsoft edge", "edge"),
    ("bravesoftware", "brave"),
    ("brave-browser", "brave"),
    ("vivaldi", "vivaldi"),
    ("/arc/", "arc"),
    ("chromium", "chromium"),
    ("google/chrome", "chrome"),
    ("chrome", "chrome"),
)

_MAC_SALT = b"saltysalt"
_MAC_IV = b" " * 16
_MAC_ITERATIONS = 1003

# 钥匙串查询结果缓存：service 名 -> 16 字节密钥或 None（None 也缓存，避免重复弹窗）
_MAC_KEY_CACHE: dict[str, bytes | None] = {}


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


# ---------------------------------------------------------------------------
# Windows：DPAPI + AES-GCM
# ---------------------------------------------------------------------------
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


def _win_key(browser_dir: str) -> bytes | None:
    """Windows：从 ``Local State`` 里取 AES-GCM 主密钥（DPAPI 解开）。"""
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


# 历史名字，保留给可能存在的旧引用
_aes_gcm_key = _win_key


def _decrypt_win(blob: bytes, key: bytes | None) -> str:
    """Windows：``v10/v11`` 前缀的 AES-GCM 密文；``v20`` 无解；其余当明文。"""
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


# ---------------------------------------------------------------------------
# macOS：钥匙串密码 + AES-128-CBC
# ---------------------------------------------------------------------------
def _mac_service_for(profile_dir: str) -> str:
    """从配置目录路径推断该用哪个钥匙串服务名；推断不出时按 Chrome 猜。"""
    low = (profile_dir or "").replace("\\", "/").lower()
    for keyword, browser in _MAC_PATH_SIGNATURES:
        if keyword in low:
            return _MAC_SAFE_STORAGE.get(browser, "Chrome Safe Storage")
    return _MAC_SAFE_STORAGE["chrome"]


def _mac_key(service_name: str) -> bytes | None:
    """取钥匙串里的 Safe Storage 密码并派生 AES-128 密钥。

    失败、超时、没装命令行工具、用户在弹窗上点了「拒绝」——全部返回 ``None``，
    并把这次失败缓存下来（同一次运行最多问一次，不反复弹钥匙串）。
    """
    if not IS_MAC:
        return None
    if service_name in _MAC_KEY_CACHE:
        return _MAC_KEY_CACHE[service_name]

    password = b""
    try:
        proc = subprocess.run(["security", "find-generic-password", "-w", "-s", service_name],
                              capture_output=True, timeout=5)
        if getattr(proc, "returncode", 1) == 0:
            password = (getattr(proc, "stdout", b"") or b"").strip()
    except subprocess.TimeoutExpired:
        get_logger().debug("读取「%s」的保存密码超时（钥匙串可能弹窗等确认）", service_name)
    except FileNotFoundError:
        get_logger().debug("系统里没有 security 命令，跳过钥匙串")
    except OSError as exc:                     # 权限、被安全软件拦住等
        get_logger().debug("读取「%s」的保存密码失败: %s", service_name, exc)
    except Exception as exc:                   # noqa: BLE001 - 任何意外都不许崩
        get_logger().debug("读取「%s」的保存密码时出现意外错误: %s", service_name, exc)

    key: bytes | None = None
    if password:
        try:
            key = pbkdf2_hmac("sha1", password, _MAC_SALT, _MAC_ITERATIONS, 16)
        except Exception as exc:               # noqa: BLE001
            get_logger().debug("派生密钥失败: %s", exc)
            key = None
    if key is None:
        get_logger().debug("没拿到「%s」的保存密码，该浏览器可能读不出登录状态", service_name)
    _MAC_KEY_CACHE[service_name] = key
    return key


def _strip_pkcs7(data: bytes) -> bytes:
    """去掉 PKCS7 补位；补位不合法就原样返回（宁可解不出也别崩）。"""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def _decrypt_mac(blob: bytes, key: bytes | None) -> str:
    """macOS：AES-128-CBC 解密（``v10`` 前缀可有可无）。"""
    if not blob:
        return ""
    data = blob[3:] if blob[:3] == b"v10" else blob
    if key is None:
        # 极少数老版本/自编译版本直接存明文，尽力而为
        return _plain_looking(data)
    if not data or len(data) % 16:
        # 长度不是 16 的整数倍：早期版本会在密文尾巴多挂 32 字节 HMAC，砍掉再试
        if len(data) > 32 and (len(data) - 32) % 16 == 0:
            data = data[:-32]
        else:
            return _plain_looking(data) or ""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        decryptor = Cipher(algorithms.AES(key), modes.CBC(_MAC_IV)).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
    except Exception as exc:                   # noqa: BLE001 - 密钥不对/库版本差异
        get_logger().debug("解密浏览器数据失败: %s", exc)
        return ""
    plain = _strip_pkcs7(plain)
    text = _decode(plain)
    if text is None and len(plain) > 32:
        # 解出来是 32 字节以上的乱码：可能是早期版本多塞的 HMAC，截断尾部再试
        text = _decode(plain[:-32])
    return text or ""


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _plain_looking(data: bytes) -> str:
    """密文解不开时，若内容本身像可打印文本就当明文返回，否则空串。"""
    if not data:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return text if text.isprintable() else ""


def _decrypt_value(blob: bytes, key: bytes | None) -> str:
    """按当前平台选解密实现（对外保持旧名字，参数含义与各自平台的密钥一致）。"""
    if IS_MAC:
        return _decrypt_mac(blob, key)
    return _decrypt_win(blob, key)


# ---------------------------------------------------------------------------
# 浏览器配置目录
# ---------------------------------------------------------------------------
def chromium_profiles() -> list[tuple[str, str]]:
    """返回 [(浏览器名, 用户数据目录), …]。平台差异交给 ``ospaths``。"""
    return ospaths.browser_profiles()


def _cookie_db_paths(profile_dir: str) -> list[str]:
    found: list[str] = []
    for rel in ("Network/Cookies", "Cookies", "Default/Network/Cookies", "Default/Cookies"):
        path = os.path.join(profile_dir, rel)
        if os.path.exists(path):
            found.append(path)
    # User Data 目录下的 Profile */Network/Cookies
    for path in glob.glob(os.path.join(profile_dir, "Profile *", "Network", "Cookies")):
        if path not in found:
            found.append(path)
    return found


def read_browser(profile_dir: str) -> dict[str, str]:
    """读一个 Chromium 配置目录里的全部登录状态（名字 -> 值）。"""
    key: bytes | None = None
    if IS_MAC:
        key = _mac_key(_mac_service_for(profile_dir))

    merged: dict[str, str] = {}
    for db in _cookie_db_paths(profile_dir):
        if not IS_MAC and key is None:
            # Local State 在 User Data 根目录或上一层
            for base in (os.path.dirname(os.path.dirname(db)), os.path.dirname(os.path.dirname(os.path.dirname(db)))):
                try:
                    key = _win_key(base)
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
            get_logger().debug("读取浏览器登录状态失败 %s: %s", db, exc)
        finally:
            if tmp != db and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return merged


# ---------------------------------------------------------------------------
# Safari（仅 macOS，尽力而为）
# ---------------------------------------------------------------------------
def safari_cookie_path() -> str:
    return os.path.join(os.path.expanduser("~"), "Library", "Cookies", "Cookies.binarycookies")


def _safari_string(record: bytes, offset: int) -> str:
    if offset <= 0 or offset >= len(record):
        return ""
    end = record.find(b"\x00", offset)
    if end < 0:
        end = len(record)
    return record[offset:end].decode("utf-8", "ignore")


def parse_binarycookies(blob: bytes) -> list[dict[str, str]]:
    """解析 Safari 的 binarycookies：返回 [{name, value, domain, path}, …]。

    格式对不上就返回已经解出来的部分（通常是空），**不抛异常**。
    """
    out: list[dict[str, str]] = []
    try:
        if len(blob) < 12 or blob[:4] != b"cook":
            return out
        pages = int.from_bytes(blob[4:8], "big")
        if pages <= 0 or pages > 4096:
            return out
        sizes = []
        for i in range(pages):
            at = 8 + i * 4
            if at + 4 > len(blob):
                return out
            size = int.from_bytes(blob[at:at + 4], "big")
            if size <= 0:
                return out
            sizes.append(size)

        pos = 8 + pages * 4
        for size in sizes:
            if pos + size > len(blob):
                return out
            page = blob[pos:pos + size]
            pos += size
            if len(page) < 8 or page[:4] != b"\x00\x00\x01\x00":
                continue
            count = int.from_bytes(page[4:8], "big")
            rec_pos = 8
            for _ in range(count):
                if rec_pos + 4 > len(page):
                    break
                rec_size = int.from_bytes(page[rec_pos:rec_pos + 4], "big")
                if rec_size < 48 or rec_pos + rec_size > len(page):
                    break
                record = page[rec_pos:rec_pos + rec_size]
                rec_pos += rec_size
                out.append({
                    "domain": _safari_string(record, int.from_bytes(record[16:20], "big")),
                    "name": _safari_string(record, int.from_bytes(record[20:24], "big")),
                    "path": _safari_string(record, int.from_bytes(record[24:28], "big")),
                    "value": _safari_string(record, int.from_bytes(record[28:32], "big")),
                })
    except Exception as exc:                  # noqa: BLE001 - 私有格式，坏就坏
        get_logger().debug("解析 Safari 登录数据失败: %s", exc)
        return out
    return out


def read_safari() -> dict[str, str]:
    """读 Safari 的登录状态；不是 macOS、没装、格式不认识都返回空。"""
    if not IS_MAC:
        return {}
    path = safari_cookie_path()
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, "rb") as fh:
            blob = fh.read()
        jar: dict[str, str] = {}
        for item in parse_binarycookies(blob):
            name, value = item.get("name") or "", item.get("value") or ""
            if name and value and name not in jar:
                jar[name] = value
        return jar
    except Exception as exc:                  # noqa: BLE001 - 读不到就算没读到
        get_logger().debug("读取 Safari 登录状态失败: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 对外：收集 / 转请求头 / 挑一个最好的
# ---------------------------------------------------------------------------
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
    if not (IS_WIN or IS_MAC):
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
    if IS_MAC:
        try:
            jar = _filter(read_safari(), platform)
        except Exception as exc:
            get_logger().debug("扫描 Safari 失败: %s", exc)
            jar = {}
        if jar:
            out.append(CookieSource(browser="Safari", path=safari_cookie_path(), cookies=jar))
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
