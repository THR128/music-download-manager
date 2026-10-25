# -*- coding: utf-8 -*-
"""本地曲库扫描 + 歌单对账。

对账思路（从实际出发，不追求完美）：
- 歌名去掉括号里的版本词后做主匹配（``泡沫 (Live)`` 与 ``泡沫`` 版本不同，不能混）；
- 歌手集合 Jaccard 做次要权重；
- 两侧都带版本标签（live/remix/伴奏/翻唱…）且完全不重叠时直接排除；
- 综合分阈值 0.60，按分数降序贪心指派，避免一个文件被两个条目抢走；
- 同曲不同 id 的重复条目用「分组覆盖」处理：只要组里有一首匹配上，整组算已下载。
"""
from __future__ import annotations

import difflib
import os
import re
import unicodedata
from dataclasses import dataclass, field

from .platforms.base import Track

AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ape", ".ogg")

_PUNCT = re.compile(r"[\s\-_·・.,'\"“”‘’!?！？:：;；/\\()\[\]{}<>《》〈〉~`@#$%^&*+=|、。，…—–]+")
_ART_SPLIT = re.compile(r"[;；,，/&、_]| feat\.? | ft\.? | - ", re.I)
_VER_WORDS = (
    "live", "remix", "acoustic", "instrumental", "伴奏", "纯音乐", "cover", "翻自", "翻唱",
    "demo", "edit", "mix", "dj", "remaster", "重制", "片段", "原唱", "女声", "男声", "摇滚",
    "钢琴", "吉他", "版", "伴奏版", "现场",
)


def normalize(text: str) -> str:
    return _PUNCT.sub("", unicodedata.normalize("NFKC", text or "").lower())


def strip_brackets(text: str) -> str:
    return re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", "", text or "")


def core_title(text: str) -> str:
    return normalize(strip_brackets(text))


def brackets(text: str) -> list[str]:
    return re.findall(r"[（(\[【]([^）)\]】]*)[）)\]】]", text or "")


def version_tags(*texts: str) -> set[str]:
    joined = normalize(" ".join(t for t in texts if t))
    return {w for w in _VER_WORDS if normalize(w) in joined}


def split_artists(text: str) -> set[str]:
    return {normalize(x) for x in _ART_SPLIT.split(text or "") if normalize(x)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        ratio = max(ratio, 0.86)
    return ratio


@dataclass
class LocalFile:
    path: str
    name: str                      # 不含扩展名的文件名
    artists: set[str]
    core: str
    tags: set[str]

    @property
    def display(self) -> str:
        return os.path.basename(self.path)


def scan_library(directory: str, exts: tuple[str, ...] = AUDIO_EXTS) -> list[LocalFile]:
    """递归扫描本地曲库（只读，不改动文件）。"""
    found: list[LocalFile] = []
    if not os.path.isdir(directory):
        return found
    for root, _dirs, names in os.walk(directory):
        for name in sorted(names):
            if not name.lower().endswith(exts):
                continue
            stem = os.path.splitext(name)[0]
            if " - " in stem:
                artist_text, title = stem.split(" - ", 1)
            else:
                artist_text, title = "", stem
            found.append(LocalFile(
                path=os.path.join(root, name),
                name=stem,
                artists=split_artists(artist_text),
                core=core_title(title),
                # 版本标签只从歌名和括号里取：歌手名里混进 "Zedd"/"Don Toliver"/"mixed matches"
                # 这类字串会让 dj / live / mix 误命中（zeddjon… 含 "dj"）
                tags=version_tags(title, " ".join(brackets(title))),
            ))
    return found


@dataclass
class Report:
    tracks: list[Track]
    files: list[LocalFile]
    pairs: dict[int, int] = field(default_factory=dict)      # track 下标 -> file 下标
    missing: list[Track] = field(default_factory=list)
    extras: list[LocalFile] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return len(self.pairs)


def _coverage_key(track: Track) -> tuple[str, tuple[str, ...]]:
    artists: set[str] = set()
    for artist in track.artists:
        artists |= split_artists(artist)
    return core_title(track.name), tuple(sorted(artists))


def match(tracks: list[Track], files: list[LocalFile], threshold: float = 0.60,
          ignore_ids: set[str] | None = None) -> Report:
    """歌单 ↔ 本地文件对账。"""
    ignore_ids = ignore_ids or set()
    report = Report(tracks=tracks, files=files)

    candidates: list[tuple[float, int, int]] = []
    for ti, track in enumerate(tracks):
        t_core = core_title(track.name)
        t_tags = version_tags(track.name, " ".join(brackets(track.name)))
        t_art: set[str] = set()
        for artist in track.artists:
            t_art |= split_artists(artist)   # 平台侧一个 artist 里可能还塞了多人
        for fi, local in enumerate(files):
            # 两边都带版本标签且完全不重叠 → 直接排除（Live 不吃录音室版）
            # 注意：只在「两侧都有标签」时排除。单侧带标签（原版想要、文件是某版）
            # 交给打分自然取舍：标签对得上的那条 track 分数更高，会先被指派走。
            if local.tags != t_tags and not (local.tags & t_tags):
                continue
            title_sim = similarity(local.core, t_core)
            if title_sim < 0.45:
                continue
            art_sim = jaccard(local.artists, t_art)
            tag_sim = jaccard(local.tags, t_tags) if (local.tags or t_tags) else 1.0
            score = 0.55 * title_sim + 0.25 * art_sim + 0.20 * tag_sim
            # 同名不同人：多半是翻唱或撞名（凤凰传奇 ≠ 李健），别自动认领
            if local.artists and t_art and art_sim == 0:
                score *= 0.55
            if score >= threshold:
                candidates.append((round(score, 4), ti, fi))

    candidates.sort(key=lambda item: -item[0])
    used_tracks: set[int] = set()
    used_files: set[int] = set()
    for _score, ti, fi in candidates:
        if ti in used_tracks or fi in used_files:
            continue
        used_tracks.add(ti)
        used_files.add(fi)
        report.pairs[ti] = fi

    covered = {_coverage_key(tracks[ti]) for ti in used_tracks}
    report.missing = [
        track for track in tracks
        if _coverage_key(track) not in covered and str(track.id) not in ignore_ids
    ]
    report.extras = [files[fi] for fi in range(len(files)) if fi not in used_files]
    return report
