# -*- coding: utf-8 -*-
"""QQ音乐 ``.qmc0/.qmc2/.qmc3/.qmcflac/.qmcogg`` 解锁（QMCv1，静态掩码）。

算法：整文件逐字节异或一个 8x7 的静态表生成的伪随机掩码，掩码按
「之」字形在表里走位，每 0x8000 字节插入两个固定字节（0xC3 / 0xD8），
因此**掩码与偏移强相关，不能只解前一段**。

输出格式按解密后的文件头判断：``ID3`` / ``0xFF 0xEx`` → mp3，``fLaC`` → flac，
``OggS`` → ogg。

算法参考 presburger/qmc-decoder（MIT），此处按算法自行实现。
"""
from __future__ import annotations

import os

from . import UnlockError, Unlocked, _out_path, _sniff_ext

SEED_MAP = (
    (0x4A, 0xD6, 0xCA, 0x90, 0x67, 0xF7, 0x52),
    (0x5E, 0x95, 0x23, 0x9F, 0x13, 0x11, 0x7E),
    (0x47, 0x74, 0x3D, 0x90, 0xAA, 0x3F, 0x51),
    (0xC6, 0x09, 0xD5, 0x9F, 0xFA, 0x66, 0xF9),
    (0xF3, 0xD6, 0xA1, 0x90, 0xA0, 0xF7, 0xF0),
    (0x1D, 0x95, 0xDE, 0x9F, 0x84, 0x11, 0xF4),
    (0x0E, 0x74, 0xBB, 0x90, 0xBC, 0x3F, 0x92),
    (0x00, 0x09, 0x5B, 0x9F, 0x62, 0x66, 0xA1),
)

BLOCK = 0x8000


class QmcMask:
    """生成 QMCv1 掩码流（与 C++ 参考实现逐字节一致）。"""

    def __init__(self) -> None:
        self.x = -1
        self.y = 8
        self.dx = 1
        self.index = -1

    def next_mask(self) -> int:
        self.index += 1
        if self.x < 0:
            self.dx = 1
            self.y = (8 - self.y) % 8
            ret = 0xC3
        elif self.x > 6:
            self.dx = -1
            self.y = 7 - self.y
            ret = 0xD8
        else:
            ret = SEED_MAP[self.y][self.x]

        self.x += self.dx
        if self.index == BLOCK or (self.index > BLOCK and (self.index + 1) % BLOCK == 0):
            return self.next_mask()
        return ret

    def generate(self, length: int) -> bytes:
        return bytes(self.next_mask() for _ in range(length))


def decrypt_bytes(data: bytes) -> bytes:
    """对整段数据做掩码还原。"""
    mask = QmcMask().generate(len(data))
    return bytes(a ^ b for a, b in zip(data, mask))


def _title_from_name(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem


def decrypt_qmc(path: str, dest_dir: str | None = None, stem: str = "") -> Unlocked:
    with open(path, "rb") as fh:
        data = fh.read()
    if not data:
        raise UnlockError("文件是空的")
    if len(data) < 4:
        raise UnlockError("文件太小，不是有效的 qmc 文件")

    plain = decrypt_bytes(data)
    ext = _sniff_ext(plain[:16])
    if ext == ".mp3" and not (plain[:3] == b"ID3" or (plain[0] == 0xFF and (plain[1] & 0xE0) == 0xE0)):
        raise UnlockError("解锁后的内容不像音频：可能是不支持的 QMCv2 加密（.mflac/.mgg）")

    out = _out_path(path, dest_dir, stem or _title_from_name(path), ext)
    tmp = out + ".part"
    with open(tmp, "wb") as fh:
        fh.write(plain)
    os.replace(tmp, out)

    return Unlocked(path=out, fmt=ext.lstrip("."), title="", artists=[], album="",
                    size=len(plain), source=path)


__all__ = ["decrypt_qmc", "decrypt_bytes", "QmcMask", "SEED_MAP"]
