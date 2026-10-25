# -*- coding: utf-8 -*-
"""跨终端环境自检。

目标：换一台电脑跑之前，先知道"哪些坑在这台机器上会踩"。
每项检查返回 ok / warn / fail，fail 会以非零退出码结束自检。
"""
from __future__ import annotations

import glob
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass

from . import config as cfgmod
from . import platforms

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _add(items: list[Check], name: str, status: str, detail: str) -> None:
    items.append(Check(name, status, detail))


def check_python(items: list[Check]) -> None:
    version = sys.version_info
    text = "%d.%d.%d" % (version[:3])
    if version < (3, 9):
        _add(items, "Python 版本", FAIL, f"{text} 太旧，需要 3.9+")
    else:
        _add(items, "Python 版本", OK, f"{text}（{platform.machine()}）")

    exe = sys.executable or ""
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.name == "nt" and not os.path.isfile(pyw):
        _add(items, "pythonw.exe", WARN, f"没找到 {pyw}，计划任务弹窗会闪黑窗")
    elif os.name == "nt":
        _add(items, "pythonw.exe", OK, pyw)


def check_deps(items: list[Check]) -> None:
    for module, pkg in (("requests", "requests"), ("cryptography", "cryptography")):
        try:
            __import__(module)
            _add(items, f"依赖 {pkg}", OK, "已安装")
        except ImportError:
            _add(items, f"依赖 {pkg}", FAIL, f"未安装，执行：pip install {pkg}")


def check_system(items: list[Check]) -> None:
    if os.name != "nt":
        _add(items, "操作系统", WARN, f"{platform.system()}"
                                       "：本工具的计划任务/客户端 cookie 解密仅支持 Windows")
    else:
        _add(items, "操作系统", OK, f"{platform.system()} {platform.release()} {platform.version()}")
    _add(items, "文件名编码", OK if sys.getfilesystemencoding().lower() in ("utf-8", "utf8")
         else WARN, sys.getfilesystemencoding())


def check_config(items: list[Check], cfg: dict, path: str) -> None:
    if os.path.isfile(path):
        _add(items, "配置文件", OK, path)
    else:
        _add(items, "配置文件", WARN, f"{path} 不存在，将使用默认值（运行 settings 可生成）")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        probe = path + ".probe"
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        _add(items, "配置可写", OK, os.path.dirname(path) or ".")
    except OSError as exc:
        _add(items, "配置可写", FAIL, f"{os.path.dirname(path)}：{exc}")

    library = cfg.get("library_dir") or ""
    if not library:
        _add(items, "曲库目录", FAIL, "未配置 library_dir")
        return
    try:
        os.makedirs(library, exist_ok=True)
        usage = shutil.disk_usage(library)
        free_gb = usage.free / 1024 ** 3
        count = len([n for n in os.listdir(library)
                     if n.lower().endswith((".mp3", ".flac", ".m4a", ".wav", ".ape", ".ogg"))])
        status = OK if free_gb > 1 else WARN
        _add(items, "曲库目录", status, f"{library}（已有 {count} 个音频，剩余 {free_gb:.1f} GB）")
    except OSError as exc:
        _add(items, "曲库目录", FAIL, f"{library}：{exc}")

    client_dir = cfg.get("client_download_dir") or ""
    if client_dir:
        if os.path.isdir(client_dir):
            try:
                pending = len([n for n in os.listdir(client_dir)
                               if n.lower().endswith((".mp3", ".flac", ".m4a"))])
                _add(items, "客户端下载目录", OK, f"{client_dir}（待归置 {pending} 个）")
            except OSError as exc:
                _add(items, "客户端下载目录", WARN, f"{client_dir}：{exc}")
        else:
            _add(items, "客户端下载目录", WARN, f"{client_dir} 不存在（不归置也能正常同步）")


def check_clients(items: list[Check]) -> None:
    local = os.environ.get("LOCALAPPDATA", "")
    known = {
        "网易云音乐": [os.path.join(local, "NetEase", "CloudMusic")],
        "QQ音乐": [r"C:\Program Files\Tencent\QQMusic", r"C:\Program Files (x86)\Tencent\QQMusic"],
        "酷狗音乐": [r"C:\Program Files\KuGou", r"C:\Program Files (x86)\KuGou"],
    }
    for name, dirs in known.items():
        hit = next((d for d in dirs if os.path.isdir(d)), "")
        if hit:
            _add(items, f"客户端 {name}", OK, hit)
        else:
            _add(items, f"客户端 {name}", WARN, "未安装（只影响自动取 cookie，不影响手填 cookie）")


def check_network(items: list[Check]) -> None:
    hosts = {
        "netease": ("music.163.com", 443),
        "qqmusic": ("u.y.qq.com", 443),
        "kugou": ("wwwapi.kugou.com", 443),
    }
    for key, (host, port) in hosts.items():
        try:
            with socket.create_connection((host, port), timeout=5):
                _add(items, f"网络 {host}", OK, f"{host}:{port} 可达")
        except OSError as exc:
            _add(items, f"网络 {host}", WARN, f"{host}:{port} 连不上（{exc}）")


def check_platforms(items: list[Check], cfg: dict) -> None:
    primary = platforms.resolve(cfg.get("platform", "netease"))
    for key, (title, can_download) in platforms.PLATFORMS.items():
        try:
            adapter = platforms.create(key, cfg.get("platforms", {}).get(key) or {})
        except Exception as exc:                     # noqa: BLE001 - 适配器缺依赖也要能自检
            _add(items, f"平台 {title}", FAIL, f"适配器加载失败：{exc}")
            continue
        if not can_download:
            _add(items, f"平台 {title}", WARN, "占位适配器：没有公开 API，不能下载")
            continue
        ok, message = adapter.check_login()
        starred = "（当前平台）" if key == primary else ""
        _add(items, f"平台 {title}{starred}", OK if ok else WARN, message)


def check_task(items: list[Check], cfg: dict) -> None:
    if os.name != "nt":
        return
    name = (cfg.get("schedule") or {}).get("task_name", "MusicSync")
    try:
        proc = subprocess.run(["schtasks", "/Query", "/TN", name],
                              capture_output=True, text=True, timeout=15,
                              encoding="gbk", errors="ignore")
    except (OSError, subprocess.SubprocessError) as exc:
        _add(items, "计划任务", WARN, f"查询失败：{exc}")
        return
    if proc.returncode != 0:
        _add(items, "计划任务", WARN, f"没有名为 {name} 的任务（可用 scripts/install_task.ps1 注册）")
        return
    next_run = ""
    for line in proc.stdout.splitlines():
        if "Next Run Time" in line or "下次运行时间" in line:
            next_run = line.split(":", 1)[-1].strip()
    _add(items, "计划任务", OK, f"{name} 已注册" + (f"，下次 {next_run}" if next_run else ""))


def check_gui(items: list[Check]) -> None:
    try:
        import tkinter  # noqa: F401
        _add(items, "图形界面 tkinter", OK, "可用")
    except ImportError as exc:
        _add(items, "图形界面 tkinter", FAIL, f"不可用：{exc}（设置界面和提示窗会失效）")

    if os.name != "nt":
        return
    huorong = glob.glob(r"D:\Huorong\Sysdiag\bin\PopBlock.exe") + glob.glob(
        r"C:\Program Files*\Huorong\Sysdiag\bin\PopBlock.exe")
    running = False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PopBlock.exe"],
                             capture_output=True, text=True, timeout=10,
                             encoding="gbk", errors="ignore").stdout
        running = "PopBlock" in out
    except (OSError, subprocess.SubprocessError):
        pass
    if huorong or running:
        _add(items, "弹窗拦截（火绒）", WARN,
             "检测到火绒弹窗拦截：请把 pythonw.exe 加入「弹窗拦截」白名单，否则结果提示窗会被杀")
    else:
        _add(items, "弹窗拦截", OK, "未检测到火绒弹窗拦截")


def run_checks(cfg: dict | None = None, path: str | None = None) -> list[Check]:
    path = cfgmod.config_path(path)
    cfg = cfg if cfg is not None else cfgmod.load(path)
    items: list[Check] = []
    check_python(items)
    check_deps(items)
    check_system(items)
    check_config(items, cfg, path)
    check_clients(items)
    check_network(items)
    check_platforms(items, cfg)
    check_task(items, cfg)
    check_gui(items)
    return items


def format_text(checks: list[Check]) -> str:
    lines = []
    for item in checks:
        lines.append(f"{MARK.get(item.status, '[????]')} {item.name}：{item.detail}")
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    lines.append(f"共 {len(checks)} 项：{len(checks) - fails - warns} 通过 / {warns} 警告 / {fails} 失败")
    return "\n".join(lines)


def main(cfg: dict | None = None, path: str | None = None) -> int:
    checks = run_checks(cfg, path)
    print(format_text(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
