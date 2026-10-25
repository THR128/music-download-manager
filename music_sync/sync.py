# -*- coding: utf-8 -*-
"""同步编排：归置客户端下载 → 拉歌单 → 对账 → 补齐缺失 → 出报告。

这一层完全不认识具体平台，只跟 ``platforms.create()`` 返回的适配器打交道。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field

from . import config as cfgmod
from . import matching, platforms
from .platforms.base import NotLoggedIn, PlatformError, Unavailable, Unsupported
from .util import get_logger, human_size, now_text

log = get_logger()


@dataclass
class SyncResult:
    time: str = ""
    platform: str = ""
    playlist: int = 0
    files: int = 0
    matched: int = 0
    new: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    grey: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def result_path() -> str:
    return os.path.join(cfgmod.config_dir(), "last_result.json")


def write_result(result: SyncResult) -> str:
    path = result_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(asdict(result), fh, ensure_ascii=False, indent=1)
    return path


def move_client_downloads(cfg: dict, result: SyncResult, dry_run: bool = False) -> None:
    """把客户端自己的下载目录里新出现的音频归到曲库目录（目标已有同名文件则跳过）。"""
    src_dir = cfg.get("client_download_dir") or ""
    dst_dir = cfg.get("library_dir") or ""
    if not cfg.get("auto_move", True) or not src_dir or not dst_dir:
        return
    if not os.path.isdir(src_dir):
        log.debug("客户端下载目录不存在，跳过归置：%s", src_dir)
        return
    os.makedirs(dst_dir, exist_ok=True)
    for name in sorted(os.listdir(src_dir)):
        if not name.lower().endswith(matching.AUDIO_EXTS):
            continue
        src = os.path.join(src_dir, name)
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            log.info("归置跳过（目标已存在）：%s", name)
            continue
        if dry_run:
            log.info("[dry] 将归置：%s", name)
            result.moved.append(name)
            continue
        try:
            shutil.move(src, dst)
            result.moved.append(name)
            log.info("归置：%s", name)
        except OSError as exc:
            log.warning("归置失败 %s：%s", name, exc)


def _download_one(adapter, track, cfg, result: SyncResult, dry_run: bool, fallbacks) -> None:
    """下载一首歌；主平台拿不到时按 fallback 平台顺序换源。"""
    from .util import safe_name

    quality = cfg.get("quality", "320")
    dest = cfg.get("library_dir") or "."
    path = os.path.join(dest, safe_name(str(track)) + "." + cfg.get("download", {}).get("ext", "mp3"))
    if os.path.exists(path) and not cfg.get("download", {}).get("overwrite"):
        return

    if dry_run:
        log.info("[dry] 将下载：%s", track)
        return

    attempts = [(adapter, track)]
    for extra in fallbacks:
        try:
            hits = extra.search(f"{track.name} {track.artist_text}".strip(), limit=3)
        except Exception as exc:                       # noqa: BLE001 - 换源失败不影响主流程
            log.debug("换源搜索失败：%s", exc)
            continue
        if hits:
            attempts.append((extra, hits[0]))

    last_reason = ""
    for index, (source, item) in enumerate(attempts):
        label = "主站" if index == 0 else f"换源({source.title})"
        try:
            written = source.download(item, dest, quality)
        except Unavailable as exc:
            last_reason = str(exc)
            log.info("  %s 拿不到音源：%s", label, exc)
            continue
        except (NotLoggedIn, Unsupported) as exc:
            last_reason = str(exc)
            log.warning("  %s 不可用：%s", label, exc)
            break
        except PlatformError as exc:
            last_reason = str(exc)
            log.warning("  %s 下载失败：%s", label, exc)
            continue
        size = human_size(os.path.getsize(written))
        result.new.append(f"{item.name} - {item.artist_text}  ({size})")
        log.info("下载完成：%s（%s，%s）", item, size, label)
        return

    if "无版权" in last_reason or "下架" in last_reason:
        result.grey.append(f"{track.name} / {track.artist_text}")
    else:
        result.failed.append(f"{track.name} / {track.artist_text}（{last_reason or '未知原因'}）")


def run(cfg: dict, dry_run: bool = False) -> SyncResult:
    """执行一次完整同步，返回结果对象（不抛异常，错误写进 result.error）。"""
    result = SyncResult(time=now_text(), platform=cfg.get("platform", ""))
    library = cfg.get("library_dir") or ""
    if not library:
        result.error = "没有配置曲库目录"
        return result

    try:
        os.makedirs(library, exist_ok=True)
    except OSError as exc:
        result.error = f"曲库目录不可用：{library}（{exc}）"
        return result

    move_client_downloads(cfg, result, dry_run)

    key = platforms.resolve(cfg.get("platform", "netease"))
    result.platform = key
    try:
        adapter = platforms.create(key, cfg.get("platforms", {}).get(key) or {})
    except Unsupported as exc:
        result.error = str(exc)
        return result

    try:
        ok, message = adapter.check_login()
        if not ok:
            result.error = f"{adapter.title} 未登录：{message}"
            return result
        tracks = adapter.list_tracks(cfg.get("playlist") or "")
    except NotLoggedIn as exc:
        result.error = f"{adapter.title} 登录态失效：{exc}"
        return result
    except PlatformError as exc:
        result.error = f"{adapter.title} 拉歌单失败：{exc}"
        return result

    log.info("歌单：%s 首（%s）", len(tracks), adapter.title)
    files = matching.scan_library(library)
    report = matching.match(tracks, files, ignore_ids={str(x) for x in cfg.get("ignore_ids") or []})
    result.playlist, result.files, result.matched = len(tracks), len(files), report.matched_count
    log.info("本地 %s 个文件 / 匹配 %s 对 / 待补 %s 首", len(files), report.matched_count,
             len(report.missing))

    fallbacks = []
    for name in cfg.get("fallback_platforms") or []:
        name = platforms.resolve(name)
        if name == key or not platforms.PLATFORMS.get(name, ("", False))[1]:
            continue
        try:
            extra = platforms.create(name, cfg.get("platforms", {}).get(name) or {})
            if extra.check_login()[0]:
                fallbacks.append(extra)
        except PlatformError:
            continue

    pause = float(cfg.get("download", {}).get("sleep", 0.4))
    for track in report.missing:
        _download_one(adapter, track, cfg, result, dry_run, fallbacks)
        time.sleep(pause)

    if result.grey:
        log.info("无版权/下架 %s 首：%s", len(result.grey), "；".join(result.grey[:10]))
    if result.failed:
        log.warning("失败 %s 首：%s", len(result.failed), "；".join(result.failed[:10]))
    log.info("== 本次结束：新增 %s 首，归置 %s 个，跳过 %s 首无版权 ==",
             len(result.new), len(result.moved), len(result.grey))
    return result
