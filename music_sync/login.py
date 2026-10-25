# -*- coding: utf-8 -*-
"""登录态获取（面向"不懂 cookie 是什么"的普通用户）。

优先级：
1. 平台官方客户端已经登录过 → 直接复用它的登录态（网易云）；
2. 浏览器里登录过该平台网页 → 从浏览器的 cookie 里复用；
3. 用户手动填过 cookie → 用配置里的。

对外只暴露三个函数：``status`` / ``auto`` / ``report``，GUI 和 CLI 共用。
所有提示文案都是中文人话，不出现 cookie 术语。
"""
from __future__ import annotations

import os

from . import browser_cookies, config as cfgmod
from .platforms import PLATFORMS, resolve
from .util import get_logger

# 平台 -> 客户端可执行文件常见位置（判断"装了没装"）
CLIENT_HINTS = {
    "netease": [r"C:\Program Files\NetEase\CloudMusic\cloudmusic.exe",
                r"C:\Program Files (x86)\NetEase\CloudMusic\cloudmusic.exe"],
    "qqmusic": [r"C:\Program Files\Tencent\QQMusic\QQMusic.exe",
                r"C:\Program Files (x86)\Tencent\QQMusic\QQMusic.exe"],
    "kugou": [r"C:\Program Files\Kugou\KGMusic\KuGou.exe",
              r"C:\Program Files (x86)\Kugou\KGMusic\KuGou.exe"],
    "soda": [],
}


def client_installed(platform: str) -> bool:
    return any(os.path.exists(p) for p in CLIENT_HINTS.get(platform, []))


def _netease_client_cookies() -> dict[str, str]:
    from .platforms import netease as nt
    try:
        client_dir = nt.find_client_dir()
        if not client_dir:
            return {}
        return nt.decrypt_client_cookies(client_dir) or {}
    except Exception as exc:
        get_logger().debug("读取网易云客户端登录态失败: %s", exc)
        return {}


def _platform_cfg(cfg: dict, platform: str) -> dict:
    return (cfg.get("platforms") or {}).setdefault(platform, {})


def status(platform: str, cfg: dict) -> tuple[bool, str]:
    """(是否已登录, 人话说明)。不联网，只看本地能不能拿到登录态。"""
    key = resolve(platform)
    title = PLATFORMS.get(key, (key, None))[0]
    if not PLATFORMS.get(key, (key, False))[1]:
        return False, "%s 暂不支持自动下载（平台没有公开接口），可以只用来做歌单对照。" % title

    stored = (_platform_cfg(cfg, key).get("cookie") or "").strip()
    if stored:
        return True, "已配置登录状态（来自设置）"
    if key == "netease":
        if _netease_client_cookies():
            return True, "已从网易云客户端自动读取登录状态"
    src = browser_cookies.best(key)
    if src:
        return True, "已从 %s 浏览器自动读取登录状态" % src.browser
    if key == "netease" and client_installed(key):
        return False, "网易云客户端已安装但还没登录，请打开客户端登录一次"
    return False, "还没登录：请用浏览器打开 %s 网页登录一次，再来这里点「自动获取」" % title


def auto(platform: str, cfg: dict, save: bool = True,
         config_path: str | None = None) -> tuple[bool, str]:
    """自动找登录态；找到就（可选）写进配置文件。返回 (成功?, 中文说明)。"""
    key = resolve(platform)
    if not PLATFORMS.get(key, (key, False))[1]:
        return False, "该平台暂不支持自动下载"

    cookies: dict[str, str] = {}
    source = ""
    if key == "netease":
        cookies = _netease_client_cookies()
        if cookies:
            source = "网易云客户端"
    if not cookies:
        src = browser_cookies.best(key)
        if src:
            cookies = src.cookies
            source = "从 %s 浏览器" % src.browser
    if not cookies:
        ok, why = status(key, cfg)
        return False, why

    header = browser_cookies.to_header(cookies)
    _platform_cfg(cfg, key)["cookie"] = header
    # 一些平台还需要 uin 之类的附加字段
    if key == "qqmusic" and "uin" in cookies:
        _platform_cfg(cfg, key)["uin"] = cookies["uin"]
    if key == "kugou" and "kg_mid" in cookies:
        _platform_cfg(cfg, key)["dfid"] = cookies.get("kg_dfid", "")

    if save:
        path = config_path or cfgmod.config_path()
        try:
            cfgmod.save(cfg, path)
        except Exception as exc:
            return True, "已获取登录状态（%s），但写配置文件失败：%s" % (
                source or "未知来源", exc)
    count = len([p for p in header.split("; ") if p])
    return True, "已获取登录状态（%s，%d 项）" % (source or "未知来源", count)


def report(cfg: dict) -> list[tuple[str, str, str]]:
    """[(平台名, 状态符号, 说明), …] —— 给自检/设置界面列表用。"""
    rows: list[tuple[str, str, str]] = []
    for key, (title, supported) in PLATFORMS.items():
        if not supported:
            rows.append((title, "占位", "平台没有公开接口，仅支持歌单对照"))
            continue
        ok, why = status(key, cfg)
        rows.append((title, "已登录" if ok else "未登录", why))
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="music-sync login",
                                     description="获取各音乐平台的登录状态（不用手动填 cookie）")
    parser.add_argument("platform", nargs="?", help="平台：netease/qqmusic/kugou/soda，留空=全部")
    parser.add_argument("--config", default=None)
    parser.add_argument("--no-save", action="store_true", help="只检测，不写入配置文件")
    args = parser.parse_args(argv)

    cfg = cfgmod.load(args.config)
    if args.platform:
        ok, msg = auto(args.platform, cfg, save=not args.no_save,
                       config_path=args.config or cfgmod.config_path())
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1

    for title, state, detail in report(cfg):
        print("[%s] %s —— %s" % (state, title, detail))
    return 0
