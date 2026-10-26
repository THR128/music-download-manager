# -*- coding: utf-8 -*-
"""把本项目上传到 GitHub（全程走 api.github.com，不需要 git push / gh）。

之所以不用 ``git push``：部分网络环境下 ``github.com`` 的 TLS 握手会被阻断，
而 ``api.github.com`` 仍可用。本脚本只用 Git Data API 一次性建出「一个提交」，
比逐个文件调 contents 接口干净得多。

用法::

    set GITHUB_TOKEN=github_pat_xxx
    python scripts/publish_github.py --repo music-download-manager
    python scripts/publish_github.py --repo music-download-manager --dry-run

需要的令牌权限（细粒度令牌）：Repository permissions → Contents: Read and write，
Account permissions → 勾选「创建仓库」相关的 Administration（或直接用 classic 令牌勾 repo）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist", "pylibs", ".pytest_cache", ".idea"}
SKIP_FILES = {"config.json", "last_result.json"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".part", ".zip", ".exe"}
SKIP_SUFFIX = (".log.1", ".egg-info")

DEFAULT_DESC = (
    "音乐下载自动化管理：把网易云 / QQ音乐 / 酷狗的「我喜欢」歌单自动补齐到本地曲库，"
    "支持定时检查、多平台换源、音质选择与结果提示窗。"
)
DEFAULT_TOPICS = ["music", "netease", "qqmusic", "kugou", "mp3", "windows", "tkinter", "python"]


def _version() -> str:
    sys.path.insert(0, ROOT)
    try:
        from music_sync import __version__
        return __version__
    except Exception:
        return "dev"


def _opener():
    """走本机加速代理（如 SteamCommunity 302）时会被中间人解密，需要关掉证书校验。

    设置环境变量 ``GITHUB_VERIFY=1`` 可以强制开启校验（直连时建议开）。
    """
    if os.environ.get("GITHUB_VERIFY") == "1":
        return urllib.request.build_opener()
    import ssl
    ctx = ssl._create_unverified_context()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


_OPENER = _opener()


def _request(method: str, path: str, token: str, payload=None) -> tuple[int, dict]:
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "music-sync-publish")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"message": body[:400]}
        return exc.code, parsed


def collect_files() -> list[str]:
    picked: list[str] = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in files:
            rel = os.path.relpath(os.path.join(base, name), ROOT).replace("\\", "/")
            if name in SKIP_FILES or name.startswith("."):
                if name not in (".gitignore",):
                    continue
            if os.path.splitext(name)[1].lower() in SKIP_EXT or name.endswith(SKIP_SUFFIX):
                continue
            picked.append(rel)
    return sorted(picked)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 GitHub API 发布本项目")
    parser.add_argument("--repo", default="music-download-manager", help="仓库名")
    parser.add_argument("--owner", default="", help="账号名（默认取令牌所属用户）")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--description", default=DEFAULT_DESC)
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要上传的文件")
    parser.add_argument("--message", default="", help="提交说明（默认带版本号）")
    args = parser.parse_args(argv)

    files = collect_files()
    total = sum(os.path.getsize(os.path.join(ROOT, f)) for f in files)
    print("待上传 %d 个文件，共 %.1f KB" % (len(files), total / 1024))
    for rel in files:
        print("  " + rel)
    if args.dry_run:
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        print("缺少 GITHUB_TOKEN 环境变量", file=sys.stderr)
        return 2

    status, me = _request("GET", "/user", token)
    if status != 200:
        print("令牌无效：%s" % me.get("message"), file=sys.stderr)
        return 2
    owner = args.owner or me["login"]
    print("账号：%s（%s）" % (owner, me.get("name") or ""))

    # 1) 建仓库（已存在就复用）
    status, repo = _request("GET", "/repos/%s/%s" % (owner, args.repo), token)
    if status == 200:
        print("仓库已存在：%s" % repo["html_url"])
    elif status == 404:
        status, repo = _request("POST", "/user/repos", token, {
            "name": args.repo,
            "description": args.description,
            "private": bool(args.private),
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,
        })
        if status not in (200, 201):
            print("建仓库失败：%s" % repo.get("message"), file=sys.stderr)
            return 1
        print("已创建仓库：%s" % repo["html_url"])
    else:
        print("查询仓库失败：%s" % repo.get("message"), file=sys.stderr)
        return 1

    # 2) 空仓库得先有一个提交，否则 Git Data API 会拒绝（"Git Repository is empty"）
    status, _ = _request("GET", "/repos/%s/%s/git/ref/heads/%s" % (owner, args.repo, args.branch),
                         token)
    if status != 200:
        status, boot = _request("PUT", "/repos/%s/%s/contents/README.md" % (owner, args.repo), token, {
            "message": "initial commit",
            "content": base64.b64encode(b"# bootstrap\n").decode(),
            "branch": args.branch,
        })
        if status not in (200, 201):
            print("初始化仓库失败：%s" % boot.get("message"), file=sys.stderr)
            return 1
        print("已初始化空仓库（占位提交，稍后会被正式内容覆盖）")

    # 3) 上传 blob
    tree_items = []
    for rel in files:
        with open(os.path.join(ROOT, rel), "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        status, blob = _request("POST", "/repos/%s/%s/git/blobs" % (owner, args.repo),
                                token, {"content": content, "encoding": "base64"})
        if status not in (200, 201):
            print("上传 %s 失败：%s" % (rel, blob.get("message")), file=sys.stderr)
            return 1
        tree_items.append({"path": rel, "mode": "100755" if rel.endswith(".sh") else "100644",
                           "type": "blob", "sha": blob["sha"]})
        print("  ok %s" % rel)

    # 3) 分支是否已存在
    status, ref = _request("GET", "/repos/%s/%s/git/ref/heads/%s" % (owner, args.repo, args.branch),
                           token)
    parent_sha = ""
    base_tree = None
    if status == 200:
        parent_sha = ref["object"]["sha"]
        status, commit = _request("GET", "/repos/%s/%s/git/commits/%s" % (owner, args.repo, parent_sha),
                                 token)
        if status == 200:
            base_tree = commit["tree"]["sha"]

    tree_payload = {"tree": tree_items}
    # 刻意不传 base_tree：远端 tree 与本地文件列表完全一致，
    # 这样本地已删除的文件（比如误传的缓存目录）会随提交一起清掉。
    status, tree = _request("POST", "/repos/%s/%s/git/trees" % (owner, args.repo), token, tree_payload)
    if status not in (200, 201):
        print("建 tree 失败：%s" % tree.get("message"), file=sys.stderr)
        return 1

    message = args.message or ("音乐下载自动化管理 v%s" % _version())
    commit_payload = {"message": message, "tree": tree["sha"]}
    if parent_sha:
        commit_payload["parents"] = [parent_sha]
    status, commit = _request("POST", "/repos/%s/%s/git/commits" % (owner, args.repo), token,
                             commit_payload)
    if status not in (200, 201):
        print("建提交失败：%s" % commit.get("message"), file=sys.stderr)
        return 1

    if parent_sha:
        status, _ = _request("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (owner, args.repo, args.branch),
                             token, {"sha": commit["sha"], "force": True})
    else:
        status, _ = _request("POST", "/repos/%s/%s/git/refs" % (owner, args.repo), token,
                             {"ref": "refs/heads/" + args.branch, "sha": commit["sha"]})
    if status not in (200, 201):
        print("更新分支失败", file=sys.stderr)
        return 1

    _request("PATCH", "/repos/%s/%s" % (owner, args.repo), token, {"topics": DEFAULT_TOPICS})
    print("\n完成：%s/tree/%s" % (repo["html_url"], args.branch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

