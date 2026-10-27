# -*- coding: utf-8 -*-
"""加密音频解锁：把客户端下载的私有格式转成普通 mp3 / flac。

**为什么需要**：平台给不出原生 mp3 直链时（版权限制、VIP 专属、接口下架），
官方客户端仍然允许付费/授权用户「下载」，但存下来的是加密容器
（网易云 ``.ncm``、QQ音乐 ``.qmc0/.qmc3/.qmcflac/.qmcogg`` …）。这些文件
只有对应客户端能放。本模块在**本地**把它们还原成标准音频文件。

**不联网、不上传**：纯粹读本地文件 → 写本地文件，算法全部在本机跑完。

算法来源（均为 MIT，本模块照算法自行实现，未拷贝其代码）：

* 网易云 ``.ncm``：taurusxin/ncmdump
* QQ音乐 ``.qmc*``（v1 静态掩码）：presburger/qmc-decoder
* 酷狗 ``.kgm/.kgma``（含 ``.vpr``）：unlock-music / @jixun/kugou-crypto

暂不支持（会明确提示，不会静默失败）：``.mflac/.mgg/.mmp4``（QMCv2）、
``.kgg``（酷狗新版）、``.kwm``（酷我）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..util import safe_name

__all__ = [
    "UnlockError",
    "Unlocked",
    "SUPPORTED_EXT",
    "HINT_ONLY_EXT",
    "detect",
    "unlock",
    "unlock_dir",
]

# 能自动解锁的扩展名 -> 输出说明里的格式名
SUPPORTED_EXT = {
    ".ncm": "网易云 .ncm",
    ".qmc0": "QQ音乐 .qmc0",
    ".qmc2": "QQ音乐 .qmc2",
    ".qmc3": "QQ音乐 .qmc3",
    ".qmcflac": "QQ音乐 .qmcflac",
    ".qmcogg": "QQ音乐 .qmcogg",
    ".kgm": "酷狗 .kgm",
    ".kgma": "酷狗 .kgma",
    ".vpr": "酷狗 .vpr",
}

# 认得出但解不开的扩展名 -> 给用户看的说明
HINT_ONLY_EXT = {
    ".mflac": "QQ音乐新版加密（mflac）",
    ".mflac0": "QQ音乐新版加密（mflac0）",
    ".mgg": "QQ音乐新版加密（mgg）",
    ".mgg1": "QQ音乐新版加密（mgg1）",
    ".mmp4": "QQ音乐新版加密（mmp4）",
    ".tkm": "QQ音乐（tkm）",
    ".kgg": "酷狗新版加密（kgg）",
    ".kwm": "酷我加密（kwm）",
}


class UnlockError(Exception):
    """解锁失败（文件损坏、格式不认识、暂不支持等）。"""


@dataclass
class Unlocked:
    """一次解锁的结果。"""

    path: str            # 输出文件路径
    fmt: str             # 输出格式（mp3 / flac / ogg）
    title: str = ""      # 文件里带的歌名（可能为空）
    artists: list[str] = field(default_factory=list)
    album: str = ""
    size: int = 0
    source: str = ""     # 源文件路径

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def detect(path: str) -> str | None:
    """判断一个文件能不能解锁：返回格式说明，不认识就返回 None。"""
    ext = os.path.splitext(path)[1].lower()
    return SUPPORTED_EXT.get(ext)


def _out_path(src: str, dest_dir: str | None, stem: str, ext: str) -> str:
    dest = dest_dir or os.path.dirname(src)
    os.makedirs(dest, exist_ok=True)
    return os.path.join(dest, safe_name(stem) + ext)


def _sniff_ext(head: bytes) -> str:
    """按文件头猜输出格式。"""
    if head[:3] == b"ID3":
        return ".mp3"
    if head[:4] == b"fLaC":
        return ".flac"
    if head[:4] == b"OggS":
        return ".ogg"
    # mp3 帧同步字
    if len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return ".mp3"
    return ".mp3"


def unlock(path: str, dest_dir: str | None = None, stem: str = "") -> Unlocked:
    """把一个加密音频文件解锁成普通音频文件。

    :param path: 源文件（.ncm / .qmc* …）
    :param dest_dir: 输出目录，默认与源文件同目录
    :param stem: 输出文件名（不含扩展名），默认用源文件名
    :raises UnlockError: 格式不认识、暂不支持或文件损坏
    """
    if not os.path.isfile(path):
        raise UnlockError("文件不存在：%s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext in HINT_ONLY_EXT:
        raise UnlockError("%s 暂不支持自动解锁，请在音乐客户端里「播放」一下再退出，"
                          "或用它的「转换格式/另存」功能导出普通音频" % HINT_ONLY_EXT[ext])
    if ext not in SUPPORTED_EXT:
        raise UnlockError("不认识的加密格式：%s" % (ext or "(无扩展名)"))

    if ext == ".ncm":
        from .ncm import decrypt_ncm
        return decrypt_ncm(path, dest_dir, stem or os.path.splitext(os.path.basename(path))[0])
    if ext in (".kgm", ".kgma", ".vpr"):
        from .kgm import decrypt_kgm
        return decrypt_kgm(path, dest_dir, stem or os.path.splitext(os.path.basename(path))[0])
    from .qmc import decrypt_qmc
    return decrypt_qmc(path, dest_dir, stem or os.path.splitext(os.path.basename(path))[0])


def unlock_dir(directory: str, dest_dir: str | None = None,
               recursive: bool = True) -> tuple[list[Unlocked], list[tuple[str, str]]]:
    """解锁一个目录里所有能解锁的文件。

    返回 ``(成功列表, [(失败文件, 原因), …])``。
    """
    ok: list[Unlocked] = []
    failed: list[tuple[str, str]] = []
    if not os.path.isdir(directory):
        return ok, [(directory, "目录不存在")]
    walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
    for base, _dirs, files in walker:
        for name in files:
            path = os.path.join(base, name)
            if os.path.splitext(name)[1].lower() not in SUPPORTED_EXT:
                continue
            try:
                ok.append(unlock(path, dest_dir))
            except UnlockError as exc:
                failed.append((path, str(exc)))
            except Exception as exc:  # noqa: BLE001 - 单个文件出错不影响其他文件
                failed.append((path, "解锁时出错：%s" % exc))
    return ok, failed


def scan_encrypted(directory: str, recursive: bool = True) -> tuple[list[str], list[str]]:
    """扫描目录里的加密文件：返回 ``(可解锁的, 认得出但解不开的)``。"""
    can: list[str] = []
    hint: list[str] = []
    if not os.path.isdir(directory):
        return can, hint
    walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
    for base, _dirs, files in walker:
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXT:
                can.append(os.path.join(base, name))
            elif ext in HINT_ONLY_EXT:
                hint.append(os.path.join(base, name))
    return can, hint
