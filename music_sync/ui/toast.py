# -*- coding: utf-8 -*-
"""同步完成的右下角提示窗 + 明细窗。

注意：这个窗口必须由**独立进程**拉起（见 toast_runner.py）。
安全软件（如火绒的"弹窗拦截"）会干掉长时间运行、又去创建窗口的进程，
所以主流程只负责写结果 JSON，再由一个全新的 pythonw 进程来显示。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

BG = "#1e1f22"
BG2 = "#2b2d31"
FG = "#e8e8e8"
DIM = "#9aa0a6"
ACC = "#5aa9f0"
FONT = "Microsoft YaHei UI"
DEFAULT_TIMEOUT = 40

_result: dict = {}

PLATFORM_TITLE = {"netease": "网易云音乐", "qqmusic": "QQ音乐",
                  "kugou": "酷狗音乐", "soda": "汽水音乐"}


def _fonts():
    return (tkfont.Font(family=FONT, size=10),
            tkfont.Font(family=FONT, size=10, weight="bold"),
            tkfont.Font(family=FONT, size=9))


def _platform_name(key: str) -> str:
    return PLATFORM_TITLE.get(key, key or "音乐")


def _summary_lines(result: dict, timeout: int):
    new = result.get("new") or []
    moved = result.get("moved") or []
    grey = result.get("grey") or []
    failed = result.get("failed") or []
    lines = []
    if result.get("error"):
        lines.append(("同步失败：" + result["error"][:40], True))
    elif new:
        lines.append(("新增 %d 首" % len(new), True))
        for item in new[:4]:
            lines.append(("  · " + item, False))
        if len(new) > 4:
            lines.append(("  · …等 %d 首" % len(new), False))
    else:
        lines.append(("没有新增歌曲", True))
    if moved:
        lines.append(("归置 %d 个文件到曲库" % len(moved), False))
    if grey:
        lines.append(("无版权/下架 %d 首" % len(grey), False))
    if failed:
        lines.append(("下载失败 %d 首" % len(failed), False))
    return lines


def show(result: dict, timeout: int = DEFAULT_TIMEOUT) -> None:
    """显示提示窗，阻塞直到关闭或倒计时结束。"""
    global _result
    _result = result
    timeout = max(5, int(timeout or DEFAULT_TIMEOUT))
    root = tk.Tk()
    root.title("%s 同步" % _platform_name(result.get("platform", "")))
    root.overrideredirect(True)
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    root.configure(bg=BG)

    f_n, f_b, f_s = _fonts()
    width, pad = 400, 14

    outer = tk.Frame(root, bg=ACC, padx=1, pady=1)
    outer.pack(fill="both", expand=True)
    box = tk.Frame(outer, bg=BG, padx=pad, pady=12)
    box.pack(fill="both", expand=True)

    head = tk.Frame(box, bg=BG)
    head.pack(fill="x")
    tk.Label(head, text="%s ↔ 本地曲库 同步完成" % _platform_name(result.get("platform", "")),
             bg=BG, fg=FG, font=f_b, anchor="w").pack(side="left")
    countdown = tk.Label(head, text="%ds" % timeout, bg=BG, fg=DIM, font=f_s, anchor="e")
    countdown.pack(side="right")

    body = tk.Frame(box, bg=BG)
    body.pack(fill="x", pady=(8, 10))
    for text, bold in _summary_lines(result, timeout):
        tk.Label(body, text=text, bg=BG, fg=(FG if bold else DIM),
                 font=(f_n if bold else f_s), anchor="w", justify="left",
                 wraplength=width - 2 * pad - 12).pack(fill="x")

    foot = tk.Frame(box, bg=BG)
    foot.pack(fill="x")
    button = tk.Label(foot, text="  查看明细  ", bg=ACC, fg="#10131a", font=f_b,
                      padx=8, pady=5, cursor="hand2")
    button.pack(side="left")
    tk.Label(foot, text="点我或 %d 秒后自动关闭" % timeout, bg=BG, fg=DIM,
             font=f_s).pack(side="right")

    root.update_idletasks()
    height = root.winfo_reqheight()
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("%dx%d+%d+%d" % (width, height, screen_w - width - 20, screen_h - height - 80))

    state = {"left": timeout, "open": False}

    def close(*_):
        try:
            root.destroy()
        except tk.TclError:
            pass

    def open_details(*_):
        if state["open"]:
            return
        state["open"] = True
        details(root, result)
        try:
            root.withdraw()
        except tk.TclError:
            pass

    def tick():
        if state["open"]:
            return
        state["left"] -= 1
        if state["left"] <= 0:
            close()
            return
        countdown.config(text="%ds" % state["left"])
        root.after(1000, tick)

    for widget in (root, outer, box, head, body, foot, button):
        widget.bind("<Button-1>", open_details)
    for child in body.winfo_children():
        child.bind("<Button-1>", open_details)

    root.after(1000, tick)
    root.mainloop()


def details(parent, result: dict) -> None:
    """同步明细窗（可滚动）。"""
    win = tk.Toplevel(parent)
    win.title("同步明细")
    win.configure(bg=BG)
    win.geometry("640x540")
    try:
        win.attributes("-topmost", True)
    except tk.TclError:
        pass
    f_n, f_b, f_s = _fonts()

    top = tk.Frame(win, bg=BG, padx=16, pady=12)
    top.pack(fill="x")
    tk.Label(top, text="本次同步明细", bg=BG, fg=FG,
             font=tkfont.Font(family=FONT, size=12, weight="bold"), anchor="w").pack(fill="x")
    pname = str(result.get("playlist_name") or "").strip()
    ptext = "歌单「%s」%s 首" % (pname, result.get("playlist", "-")) if pname else "歌单 %s 首" % result.get("playlist", "-")
    tk.Label(top, text="%s  ·  %s / 本地文件 %s 个 / 匹配 %s 对" % (
        result.get("time", ""), ptext,
        result.get("files", "-"), result.get("matched", "-")),
        bg=BG, fg=DIM, font=f_s, anchor="w").pack(fill="x", pady=(4, 0))

    wrap = tk.Frame(win, bg=BG2, padx=12, pady=8)
    wrap.pack(fill="both", expand=True, padx=16, pady=(0, 12))
    scroll = tk.Scrollbar(wrap, bg=BG2, activebackground=ACC, troughcolor=BG, bd=0)
    scroll.pack(side="right", fill="y")
    text = tk.Text(wrap, bg=BG2, fg=FG, font=f_n, bd=0, highlightthickness=0,
                   wrap="word", yscrollcommand=scroll.set, insertbackground=FG)
    text.pack(side="left", fill="both", expand=True)
    scroll.config(command=text.yview)

    def section(title: str, items: list[str]) -> None:
        if not items:
            return
        text.insert("end", "\n%s（%d）\n" % (title, len(items)), ("h",))
        for item in items:
            text.insert("end", "    " + item + "\n", ("i",))

    text.tag_config("h", foreground=ACC, font=f_b, spacing1=6, spacing3=4)
    text.tag_config("i", foreground=FG, font=f_n, lmargin1=8, lmargin2=8)
    text.insert("end", "%s「歌单」→ 本地曲库\n" % _platform_name(result.get("platform", "")), ("h",))
    if result.get("error"):
        text.insert("end", "\n" + result["error"] + "\n", ("i",))
    section("新增下载", result.get("new") or [])
    section("归置到曲库", result.get("moved") or [])
    section("无版权/下架", result.get("grey") or [])
    section("下载失败", result.get("failed") or [])
    if not any(result.get(k) for k in ("new", "grey", "failed", "error")):
        text.insert("end", "\n本次没有变化，一切已同步。\n", ("i",))
    text.config(state="disabled")

    tk.Label(top, text="关闭窗口即退出", bg=BG, fg=DIM, font=f_s, anchor="e").pack(fill="x")

    def on_close(*_):
        try:
            win.destroy()
            parent.destroy()
        except tk.TclError:
            pass

    win.protocol("WM_DELETE_WINDOW", on_close)
