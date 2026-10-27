# -*- coding: utf-8 -*-
"""启动设置界面、按窗口真实边界截图，然后关闭窗口（给 README 配图用）。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

from PIL import ImageGrab

ROOT = r"C:\Users\Administrator\Documents\deepseek-harness\default-workspace\music-sync"
PY = sys.executable
TITLE = "音乐下载自动化管理 · 设置"
OUT = os.path.join(ROOT, "docs", "settings.png")

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


def find_window(title: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if title in buf.value:
                found.append(hwnd)
            return True

        user32.EnumWindows(_cb, 0)
        if found:
            return found[0]
        time.sleep(0.4)
    return None


def main() -> int:
    proc = subprocess.Popen([PY, "-m", "music_sync", "--config", os.path.join(ROOT, "config.json"),
                             "settings"], cwd=ROOT)
    hwnd = find_window(TITLE)
    if not hwnd:
        proc.kill()
        print("没找到设置窗口")
        return 1
    # 窗口位置可用参数调整，默认放右上角，避开桌面左下角的宠物挂件
    pos_x = int(sys.argv[1]) if len(sys.argv) > 1 else 1120
    pos_y = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    user32.SetWindowPos(hwnd, 0, pos_x, pos_y, 0, 0, 0x0001 | 0x0004)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.5)

    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    box = (rect.left, rect.top, rect.right, rect.bottom)
    print("窗口边界：", box)

    img = ImageGrab.grab(bbox=box, all_screens=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT)
    print("已保存 %s（%dx%d）" % (OUT, img.width, img.height))

    user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
    time.sleep(1.0)
    if proc.poll() is None:
        proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
