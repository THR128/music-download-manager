# -*- coding: utf-8 -*-
"""macOS 移植部分的离线单测 —— 全部在 Windows 上跑，不依赖真 Mac。

这里测的是「平台差异出口」和「macOS 的加密/路径分支」，做法是把模块里的
平台常量、``os.path`` 探测、``Subprocess`` 调用统统 monkeypatch 掉：

* ``ospaths`` 的路径规则（伪造 IS_MAC / IS_WIN + 目录探测）；
* ``_decrypt_mac`` 的 AES-128-CBC 解密（测试里现场用 ``cryptography`` 加密样本）；
* 钥匙串取密码失败时整条 ``best()`` 链路只是返回 None，不崩；
* Safari 的私有二进制格式遇到垃圾数据返回空，不崩；
* 网易云客户端 macOS 上「不加密、直接明文」这条降级路。

用 ``unittest`` 写（``python -m unittest discover`` 能跑），pytest 也能直接收集。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import unittest
from hashlib import pbkdf2_hmac
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:                              # 支持 python tests/test_mac_port.py
    sys.path.insert(0, ROOT)

from music_sync import browser_cookies, ospaths            # noqa: E402
from music_sync.platforms import netease                   # noqa: E402

SALT = b"saltysalt"
IV = b" " * 16


def mac_key(password: bytes = b"secret-password") -> bytes:
    """按 macOS Chromium 的规则派生 16 字节 AES 密钥。"""
    return pbkdf2_hmac("sha1", password, SALT, 1003, 16)


def mac_encrypt(plain: bytes, key: bytes, prefix: bytes = b"") -> bytes:
    """用与浏览器相同的方式加密（测试里造样本用）。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - len(plain) % 16
    data = plain + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    return prefix + encryptor.update(data) + encryptor.finalize()


def make_cookie_db(path: str, rows: list[tuple[str, str, str, bytes]]) -> str:
    """造一个最小可用的 Cookies 库：rows = [(host, name, value, encrypted_value)]。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("create table cookies (host_key text, name text, value text, "
                     "encrypted_value blob)")
        conn.executemany("insert into cookies values (?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return path


class OspathsTest(unittest.TestCase):
    """平台路径规则：只按常量分支，不碰真实文件系统（除 ~ 展开外）。"""

    def setUp(self):
        # 让 MUSIC_SYNC_HOME 为空，避免开发机的环境变量影响断言
        self._env = patch.dict(os.environ, {"MUSIC_SYNC_HOME": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_constants_are_booleans(self):
        self.assertIsInstance(ospaths.IS_WIN, bool)
        self.assertIsInstance(ospaths.IS_MAC, bool)
        self.assertIsInstance(ospaths.IS_LINUX, bool)
        # 当前这台机器是 Windows，两个平台标记不能同时为真
        self.assertFalse(ospaths.IS_WIN and ospaths.IS_MAC)

    def test_config_dir_windows(self):
        with patch.object(ospaths, "IS_WIN", True), patch.object(ospaths, "IS_MAC", False):
            self.assertEqual(ospaths.config_dir(),
                             os.path.join(os.path.expanduser("~"), ".music-sync"))

    def test_config_dir_macos(self):
        with patch.object(ospaths, "IS_WIN", False), patch.object(ospaths, "IS_MAC", True):
            self.assertEqual(ospaths.config_dir(),
                             os.path.join(os.path.expanduser("~"), "Library",
                                          "Application Support", "music-sync"))

    def test_config_dir_linux(self):
        with patch.object(ospaths, "IS_WIN", False), patch.object(ospaths, "IS_MAC", False):
            self.assertEqual(ospaths.config_dir(),
                             os.path.join(os.path.expanduser("~"), ".config", "music-sync"))

    def test_config_dir_env_wins(self):
        with patch.dict(os.environ, {"MUSIC_SYNC_HOME": "~/my-music-home"}):
            expected = os.path.abspath(os.path.expanduser("~/my-music-home"))
            for is_mac in (False, True):
                with patch.object(ospaths, "IS_WIN", not is_mac), patch.object(ospaths, "IS_MAC", is_mac):
                    self.assertEqual(ospaths.config_dir(), expected)

    def test_default_library_dir_windows_unchanged(self):
        """Windows 默认曲库目录必须保持 F:\\FFOutput —— 老用户的歌都在那儿。"""
        with patch.object(ospaths, "IS_WIN", True), patch.object(ospaths, "IS_MAC", False):
            self.assertEqual(ospaths.default_library_dir(), "F:\\FFOutput")

    def test_default_library_dir_macos(self):
        with patch.object(ospaths, "IS_WIN", False), patch.object(ospaths, "IS_MAC", True):
            self.assertEqual(ospaths.default_library_dir(),
                             os.path.join(os.path.expanduser("~"), "Music", "本地曲库"))

    def test_default_library_dir_linux(self):
        with patch.object(ospaths, "IS_WIN", False), patch.object(ospaths, "IS_MAC", False):
            self.assertEqual(ospaths.default_library_dir(),
                             os.path.join(os.path.expanduser("~"), "Music"))

    def test_default_client_download_dir(self):
        with patch.object(ospaths, "IS_WIN", True), patch.object(ospaths, "IS_MAC", False):
            self.assertEqual(ospaths.default_client_download_dir(), "D:\\CloudMusic")
        with patch.object(ospaths, "IS_WIN", False), patch.object(ospaths, "IS_MAC", True):
            self.assertEqual(ospaths.default_client_download_dir(),
                             os.path.join(os.path.expanduser("~"), "Music", "CloudMusic"))

    def test_browser_profiles_macos_keeps_only_existing(self):
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
        with patch.object(ospaths, "IS_WIN", False), patch.object(ospaths, "IS_MAC", True), \
                patch.object(ospaths.os.path, "isdir",
                             side_effect=lambda p: p.startswith(os.path.join(root, "Google"))):
            profiles = dict(ospaths.browser_profiles())
        self.assertIn("Chrome", profiles)
        self.assertEqual(profiles["Chrome"], os.path.join(root, "Google", "Chrome", "Default"))
        self.assertNotIn("Edge", profiles)          # 目录不存在就该被过滤掉

    def test_browser_profiles_windows_keeps_windows_list(self):
        with patch.object(ospaths, "IS_WIN", True), patch.object(ospaths, "IS_MAC", False), \
                patch.object(ospaths.os.path, "isdir", return_value=True):
            names = [name for name, _ in ospaths.browser_profiles()]
        for expected in ("Edge", "Chrome", "Brave", "Vivaldi", "Opera",
                         "360极速", "360安全", "QQ浏览器", "搜狗高速"):
            self.assertIn(expected, names)

    def test_netease_client_dirs_macos(self):
        home = os.path.expanduser("~")
        keep = os.path.join(home, "Library", "Containers", "com.netease.163music",
                            "Data", "Library", "Application Support", "com.netease.163music")
        with patch.object(ospaths, "IS_WIN", False), patch.object(ospaths, "IS_MAC", True), \
                patch.object(ospaths.os.path, "exists", side_effect=lambda p: p == keep), \
                patch.object(ospaths.os.path, "isdir", side_effect=lambda p: p == keep):
            dirs = ospaths.netease_client_dirs()
        self.assertEqual(dirs, [keep])

    def test_netease_client_dirs_windows_glob(self):
        base = os.path.join("C:\\", "FakeLocal", "NetEase", "CloudMusic")
        found = [os.path.join(base, "webapp2"), os.path.join(base, "webapp1")]
        with patch.object(ospaths, "IS_WIN", True), patch.object(ospaths, "IS_MAC", False), \
                patch.dict(os.environ, {"LOCALAPPDATA": "C:\\FakeLocal"}), \
                patch.object(ospaths.glob, "glob", return_value=found), \
                patch.object(ospaths.os.path, "isdir", return_value=True), \
                patch.object(ospaths.os.path, "getmtime", side_effect=lambda p: 1.0 if p.endswith("1") else 2.0):
            dirs = ospaths.netease_client_dirs()
        self.assertEqual(dirs, [os.path.join(base, "webapp2"), os.path.join(base, "webapp1")])

    def test_mac_installed_apps_on_windows_is_empty(self):
        with patch.object(ospaths, "IS_WIN", True), patch.object(ospaths, "IS_MAC", False):
            self.assertEqual(ospaths.mac_installed_apps(), {})

    def test_mac_installed_apps_scans_applications(self):
        entries = ["QQMusic.app", "neteaseMusic.app", "SomeOther.app"]
        with patch.object(ospaths, "IS_WIN", False), patch.object(ospaths, "IS_MAC", True), \
                patch.object(ospaths.os.path, "isdir",
                             side_effect=lambda p: p == "/Applications"), \
                patch.object(ospaths.os, "listdir",
                             side_effect=lambda p: entries if p == "/Applications" else []):
            apps = ospaths.mac_installed_apps()
        self.assertEqual(apps.get("qqmusic"), os.path.join("/Applications", "QQMusic.app"))
        self.assertEqual(apps.get("netease"), os.path.join("/Applications", "neteaseMusic.app"))
        self.assertNotIn("kugou", apps)

    def test_mac_installed_apps_survives_listdir_error(self):
        with patch.object(ospaths, "IS_WIN", False), patch.object(ospaths, "IS_MAC", True), \
                patch.object(ospaths.os.path, "isdir", return_value=True), \
                patch.object(ospaths.os, "listdir", side_effect=PermissionError), \
                patch.object(ospaths.os.path, "exists", return_value=False):
            self.assertEqual(ospaths.mac_installed_apps(), {})


class MacDecryptTest(unittest.TestCase):
    """macOS 的 AES-128-CBC 解密：样本现场加密，不依赖任何真机数据。"""

    def setUp(self):
        self.key = mac_key()

    def test_roundtrip_plain(self):
        blob = mac_encrypt(b"MUSIC_U=abcdef012345", self.key)
        self.assertEqual(browser_cookies._decrypt_mac(blob, self.key), "MUSIC_U=abcdef012345")

    def test_roundtrip_with_v10_prefix(self):
        blob = mac_encrypt(b"MUSIC_A=xyz", self.key, prefix=b"v10")
        self.assertEqual(browser_cookies._decrypt_mac(blob, self.key), "MUSIC_A=xyz")

    def test_padding_block_aligned(self):
        """正好 16 字节的明文会补一整块，解出来不能带尾巴。"""
        payload = b"0123456789abcdef"
        blob = mac_encrypt(payload, self.key)
        self.assertEqual(len(blob), 32)
        self.assertEqual(browser_cookies._decrypt_mac(blob, self.key), payload.decode())

    def test_padding_edge_lengths(self):
        for length in (1, 15, 16, 17, 31, 32):
            payload = b"x" * length
            blob = mac_encrypt(payload, self.key)
            self.assertEqual(browser_cookies._decrypt_mac(blob, self.key),
                             payload.decode(), msg=f"len={length}")

    def test_cjk_value(self):
        blob = mac_encrypt("MUSIC_U=中文值".encode("utf-8"), self.key)
        self.assertEqual(browser_cookies._decrypt_mac(blob, self.key), "MUSIC_U=中文值")

    def test_wrong_key_returns_empty(self):
        blob = mac_encrypt(b"MUSIC_U=abcdef", self.key)
        self.assertEqual(browser_cookies._decrypt_mac(blob, mac_key(b"another")), "")

    def test_empty_blob(self):
        self.assertEqual(browser_cookies._decrypt_mac(b"", self.key), "")

    def test_garbage_does_not_raise(self):
        self.assertEqual(browser_cookies._decrypt_mac(b"\x00\x01\x02", self.key), "")

    def test_no_key_treats_printable_as_plaintext(self):
        """拿不到钥匙串密钥时，明文存的那些还能读出来。"""
        self.assertEqual(browser_cookies._decrypt_mac(b"plain-token", None), "plain-token")

    def test_strip_pkcs7_rejects_bad_padding(self):
        self.assertEqual(browser_cookies._strip_pkcs7(b"abc\x02\x03"), b"abc\x02\x03")
        self.assertEqual(browser_cookies._strip_pkcs7(b"abc\x03\x03\x03"), b"abc")


class MacKeychainTest(unittest.TestCase):
    """钥匙串取密码：失败要降级，而且同一次运行不能反复去问。"""

    def setUp(self):
        self._cache = patch.object(browser_cookies, "_MAC_KEY_CACHE", {})
        self._cache.start()
        self._mac = patch.object(browser_cookies, "IS_MAC", True)
        self._mac.start()
        self._win = patch.object(browser_cookies, "IS_WIN", False)
        self._win.start()

    def tearDown(self):
        self._cache.stop()
        self._mac.stop()
        self._win.stop()

    def test_missing_security_command_returns_none(self):
        with patch.object(browser_cookies.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(browser_cookies._mac_key("Chrome Safe Storage"))

    def test_failure_is_cached(self):
        calls = []

        def boom(*args, **kwargs):
            calls.append(args)
            raise FileNotFoundError

        with patch.object(browser_cookies.subprocess, "run", side_effect=boom):
            self.assertIsNone(browser_cookies._mac_key("Brave Safe Storage"))
            self.assertIsNone(browser_cookies._mac_key("Brave Safe Storage"))
        self.assertEqual(len(calls), 1)

    def test_timeout_returns_none(self):
        with patch.object(browser_cookies.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("security", 5)):
            self.assertIsNone(browser_cookies._mac_key("Vivaldi Safe Storage"))

    def test_nonzero_exit_returns_none(self):
        proc = subprocess.CompletedProcess(["security"], 44, b"", b"not found")
        with patch.object(browser_cookies.subprocess, "run", return_value=proc):
            self.assertIsNone(browser_cookies._mac_key("Arc Safe Storage"))

    def test_derives_pbkdf2_key_from_keychain_password(self):
        proc = subprocess.CompletedProcess(["security"], 0, b"secret-password\n", b"")
        with patch.object(browser_cookies.subprocess, "run", return_value=proc):
            key = browser_cookies._mac_key("Chrome Safe Storage")
        self.assertEqual(key, mac_key())

    def test_service_name_inferred_from_profile_path(self):
        self.assertEqual(browser_cookies._mac_service_for(
            "/Users/x/Library/Application Support/Microsoft Edge/Default"),
            "Microsoft Edge Safe Storage")
        self.assertEqual(browser_cookies._mac_service_for(
            "/Users/x/Library/Application Support/BraveSoftware/Brave-Browser/Default"),
            "Brave Safe Storage")
        self.assertEqual(browser_cookies._mac_service_for(
            "/Users/x/Library/Application Support/Arc/User Data/Default"),
            "Arc Safe Storage")
        self.assertEqual(browser_cookies._mac_service_for(
            "/Users/x/Library/Application Support/Google/Chrome/Default"),
            "Chrome Safe Storage")

    def test_best_without_keychain_returns_none(self):
        """取不到密钥 + 库里全是密文 -> 没有可用来源，但绝不抛异常。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            profile = os.path.join(tmp, "Chrome", "Default")
            enc = mac_encrypt(b"MUSIC_U=deadbeef", mac_key())
            make_cookie_db(os.path.join(profile, "Network", "Cookies"),
                           [(".music.163.com", "MUSIC_U", "", enc)])
            with patch.object(browser_cookies.subprocess, "run", side_effect=FileNotFoundError), \
                    patch.object(browser_cookies, "chromium_profiles",
                                 return_value=[("Chrome", profile)]), \
                    patch.object(browser_cookies, "read_safari", return_value={}):
                self.assertIsNone(browser_cookies.best("netease"))

    def test_collect_reads_encrypted_cookies_with_keychain_password(self):
        """完整链路：钥匙串密码 -> PBKDF2 -> 解开库里的密文 -> 组装请求头。"""
        import tempfile

        proc = subprocess.CompletedProcess(["security"], 0, b"secret-password", b"")
        with tempfile.TemporaryDirectory() as tmp:
            profile = os.path.join(tmp, "Google", "Chrome", "Default")
            enc = mac_encrypt(b"abcdef0123456789", mac_key())
            make_cookie_db(os.path.join(profile, "Network", "Cookies"),
                           [(".music.163.com", "MUSIC_U", "", enc),
                            ("y.qq.com", "qm_keyst", "", b"")])
            with patch.object(browser_cookies.subprocess, "run", return_value=proc), \
                    patch.object(browser_cookies, "chromium_profiles",
                                 return_value=[("Chrome", profile)]), \
                    patch.object(browser_cookies, "read_safari", return_value={}):
                found = browser_cookies.collect("netease")
                best = browser_cookies.best("netease")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].cookies, {"MUSIC_U": "abcdef0123456789"})
        self.assertEqual(best.browser, "Chrome")
        self.assertEqual(browser_cookies.to_header(best.cookies), "MUSIC_U=abcdef0123456789")

    def test_collect_on_unsupported_platform_is_empty(self):
        with patch.object(browser_cookies, "IS_WIN", False), patch.object(browser_cookies, "IS_MAC", False):
            self.assertEqual(browser_cookies.collect("netease"), [])


class SafariTest(unittest.TestCase):
    """Safari 的私有二进制格式：能解析就解析，认不出就安静地返回空。"""

    @staticmethod
    def _build_binarycookies(records: list[tuple[str, str, str, str]]) -> bytes:
        """按 Apple 的布局拼一个最小文件，供解析器自测。"""
        # 注意：构造顺序必须与解析器的偏移约定一致（域/名/路径/值 在 16/20/24/28）
        body = bytearray()
        count = len(records)
        for domain, name, path, value in records:
            strings = b""
            offsets = []
            for text in (domain, name, path, value):
                offsets.append(48 + len(strings))      # 记录头 48 字节后紧接着字符串
                encoded = text.encode("utf-8") + b"\x00"
                strings += encoded
                while len(strings) % 4:                # 4 字节对齐，更贴近真实文件
                    strings += b"\x00"
            size = 48 + len(strings)
            record = bytearray()
            record += size.to_bytes(4, "big")          # 0  size
            record += (0x100).to_bytes(4, "big")       # 4  未知
            record += (0).to_bytes(4, "big")           # 8  标志
            record += (0).to_bytes(4, "big")           # 12 端口
            for off in offsets:                        # 16/20/24/28 四个字符串偏移
                record += off.to_bytes(4, "big")
            record += (0).to_bytes(8, "big")           # 32 过期时间
            record += (0).to_bytes(8, "big")           # 40 创建时间
            record += strings
            assert len(record) == size
            body += record
        page = bytearray(b"\x00\x00\x01\x00") + count.to_bytes(4, "big") + body
        header = bytearray(b"cook") + (1).to_bytes(4, "big") + len(page).to_bytes(4, "big")
        return bytes(header + page)

    def test_parse_valid_file(self):
        blob = self._build_binarycookies([
            (".music.163.com", "MUSIC_U", "/", "abcdef012345"),
            (".qq.com", "qm_keyst", "/", "zzz"),
        ])
        records = browser_cookies.parse_binarycookies(blob)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["name"], "MUSIC_U")
        self.assertEqual(records[0]["value"], "abcdef012345")
        self.assertEqual(records[0]["domain"], ".music.163.com")
        self.assertEqual(records[0]["path"], "/")

    def test_garbage_input_returns_empty(self):
        for blob in (b"", b"\x00" * 4, b"cook", b"cook\x00\x00\x00\x02junk",
                     b"not a cookie file at all", b"cook" + b"\xff" * 64):
            self.assertEqual(browser_cookies.parse_binarycookies(blob), [])

    def test_read_safari_with_garbage_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Cookies.binarycookies")
            with open(path, "wb") as fh:
                fh.write(b"totally not binarycookies")
            with patch.object(browser_cookies, "IS_MAC", True), \
                    patch.object(browser_cookies, "safari_cookie_path", return_value=path):
                result = browser_cookies.read_safari()
        self.assertIn(result, ({}, []))

    def test_read_safari_missing_file(self):
        with patch.object(browser_cookies, "IS_MAC", True), \
                patch.object(browser_cookies, "safari_cookie_path",
                             return_value=os.path.join("no", "such", "file")):
            self.assertEqual(browser_cookies.read_safari(), {})

    def test_read_safari_on_windows_is_empty(self):
        with patch.object(browser_cookies, "IS_MAC", False):
            self.assertEqual(browser_cookies.read_safari(), {})

    def test_read_safari_parses_valid_file(self):
        import tempfile

        blob = self._build_binarycookies([(".music.163.com", "MUSIC_U", "/", "tok")])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Cookies.binarycookies")
            with open(path, "wb") as fh:
                fh.write(blob)
            with patch.object(browser_cookies, "IS_MAC", True), \
                    patch.object(browser_cookies, "safari_cookie_path", return_value=path):
                jar = browser_cookies.read_safari()
        self.assertEqual(jar, {"MUSIC_U": "tok"})


class NeteaseMacTest(unittest.TestCase):
    """网易云客户端的 macOS 分支：试钥匙串 -> 当明文 -> 空表，逐级降级。"""

    def _client_dir(self, tmp: str, rows) -> str:
        client = os.path.join(tmp, "com.netease.163music")
        make_cookie_db(os.path.join(client, "Cookies"), rows)
        return client

    def test_plaintext_cookies_are_read(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = self._client_dir(tmp, [
                (".music.163.com", "MUSIC_U", "plain-token-123", b""),
                (".music.163.com", "__csrf", "", b""),
                (".example.com", "other", "nope", b""),
            ])
            with patch.object(ospaths, "IS_MAC", True), patch.object(ospaths, "IS_WIN", False), \
                    patch.object(browser_cookies, "IS_MAC", True), \
                    patch.object(browser_cookies, "_mac_key", return_value=None):
                jar = netease.decrypt_client_cookies(client)
        self.assertEqual(jar, {"MUSIC_U": "plain-token-123"})

    def test_encrypted_cookies_are_read_with_keychain_key(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = self._client_dir(tmp, [
                (".music.163.com", "MUSIC_U", "", mac_encrypt(b"enc-token", mac_key())),
            ])
            with patch.object(ospaths, "IS_MAC", True), patch.object(ospaths, "IS_WIN", False), \
                    patch.object(browser_cookies, "IS_MAC", True), \
                    patch.object(browser_cookies, "_mac_key", return_value=mac_key()):
                jar = netease.decrypt_client_cookies(client)
        self.assertEqual(jar, {"MUSIC_U": "enc-token"})

    def test_no_database_returns_empty_dict(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = os.path.join(tmp, "com.netease.163music")
            os.makedirs(client)
            with patch.object(ospaths, "IS_MAC", True), patch.object(ospaths, "IS_WIN", False):
                self.assertEqual(netease.decrypt_client_cookies(client), {})

    def test_find_client_dir_on_mac_skips_app_bundle(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "NeteaseMusic.app")
            os.makedirs(app)
            client = self._client_dir(tmp, [(".music.163.com", "MUSIC_U", "x", b"")])
            with patch.object(ospaths, "IS_MAC", True), patch.object(ospaths, "IS_WIN", False), \
                    patch.object(ospaths, "netease_client_dirs", return_value=[app, client]):
                self.assertEqual(netease.find_client_dir(), client)

    def test_find_client_dir_empty_when_nothing_installed(self):
        with patch.object(ospaths, "netease_client_dirs", return_value=[]):
            self.assertEqual(netease.find_client_dir(), "")

    def test_windows_branch_still_used_on_windows(self):
        """Windows 分支没被误改：目录里没有 LocalPrefs.json 就照旧报错。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ospaths, "IS_MAC", False), patch.object(ospaths, "IS_WIN", True):
                with self.assertRaises(netease.PlatformError):
                    netease.decrypt_client_cookies(tmp)

    def test_module_import_does_not_touch_win32(self):
        """netease 模块在非 Windows 上必须能 import（ctypes.wintypes 只能函数内 import）。"""
        import importlib
        import pathlib

        source = pathlib.Path(netease.__file__).read_text(encoding="utf-8")
        top_level = [line for line in source.splitlines()
                     if line.startswith(("import ", "from "))]
        self.assertFalse(any("ctypes" in line for line in top_level))
        importlib.reload(netease)          # 重载一次确认没有副作用
        self.assertTrue(hasattr(netease, "find_client_dir"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
