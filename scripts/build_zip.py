# -*- coding: utf-8 -*-
"""把项目打成一个带版本号的发布压缩包（给开发者日志站 / GitHub Release 用）。

包里顶层目录名带版本号，例如 ``music-download-manager-v1.2.0/``，解压即可运行。

用法::

    python scripts/build_zip.py                 # 版本号取 music_sync.__version__
    python scripts/build_zip.py --out D:\\tmp
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from music_sync import __version__  # noqa: E402

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist", "pylibs", ".idea", ".pytest_cache"}
SKIP_FILES = {"config.json", "last_result.json", "thumbs.db"}
SKIP_EXT = {".pyc", ".pyo", ".log", ".part", ".zip", ".exe"}


def should_skip(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    if name.lower() in SKIP_FILES:
        return True
    return os.path.splitext(name)[1].lower() in SKIP_EXT


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="打包发布压缩包")
    parser.add_argument("--out", default=os.environ.get("TEMP", "."), help="输出目录")
    parser.add_argument("--version", default=__version__, help="版本号")
    args = parser.parse_args(argv)

    top = "music-download-manager-v%s" % args.version
    out = os.path.join(args.out, top + ".zip")
    os.makedirs(args.out, exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                path = os.path.join(base, name)
                rel = os.path.relpath(path, ROOT)
                if should_skip(rel):
                    continue
                zf.write(path, top + "/" + rel.replace("\\", "/"))
                count += 1
    print("已打包 %d 个文件 -> %s（%.0f KB）" % (
        count, out, os.path.getsize(out) / 1024.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
