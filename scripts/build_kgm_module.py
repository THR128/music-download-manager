# -*- coding: utf-8 -*-
"""把 kgm_head.txt + 生成的掩码表 + 函数体拼成 music_sync/unlock/kgm.py。"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEAD = os.path.join(ROOT, "music_sync", "unlock", "kgm_head.txt")
TABLES = os.path.join(os.environ["TEMP"], "kgm_tables.py")
OUT = os.path.join(ROOT, "music_sync", "unlock", "kgm.py")

BODY = '''
def _mask_at(offset: int) -> int:
    """掩码：低 8 位来自固定的 272 字节表，高位由偏移量逐层折叠异或而来。"""
    value = 0
    high = offset >> 4
    while high > 0:
        value ^= TABLE_1[high % TABLE_SIZE]
        high >>= 4
        value ^= TABLE_2[high % TABLE_SIZE]
        high >>= 4
    return TABLE_V2[offset % TABLE_SIZE] ^ value


def _xor_low_half(value: int) -> int:
    return value ^ ((value & 0x0F) << 4)


def _decrypt_byte(byte: int, key: bytes, offset: int) -> int:
    return _xor_low_half(_mask_at(offset) ^ byte ^ key[offset % 17])


def _decrypt_stream(handle: BinaryIO, out: BinaryIO, key: bytes, vpr: bool) -> None:
    offset = 0
    while True:
        block = handle.read(CHUNK)
        if not block:
            break
        buf = bytearray(len(block))
        for i, byte in enumerate(block):
            pos = offset + i
            value = _xor_low_half(_mask_at(pos) ^ byte ^ key[pos % 17])
            if vpr:
                value ^= VPR_KEY[pos % 17]
            buf[i] = value
        out.write(buf)
        offset += len(block)


def decrypt_kgm(path: str, dest_dir: str | None = None, stem: str = "") -> Unlocked:
    """解密酷狗 .kgm / .kgma / .vpr，输出同目录（或 dest_dir）下的普通音频文件。"""
    size = os.path.getsize(path)
    if size < 0x3C:
        raise UnlockError("文件太小，不像是酷狗加密音频")

    with open(path, "rb") as handle:
        head = handle.read(0x3C)
        if head.startswith(KGM_HEADER):
            vpr = False
            fmt = "kgm"
        elif head.startswith(VPR_HEADER):
            vpr = True
            fmt = "vpr"
        else:
            raise UnlockError("不是酷狗加密音频（文件头不匹配）")

        (header_len,) = struct.unpack("<I", head[0x10:0x14])
        if header_len < 0x3C or header_len > size:
            raise UnlockError("酷狗加密文件头损坏（音频偏移异常）")

        key = bytes(head[0x1C:0x2C]) + b"\\x00"

        handle.seek(header_len)
        tail = handle.read(64)
        ext = _sniff_ext(tail) or ".mp3"
        handle.seek(header_len)

        out_path = _out_path(path, dest_dir, stem, ext)
        tmp_path = out_path + ".part"
        try:
            with open(tmp_path, "wb") as out:
                _decrypt_stream(handle, out, key, vpr)
            if os.path.getsize(tmp_path) < 128:
                raise UnlockError("解密后的文件异常小，可能不是受支持的酷狗格式")
            os.replace(tmp_path, out_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    return Unlocked(path=out_path, fmt=fmt, title=stem, artists=[], album="",
                    size=os.path.getsize(out_path), source=path)


def encrypt_kgm(data: bytes, key: bytes) -> bytes:
    """仅供测试用：按同一算法把明文音频「加密」，验证解密可逆。"""
    if len(key) != 17:
        raise ValueError("key 必须是 17 字节")
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        value = _xor_low_half(_mask_at(i) ^ byte ^ key[i % 17])
        out[i] = value
    return bytes(out)
'''

with open(HEAD, encoding="utf-8") as fh:
    text = fh.read()
with open(TABLES, encoding="utf-8") as fh:
    tables = fh.read()
tables = "\n".join(line for line in tables.splitlines() if not line.startswith("# "))
with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(text.rstrip("\n") + "\n\n" + tables.rstrip("\n") + "\n" + BODY)
print("已生成", OUT, os.path.getsize(OUT), "字节")
