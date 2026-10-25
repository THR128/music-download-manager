# -*- coding: utf-8 -*-
"""设置界面（tkinter，单窗口）。

入口：``run(config_path=None) -> int``，也支持 ``python -m music_sync settings``。
设计取舍：

* 只用标准库 tkinter，装完 Python 就能用，不拖第三方 UI 依赖；
* 所有控件与 config.json 双向绑定，保存后重开界面必须回显原值；
* 登录态检测、自检都在后台线程跑，结果用 ``after()`` 回填，界面不卡；
* 保存前做基本校验，校验失败只弹窗、不关窗口，避免用户白填一遍。
"""
from __future__ import annotations

import copy
import os
import re
import subprocess
import sys
import threading
import unicodedata

# --- 直接 `python music_sync\ui\settings.py` 运行时，把项目根塞进 sys.path ---
if __package__ in (None, ""):                       # pragma: no cover - 仅直接运行时走到
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(os.path.dirname(_HERE))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from music_sync import config as config_mod
from music_sync.platforms import PLATFORMS, create
from music_sync.util import LOG_NAME, get_logger

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

log = get_logger()

# ---------------------------------------------------------------- 常量

WINDOW_TITLE = "音乐下载自动化管理 · 设置"
WINDOW_SIZE = "760x640"
APP_NAME = "音乐下载自动化管理"
APP_VERSION = "1.1.0"
FONT_UI = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 10, "bold")
FONT_HINT = ("Microsoft YaHei UI", 8)

#: 音质档位：key 写进 config，中文名给用户看
QUALITIES: list[tuple[str, str]] = [
    ("320", "320 kbps 高品质"),
    ("192", "192 kbps 标准"),
    ("128", "128 kbps 流畅"),
    ("lossless", "无损（仅部分平台）"),
]
QUALITY_KEYS = [k for k, _ in QUALITIES]
QUALITY_LABELS = {k: label for k, label in QUALITIES}
QUALITY_BY_LABEL = {label: k for k, label in QUALITIES}

WEEKDAYS: list[tuple[str, str]] = [
    ("monday", "周一"), ("tuesday", "周二"), ("wednesday", "周三"),
    ("thursday", "周四"), ("friday", "周五"), ("saturday", "周六"),
    ("sunday", "周日"),
]
WEEKDAY_LABELS = {k: label for k, label in WEEKDAYS}
WEEKDAY_BY_LABEL = {label: k for k, label in WEEKDAYS}

MODES: list[tuple[str, str]] = [
    ("weekly", "每周固定一天"),
    ("daily", "每天"),
    ("hours", "每隔 N 小时"),
]
MODE_LABELS = {k: label for k, label in MODES}
MODE_BY_LABEL = {label: k for k, label in MODES}

PLATFORM_LABELS = {key: info[0] for key, info in PLATFORMS.items()}
PLATFORM_BY_LABEL = {info[0]: key for key, info in PLATFORMS.items()}
PLATFORM_DOWNLOADABLE = {key: info[1] for key, info in PLATFORMS.items()}

#: 能拿到登录态的平台（汽水没有公开接口，登录按钮直接置灰）
LOGINABLE = {"netease", "qqmusic", "kugou"}

TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:：]([0-5]?\d)$")


# ---------------------------------------------------------------- 工具函数

def enable_dpi_awareness() -> None:
    """让窗口在高 DPI 屏上不糊。失败不影响使用（老系统没有 shcore）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)     # 系统 DPI 感知
        except Exception:                                     # noqa: BLE001 - 老系统兜底
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:                                  # noqa: BLE001 - 纯装饰
        log.debug("设置 DPI 感知失败（忽略）：%s", exc)


def project_root() -> str:
    """music_sync/ui/settings.py -> 项目根目录。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pythonw_path() -> str:
    """优先用 pythonw.exe，避免计划任务/子进程弹黑框。"""
    exe = sys.executable or "python.exe"
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(candidate):
        return candidate
    return exe


def normalize_time(text: str) -> str | None:
    """把 ``21:0`` / ``9：30`` 规整成 ``21:00`` / ``09:30``；不合法返回 None。"""
    match = TIME_RE.match((text or "").strip())
    if not match:
        return None
    hour, minute = match.groups()
    return f"{int(hour):02d}:{int(minute):02d}"


def quality_levels_of(platform_key: str, platform_cfg: dict) -> tuple[str, ...]:
    """取某平台支持的音质档位；适配器还没就绪时返回空元组。"""
    try:
        adapter = create(platform_key, platform_cfg or {})
        return tuple(getattr(adapter, "quality_levels", ()) or ())
    except Exception:                                     # noqa: BLE001 - 适配器缺失/未安装
        return ()


def clip_text(text: str, cells: int = 34) -> str:
    """按「显示宽度」截断（中文算 2 格），避免长状态文字把按钮挤到窗口外。"""
    out: list[str] = []
    used = 0
    for char in text or "":
        width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + width > cells:
            return "".join(out) + "…"
        out.append(char)
        used += width
    return "".join(out)


# ---------------------------------------------------------------- 主窗口

def probe_login(platform_key: str, platform_cfg: dict, cfg: dict) -> tuple[bool, str]:
    """检测登录态：优先用 login.status（客户端 → 浏览器 → 配置三路），退回适配器自查。"""
    try:
        from music_sync import login as login_mod
    except Exception:                                     # noqa: BLE001 - 模块可能还没写好
        login_mod = None
    status_fn = getattr(login_mod, "status", None) if login_mod is not None else None
    if callable(status_fn):
        try:
            ok, message = status_fn(platform_key, cfg or {})
            return bool(ok), str(message or "")
        except Exception as exc:                          # noqa: BLE001 - 检测失败不该炸界面
            return False, f"检测失败：{exc}"
    try:
        adapter = create(platform_key, platform_cfg or {})
        ok, message = adapter.check_login()
        return bool(ok), str(message or "")
    except Exception as exc:                              # noqa: BLE001 - 适配器缺失/依赖未装
        return False, str(exc) or exc.__class__.__name__


def auto_login(platform_key: str, cfg: dict, config_file: str) -> tuple[bool, str]:
    """自动获取登录状态（用户不用懂 cookie），成功时把结果写进内存里的 cfg。"""
    try:
        from music_sync import login as login_mod
    except Exception:                                     # noqa: BLE001 - 模块可能还没写好
        login_mod = None
    auto_fn = getattr(login_mod, "auto", None) if login_mod is not None else None
    if callable(auto_fn):
        try:
            ok, message = auto_fn(platform_key, cfg, save=False, config_path=config_file)
            return bool(ok), str(message or "")
        except TypeError:                                 # 老签名不带 config_path
            try:
                ok, message = auto_fn(platform_key, cfg)
                return bool(ok), str(message or "")
            except Exception as exc:                      # noqa: BLE001
                return False, f"自动获取失败：{exc}"
        except Exception as exc:                          # noqa: BLE001 - 读浏览器/解密都可能失败
            return False, f"自动获取失败：{exc}"

    # login 模块还没到位时的兜底：直接读浏览器里的登录状态
    try:
        from music_sync import browser_cookies
    except Exception:                                     # noqa: BLE001
        return False, "自动获取登录状态的功能还没就绪，请稍后再试。"
    if not getattr(browser_cookies, "KEY_COOKIES", {}).get(platform_key):
        return False, "这个平台没有可自动读取的登录字段，请用「手动粘贴」。"
    try:
        source = browser_cookies.best(platform_key)
    except Exception as exc:                              # noqa: BLE001
        return False, f"读取浏览器登录状态失败：{exc}"
    if source is None or not source.cookies:
        return False, "没找到登录状态，请先用浏览器打开该平台网页并登录一次，再点这里。"
    header = browser_cookies.to_header(source.cookies)
    if not header:
        return False, "读到了数据但没解析出登录字段。"
    platform_cfg = (cfg.setdefault("platforms", {}) or {}).setdefault(platform_key, {})
    platform_cfg["cookie"] = header
    return True, f"已从 {source.browser} 读取登录状态（{source.count} 项），点「保存」后生效。"


class SettingsApp:
    """设置窗口。所有状态都挂在这个对象上，方便回调里访问。"""

    def __init__(self, config_path: str | None = None):
        # 先确保 config.json 存在：界面里的「立即运行」「打开日志」都依赖它
        self.config_file = config_mod.ensure_exists(config_path)
        self.cfg: dict = config_mod.load(self.config_file)
        self._alive = True
        self._status_labels: dict[str, ttk.Label] = {}

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(720, 580)
        self._setup_style()

        self._make_vars()
        self._build_ui()
        self._load_into_vars()
        self._apply_schedule_visibility()
        self._refresh_quality_hint()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._probe_all_async)          # 界面先出来，再去探登录态

    # ------------------------------------------------------------ 外观
    def _setup_style(self) -> None:
        self.root.option_add("*Font", FONT_UI)
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")                         # Windows 下最顺眼
        except tk.TclError:
            pass
        style.configure(".", font=FONT_UI)
        style.configure("TLabelframe.Label", font=FONT_TITLE)
        style.configure("Hint.TLabel", font=FONT_HINT, foreground="#777777")
        style.configure("Ok.TLabel", foreground="#1a7f37")
        style.configure("Bad.TLabel", foreground="#b4241c")
        style.configure("Idle.TLabel", foreground="#777777")
        style.configure("TButton", padding=(8, 3))

    # ------------------------------------------------------------ 变量
    def _make_vars(self) -> None:
        self.var_platform = tk.StringVar()
        self.var_playlist = tk.StringVar()
        self.var_library = tk.StringVar()
        self.var_client = tk.StringVar()
        self.var_quality = tk.StringVar()        # 存中文标签，方便 Combobox 直接显示
        self.var_auto_move = tk.BooleanVar()

        self.var_sched_on = tk.BooleanVar()
        self.var_mode = tk.StringVar()
        self.var_weekday = tk.StringVar()
        self.var_time = tk.StringVar()
        self.var_hours = tk.StringVar()
        self.var_task_name = tk.StringVar()

        self.var_toast = tk.BooleanVar()
        self.var_timeout = tk.StringVar()

        # 频率一变就立刻切换下面几行的显示，不用等用户点别处
        self.var_mode.trace_add("write", lambda *_: self._apply_schedule_visibility())

    # ------------------------------------------------------------ 布局
    def _build_ui(self) -> None:
        # 先 pack 底部按钮栏：pack 按顺序分配空间，按钮先占位才不会被上面挤掉
        self._build_buttons(self.root).pack(side="bottom", fill="x", padx=12, pady=(6, 10))

        body = ttk.Frame(self.root, padding=(12, 8, 12, 4))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        self._build_basic(body).grid(row=0, column=0, sticky="ew")
        self._build_schedule(body).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self._build_login(body).grid(row=2, column=0, sticky="ew", pady=(6, 0))

    def _build_basic(self, parent) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="基本设置", padding=(10, 3))
        frame.columnconfigure(1, weight=1)

        # 平台
        ttk.Label(frame, text="音乐平台").grid(row=0, column=0, sticky="w", pady=2)
        self.cb_platform = ttk.Combobox(
            frame, textvariable=self.var_platform, state="readonly", width=18,
            values=[PLATFORM_LABELS[k] for k in PLATFORMS])
        self.cb_platform.grid(row=0, column=1, sticky="w", pady=2)
        self.cb_platform.bind("<<ComboboxSelected>>", lambda _e: self._refresh_quality_hint())
        ttk.Label(frame, text="只同步这个平台的歌单", style="Hint.TLabel").grid(
            row=0, column=2, sticky="w", padx=(8, 0))

        # 歌单
        ttk.Label(frame, text="歌单").grid(row=1, column=0, sticky="w", pady=2)
        entry_playlist = ttk.Entry(frame, textvariable=self.var_playlist)
        entry_playlist.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Button(frame, text="说明", width=6, command=self._show_playlist_help).grid(
            row=1, column=2, sticky="w", padx=(8, 0))

        # 曲库目录
        ttk.Label(frame, text="曲库目录").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=self.var_library).grid(row=2, column=1, sticky="ew", pady=2)
        ttk.Button(frame, text="浏览…", width=8,
                   command=lambda: self._browse_dir(self.var_library)).grid(
            row=2, column=2, sticky="w", padx=(8, 0))

        # 客户端下载目录
        ttk.Label(frame, text="客户端下载目录").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=self.var_client).grid(row=3, column=1, sticky="ew", pady=2)
        ttk.Button(frame, text="浏览…", width=8,
                   command=lambda: self._browse_dir(self.var_client)).grid(
            row=3, column=2, sticky="w", padx=(8, 0))

        # 音质 + 自动归置（同一行省高度）
        ttk.Label(frame, text="音质").grid(row=4, column=0, sticky="w", pady=2)
        quality_row = ttk.Frame(frame)
        quality_row.grid(row=4, column=1, columnspan=2, sticky="ew", pady=2)
        self.cb_quality = ttk.Combobox(
            quality_row, textvariable=self.var_quality, state="readonly", width=18,
            values=[label for _, label in QUALITIES])
        self.cb_quality.pack(side="left")
        self.cb_quality.bind("<<ComboboxSelected>>", lambda _e: self._refresh_quality_hint())
        ttk.Checkbutton(quality_row, text="下载完成后把客户端下载归到曲库",
                        variable=self.var_auto_move).pack(side="left", padx=(18, 0))

        # 灰字提醒（无损不支持 / 平台不可下载都写这一行，平时留空占位）
        self.lbl_hint = ttk.Label(frame, text="", style="Hint.TLabel")
        self.lbl_hint.grid(row=5, column=1, columnspan=2, sticky="w", pady=(0, 2))
        return frame

    def _build_schedule(self, parent) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="定时与提醒", padding=(10, 3))
        frame.columnconfigure(1, weight=1)

        # 启用 + 频率
        top = ttk.Frame(frame)
        top.grid(row=0, column=0, columnspan=3, sticky="ew", pady=2)
        ttk.Checkbutton(top, text="启用计划任务", variable=self.var_sched_on).pack(side="left")
        ttk.Label(top, text="频率").pack(side="left", padx=(18, 6))
        self.cb_mode = ttk.Combobox(top, textvariable=self.var_mode, state="readonly", width=16,
                                    values=[label for _, label in MODES])
        self.cb_mode.pack(side="left")

        # 星期（仅 weekly）
        self.row_weekday = ttk.Frame(frame)
        self.row_weekday.grid(row=1, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(self.row_weekday, text="星期").pack(side="left")
        ttk.Combobox(self.row_weekday, textvariable=self.var_weekday, state="readonly", width=10,
                     values=[label for _, label in WEEKDAYS]).pack(side="left", padx=(47, 0))

        # 时间（weekly / daily）
        self.row_time = ttk.Frame(frame)
        self.row_time.grid(row=2, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(self.row_time, text="执行时间").pack(side="left")
        ttk.Entry(self.row_time, textvariable=self.var_time, width=10).pack(side="left", padx=(28, 6))
        ttk.Label(self.row_time, text="24 小时制，例如 21:00", style="Hint.TLabel").pack(side="left")

        # 间隔小时（仅 hours）
        self.row_hours = ttk.Frame(frame)
        self.row_hours.grid(row=3, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(self.row_hours, text="间隔").pack(side="left")
        ttk.Spinbox(self.row_hours, from_=1, to=8760, increment=1, width=8,
                    textvariable=self.var_hours).pack(side="left", padx=(47, 6))
        ttk.Label(self.row_hours, text="小时（1~8760，168 = 一周一次）", style="Hint.TLabel").pack(
            side="left")

        # 任务名称
        ttk.Label(frame, text="计划任务名称").grid(row=4, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=self.var_task_name, width=24).grid(
            row=4, column=1, sticky="w", pady=2)
        ttk.Label(frame, text="与 install_task.ps1 的 -TaskName 一致",
                  style="Hint.TLabel").grid(row=4, column=2, sticky="w", padx=(8, 0))

        # 提示窗
        notify = ttk.Frame(frame)
        notify.grid(row=5, column=0, columnspan=3, sticky="ew", pady=2)
        ttk.Checkbutton(notify, text="同步结束后弹结果提示窗",
                        variable=self.var_toast).pack(side="left")
        ttk.Label(notify, text="自动关闭").pack(side="left", padx=(18, 6))
        ttk.Spinbox(notify, from_=5, to=300, increment=5, width=6,
                    textvariable=self.var_timeout).pack(side="left")
        ttk.Label(notify, text="秒（5~300）").pack(side="left", padx=(4, 0))
        return frame

    def _build_login(self, parent) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="平台登录态", padding=(10, 3))
        frame.columnconfigure(1, weight=1)

        for index, (key, (title, downloadable)) in enumerate(PLATFORMS.items()):
            ttk.Label(frame, text=title, width=11, anchor="w").grid(
                row=index, column=0, sticky="w", pady=1)
            label = ttk.Label(frame, text="检测中…", style="Idle.TLabel", anchor="w", width=40)
            label.grid(row=index, column=1, sticky="w", pady=1, padx=(4, 8))
            self._status_labels[key] = label
            state = "normal" if key in LOGINABLE else "disabled"
            ttk.Button(frame, text="自动获取", width=9, state=state,
                       command=lambda k=key: self._auto_login(k)).grid(
                row=index, column=2, sticky="e", padx=(0, 4), pady=1)
            ttk.Button(frame, text="手动粘贴", width=9, state=state,
                       command=lambda k=key: self._edit_cookie(k)).grid(
                row=index, column=3, sticky="e", padx=(0, 4), pady=1)
            ttk.Button(frame, text="检测", width=6,
                       command=lambda k=key: self._probe_async(k)).grid(
                row=index, column=4, sticky="e", pady=1)
            if not downloadable:
                label.configure(text="只做歌单对照，不支持下载（没有公开接口）", style="Idle.TLabel")

        ttk.Label(frame, text="「自动获取」会读你已经登录过的客户端/浏览器，一般点一下就行；"
                              "拿到状态后记得点「保存」。",
                  style="Hint.TLabel").grid(
            row=len(PLATFORMS), column=0, columnspan=5, sticky="w", pady=(3, 0))
        return frame

    def _build_buttons(self, parent) -> ttk.Frame:
        bar = ttk.Frame(parent)
        left = ttk.Frame(bar)
        left.pack(side="left")
        right = ttk.Frame(bar)
        right.pack(side="right")

        ttk.Button(left, text="立即运行一次", command=self._run_now).pack(side="left", padx=(0, 6))
        ttk.Button(left, text="打开日志", command=self._open_log).pack(side="left", padx=(0, 6))
        ttk.Button(left, text="自检", command=self._selfcheck).pack(side="left")

        ttk.Button(right, text="关闭", command=self._on_close).pack(side="right", padx=(6, 0))
        ttk.Button(right, text="保存", command=self._save).pack(side="right")
        return bar

    # ------------------------------------------------------------ 载入 / 回填
    def _load_into_vars(self) -> None:
        cfg = self.cfg
        platform_key = str(cfg.get("platform") or "netease")
        self.var_platform.set(PLATFORM_LABELS.get(platform_key, platform_key))
        self.var_playlist.set(str(cfg.get("playlist") or ""))
        self.var_library.set(str(cfg.get("library_dir") or ""))
        self.var_client.set(str(cfg.get("client_download_dir") or ""))
        quality = str(cfg.get("quality") or "320")
        self.var_quality.set(QUALITY_LABELS.get(quality, QUALITY_LABELS["320"]))
        self.var_auto_move.set(bool(cfg.get("auto_move", True)))

        sched = cfg.get("schedule") or {}
        self.var_sched_on.set(bool(sched.get("enabled", True)))
        mode = str(sched.get("mode") or "weekly")
        self.var_mode.set(MODE_LABELS.get(mode, MODE_LABELS["weekly"]))
        weekday = str(sched.get("weekday") or "sunday")
        self.var_weekday.set(WEEKDAY_LABELS.get(weekday, WEEKDAY_LABELS["sunday"]))
        self.var_time.set(str(sched.get("time") or "21:00"))
        self.var_hours.set(str(sched.get("hours") or 168))
        self.var_task_name.set(str(sched.get("task_name") or "MusicSync"))

        notify = cfg.get("notify") or {}
        self.var_toast.set(bool(notify.get("toast", True)))
        self.var_timeout.set(str(notify.get("timeout") or 40))

    # ------------------------------------------------------------ 交互
    def _current_platform_key(self) -> str:
        label = self.var_platform.get()
        return PLATFORM_BY_LABEL.get(label, label or "netease")

    def _current_mode_key(self) -> str:
        label = self.var_mode.get()
        return MODE_BY_LABEL.get(label, "weekly")

    def _current_quality_key(self) -> str:
        label = self.var_quality.get()
        return QUALITY_BY_LABEL.get(label, "320")

    def _apply_schedule_visibility(self) -> None:
        """按频率动态显示/隐藏星期、时间、间隔三行。"""
        mode = self._current_mode_key()
        rows = {
            "weekly": (True, True, False),
            "daily": (False, True, False),
            "hours": (False, False, True),
        }
        show_weekday, show_time, show_hours = rows.get(mode, rows["weekly"])
        for frame, visible in (
            (self.row_weekday, show_weekday),
            (self.row_time, show_time),
            (self.row_hours, show_hours),
        ):
            if visible:
                frame.grid()
            else:
                frame.grid_remove()

    def _refresh_quality_hint(self) -> None:
        """无损只部分平台支持、汽水只有占位适配器：都在灰字里说清楚。"""
        key = self._current_platform_key()
        quality = self._current_quality_key()
        name = PLATFORM_LABELS.get(key, key)
        if not PLATFORM_DOWNLOADABLE.get(key, True):
            self.lbl_hint.configure(text=f"{name}目前只有占位适配器，不能下载，仅能做歌单对照。")
            return
        if quality != "lossless":
            self.lbl_hint.configure(text="")
            return
        levels = quality_levels_of(key, (self.cfg.get("platforms") or {}).get(key) or {})
        if not levels:
            self.lbl_hint.configure(text=f"无法确认{name}是否支持无损（适配器未就绪），取不到无损时会自动降级。")
        elif "lossless" in levels:
            self.lbl_hint.configure(text=f"{name}支持无损，但部分歌曲仍需会员权益。")
        else:
            self.lbl_hint.configure(text=f"{name}不支持无损，保存后下载时会自动降级到 320 kbps。")

    def _browse_dir(self, var: tk.StringVar) -> None:
        current = var.get().strip()
        initial = current if current and os.path.isdir(config_mod.expand(current)) else os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="选择目录", initialdir=initial, mustexist=False)
        if chosen:
            var.set(os.path.normpath(chosen))

    def _show_playlist_help(self) -> None:
        messagebox.showinfo(
            "歌单怎么填",
            "两种写法都行：\n"
            "  1. 歌单 id：网页版歌单地址里 id= 后面那串数字；\n"
            "  2. 歌单链接：整条 https://music.163.com/#/playlist?id=xxxx 直接粘贴。\n\n"
            "注意：只能同步当前登录账号能访问的歌单，私密歌单必须先登录。",
            parent=self.root)

    # ------------------------------------------------------------ 登录态
    def _set_status(self, key: str, ok: bool, message: str) -> None:
        label = self._status_labels.get(key)
        if label is None or not self._alive:
            return
        body = (message or "").strip() or ("已登录" if ok else "未登录")
        if ok and body == "已登录":
            text, style = "已登录", "Ok.TLabel"
        elif ok:
            text, style = f"已登录（{body}）", "Ok.TLabel"
        elif not PLATFORM_DOWNLOADABLE.get(key, True):
            text, style = f"未登录：{body}", "Idle.TLabel"
        else:
            text, style = f"未登录：{body}", "Bad.TLabel"
        # 组合完再按显示宽度裁，保证不会顶到右边的按钮
        label.configure(text=clip_text(text, 38), style=style)

    def _probe_async(self, key: str) -> None:
        """后台线程检测单个平台，结果用 after() 回填，绝不阻塞 UI。"""
        label = self._status_labels.get(key)
        if label is not None:
            label.configure(text="检测中…", style="Idle.TLabel")
        platform_cfg = copy.deepcopy((self.cfg.get("platforms") or {}).get(key) or {})
        cfg = copy.deepcopy(self.cfg)

        def work() -> None:
            ok, message = probe_login(key, platform_cfg, cfg)
            self._post(self._set_status, key, ok, message)

        threading.Thread(target=work, name=f"probe-{key}", daemon=True).start()

    def _auto_login(self, key: str) -> None:
        """一键获取登录状态：读已登录的客户端/浏览器，不用手填 cookie。"""
        title = PLATFORM_LABELS.get(key, key)
        label = self._status_labels.get(key)
        if label is not None:
            label.configure(text=f"正在读取{title}的登录状态…", style="Idle.TLabel")

        def work() -> None:
            ok, message = auto_login(key, self.cfg, self.config_file)
            self._post(self._after_auto_login, key, ok, message)

        threading.Thread(target=work, name=f"login-{key}", daemon=True).start()

    def _after_auto_login(self, key: str, ok: bool, message: str) -> None:
        if not self._alive:
            return
        title = PLATFORM_LABELS.get(key, key)
        if ok:
            self._pull_cookie_from_file(key)
            messagebox.showinfo("已获取登录状态",
                                f"{title}：{message}\n\n记得点「保存」才会写进配置文件。",
                                parent=self.root)
        else:
            messagebox.showwarning(
                "没找到登录状态",
                f"{title}：{message}\n\n"
                f"办法：先在浏览器里打开{title}网页并登录一次（QQ音乐/酷狗也可以用官方客户端登录），"
                f"再回到这里点「自动获取」；实在不行就用「手动粘贴」。",
                parent=self.root)
        self._probe_async(key)

    def _pull_cookie_from_file(self, key: str) -> None:
        """login.auto 可能已经写盘了，把该平台的登录字段同步回内存，免得下次「保存」把它覆盖掉。"""
        current = str(((self.cfg.get("platforms") or {}).get(key) or {}).get("cookie") or "")
        if current:
            return
        try:
            disk = config_mod.load(self.config_file)
        except (OSError, ValueError):
            return
        fresh = str(((disk.get("platforms") or {}).get(key) or {}).get("cookie") or "")
        if fresh:
            target = (self.cfg.setdefault("platforms", {}) or {}).setdefault(key, {})
            target["cookie"] = fresh

    def _probe_all_async(self) -> None:
        for key in PLATFORMS:
            self._probe_async(key)

    def _edit_cookie(self, key: str) -> None:
        title = PLATFORM_LABELS.get(key, key)
        current = str(((self.cfg.get("platforms") or {}).get(key) or {}).get("cookie") or "")

        dialog = tk.Toplevel(self.root)
        dialog.title(f"{title} - 手动粘贴登录信息")
        dialog.transient(self.root)
        dialog.geometry("620x340")
        dialog.minsize(480, 260)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)

        ttk.Label(
            dialog,
            text=(f"一般不用走到这一步：先试「自动获取」。实在不行再手动粘——\n"
                  f"浏览器登录 {title} 后按 F12 → Network（网络）→ 刷新页面 → 随便点一个请求，\n"
                  "在 Request Headers 里找到 Cookie，把整行的值复制粘贴到下面。\n"
                  "（网易云通常连粘都不用粘：客户端登录后会自动读取。）"),
            style="Hint.TLabel", justify="left").grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 6))

        text = tk.Text(dialog, wrap="char", height=9, font=("Consolas", 9), undo=True)
        text.grid(row=1, column=0, sticky="nsew", padx=10)
        scroll = ttk.Scrollbar(dialog, orient="vertical", command=text.yview)
        scroll.grid(row=1, column=1, sticky="ns", pady=(0, 0))
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", current)

        def confirm() -> None:
            value = text.get("1.0", "end").strip()
            platforms = self.cfg.setdefault("platforms", {})
            platforms.setdefault(key, {})["cookie"] = value
            dialog.destroy()
            log.info("已写入 %s 的 cookie（%d 字符，未保存到磁盘）", key, len(value))
            self._probe_async(key)

        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", padx=10, pady=(6, 10))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="确定", command=confirm).pack(side="right")

        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.grab_set()
        text.focus_set()

    # ------------------------------------------------------------ 保存
    def _collect(self) -> dict | None:
        """校验并组装新配置；不合法时弹窗并返回 None。"""
        library = self.var_library.get().strip()
        client = self.var_client.get().strip()
        if not library:
            messagebox.showerror("填错了", "曲库目录不能为空。", parent=self.root)
            return None

        targets = [("曲库目录", library)]
        if client:
            targets.append(("客户端下载目录", client))
        for name, path in targets:
            full = config_mod.expand(path)
            try:
                os.makedirs(full, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "目录不可用", f"{name}无法创建或访问：\n{full}\n\n{exc}", parent=self.root)
                return None

        quality = self._current_quality_key()
        if quality not in QUALITY_KEYS:
            messagebox.showerror("填错了", f"音质不在允许的档位里：{quality}", parent=self.root)
            return None

        mode = self._current_mode_key()
        time_text = self.var_time.get()
        normalized = normalize_time(time_text)
        if mode in ("weekly", "daily") and not normalized:
            messagebox.showerror(
                "时间格式不对", f"执行时间要写成 HH:MM（24 小时制），例如 21:00。\n现在填的是：{time_text!r}",
                parent=self.root)
            return None

        hours = 168
        if mode == "hours":
            raw = self.var_hours.get().strip()
            if not raw.isdigit() or not (1 <= int(raw) <= 8760):
                messagebox.showerror(
                    "间隔不对", f"间隔小时数要是 1~8760 的整数，现在填的是：{raw!r}", parent=self.root)
                return None
            hours = int(raw)

        timeout_raw = self.var_timeout.get().strip()
        if not timeout_raw.isdigit() or not (5 <= int(timeout_raw) <= 300):
            messagebox.showerror(
                "秒数不对", f"提示窗自动关闭秒数要是 5~300 的整数，现在填的是：{timeout_raw!r}",
                parent=self.root)
            return None

        task_name = self.var_task_name.get().strip() or "MusicSync"
        weekday = WEEKDAY_BY_LABEL.get(self.var_weekday.get(), "sunday")
        playlist = self.var_playlist.get().strip()

        new = copy.deepcopy(self.cfg)                   # 保留 ignore_ids / download 等未在界面里的字段
        new["platform"] = self._current_platform_key()
        new["playlist"] = playlist
        new["library_dir"] = config_mod.expand(library)
        new["client_download_dir"] = config_mod.expand(client) if client else ""
        new["quality"] = quality
        new["auto_move"] = bool(self.var_auto_move.get())

        schedule = dict(new.get("schedule") or {})
        schedule.update({
            "enabled": bool(self.var_sched_on.get()),
            "mode": mode,
            "weekday": weekday,
            "time": normalized or (normalize_time(time_text) or str(schedule.get("time") or "21:00")),
            "hours": hours,
            "task_name": task_name,
        })
        new["schedule"] = schedule

        notify = dict(new.get("notify") or {})
        notify.update({"toast": bool(self.var_toast.get()), "timeout": int(timeout_raw)})
        new["notify"] = notify
        return new

    def _save(self) -> None:
        new = self._collect()
        if new is None:
            return
        try:
            path = config_mod.save(new, self.config_file)
        except OSError as exc:
            messagebox.showerror("保存失败", f"写不进配置文件：\n{exc}", parent=self.root)
            return
        self.cfg = new
        log.info("设置已保存：%s", path)
        messagebox.showinfo("已保存", f"配置已写入：\n{path}", parent=self.root)
        self._refresh_quality_hint()

    # ------------------------------------------------------------ 底部按钮
    def _run_now(self) -> None:
        if not os.path.isfile(self.config_file):
            config_mod.ensure_exists(self.config_file)
        command = [pythonw_path(), "-m", "music_sync",
                   "--config", self.config_file, "sync", "--gui"]
        cwd = project_root()
        try:
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(command, cwd=cwd, creationflags=flags)
        except OSError as exc:
            messagebox.showerror("启动失败", f"没能启动同步进程：\n{exc}\n\n命令：{' '.join(command)}",
                                 parent=self.root)
            return
        log.info("已在后台启动一次同步：%s", " ".join(command))
        messagebox.showinfo(
            "已开始", "同步已在后台开始，完成后会弹提示窗。\n进度和错误可以在「打开日志」里看。",
            parent=self.root)

    def _open_log(self) -> None:
        path = os.path.join(config_mod.config_dir(), LOG_NAME)
        if not os.path.isfile(path):
            messagebox.showinfo("还没有日志", f"日志文件还不存在：\n{path}\n\n先运行一次同步就会生成。",
                                parent=self.root)
            return
        try:
            os.startfile(path)                          # noqa: S606 - Windows 打开文本
        except OSError as exc:
            messagebox.showerror("打不开", f"{path}\n\n{exc}", parent=self.root)

    def _selfcheck(self) -> None:
        """后台跑自检，结果塞进一个只读 Toplevel。"""
        try:
            from music_sync import selfcheck            # 可能还没写好，兜底提示
        except ImportError:
            messagebox.showinfo("自检模块未就绪",
                                "自检模块还没写好（music_sync/selfcheck.py 不存在），先跳过这一步。",
                                parent=self.root)
            return
        run_checks = getattr(selfcheck, "run_checks", None)
        if not callable(run_checks):
            messagebox.showinfo("自检模块未就绪", "selfcheck 里没有 run_checks()，先跳过这一步。",
                                parent=self.root)
            return

        cfg = copy.deepcopy(self.cfg)

        window = tk.Toplevel(self.root)
        window.title("自检结果")
        window.transient(self.root)
        window.geometry("680x420")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)
        text = tk.Text(window, wrap="word", font=("Consolas", 9), state="disabled")
        text.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        scroll = ttk.Scrollbar(window, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns", pady=10)
        text.configure(yscrollcommand=scroll.set)
        ttk.Button(window, text="关闭", command=window.destroy).grid(
            row=1, column=0, columnspan=2, sticky="e", padx=10, pady=(0, 10))

        def show(items: list[str]) -> None:
            if not self._alive:
                return
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", "\n".join(items) if items else "（自检没有返回内容）")
            text.configure(state="disabled")

        def work() -> None:
            try:
                try:
                    checks = run_checks(cfg, self.config_file)
                except TypeError:                      # 老签名只收一个参数
                    checks = run_checks(cfg)
                if isinstance(checks, str):
                    items = checks.splitlines()
                else:
                    formatter = getattr(selfcheck, "format_text", None)
                    if callable(formatter):
                        items = str(formatter(checks)).splitlines()
                    else:
                        items = [str(line) for line in (checks or [])]
            except Exception as exc:                    # noqa: BLE001 - 自检本身也可能炸
                items = [f"自检失败：{exc.__class__.__name__}: {exc}"]
            self._post(show, items or ["自检完成，没有任何输出。"])

        threading.Thread(target=work, name="selfcheck", daemon=True).start()

    # ------------------------------------------------------------ 收尾
    def _post(self, callback, *args) -> None:
        """从后台线程安全地调度回 UI 线程。"""
        if not self._alive:
            return
        try:
            self.root.after(0, lambda: callback(*args))
        except (RuntimeError, tk.TclError):               # 窗口已经销毁
            pass

    def _on_close(self) -> None:
        self._alive = False
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def mainloop(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------- 入口

def run(config_path: str | None = None) -> int:
    """启动设置界面，返回进程退出码。"""
    enable_dpi_awareness()
    try:
        app = SettingsApp(config_path)
    except Exception as exc:                              # noqa: BLE001 - 起不来时给个说法
        log.exception("设置界面启动失败")
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("无法启动设置界面", f"{exc.__class__.__name__}: {exc}")
            root.destroy()
        except Exception:                                 # noqa: BLE001
            print(f"无法启动设置界面：{exc}", file=sys.stderr)
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
