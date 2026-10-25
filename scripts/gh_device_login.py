# -*- coding: utf-8 -*-
"""GitHub 设备码登录（等价于 ``gh auth login``，但不需要装 gh）。

流程：
1. 本脚本向 GitHub 申请一个设备码；
2. 你在浏览器打开它给的网址、输入它给的 8 位码，点授权；
3. 脚本轮询到令牌后写进 ``%USERPROFILE%\\.music-sync\\github_token``（不放进仓库）。

用法::

    python scripts/gh_device_login.py            # 申请设备码并等待授权
    python scripts/gh_device_login.py --scope repo,workflow
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

#: GitHub CLI 的 OAuth client_id（公开值，已开启设备流）
CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"


def token_path() -> str:
    base = os.environ.get("MUSIC_SYNC_HOME") or os.path.join(os.path.expanduser("~"), ".music-sync")
    return os.path.join(base, "github_token")


def _opener():
    if os.environ.get("GITHUB_VERIFY") == "1":
        return urllib.request.build_opener()
    ctx = ssl._create_unverified_context()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def _post(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Accept": "application/json",
                                          "User-Agent": "music-sync-device-login"})
    with _opener().open(req, timeout=60) as resp:
        return json.load(resp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GitHub 设备码登录")
    parser.add_argument("--scope", default="repo")
    parser.add_argument("--save", action="store_true", default=True)
    args = parser.parse_args(argv)

    start = _post(DEVICE_CODE_URL, {"client_id": CLIENT_ID, "scope": args.scope})
    if "device_code" not in start:
        print("申请设备码失败：%s" % start, file=sys.stderr)
        return 1

    print("=" * 58)
    print("请在浏览器打开：%s" % start["verification_uri"])
    print("输入代码：%s" % start["user_code"])
    print("=" * 58)
    print("（等你在浏览器里点完授权，我这边自动继续）", flush=True)

    interval = max(5, int(start.get("interval", 5)))
    deadline = time.time() + int(start.get("expires_in", 900))
    device_code = start["device_code"]
    while time.time() < deadline:
        time.sleep(interval)
        try:
            result = _post(TOKEN_URL, {
                "client_id": CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
        except Exception as exc:  # 网络抖动就继续等
            print("轮询出错，继续等：%s" % exc, file=sys.stderr)
            continue
        token = result.get("access_token")
        if token:
            if args.save:
                path = token_path()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(token)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                print("已保存令牌到 %s" % path)
            print("TOKEN=" + token)
            return 0
        error = result.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        print("授权失败：%s %s" % (error, result.get("error_description", "")), file=sys.stderr)
        return 1
    print("设备码已过期，请重新运行", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
