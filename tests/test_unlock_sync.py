# -*- coding: utf-8 -*-
"""同步流程里的解锁环节：扫客户端下载目录 → 解锁 → 按「歌手 - 歌名」归入曲库。"""

from __future__ import annotations

import os

from music_sync import sync as sync_mod
from test_unlock import make_ncm

AUDIO = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64


def _cfg(tmp_path, **unlock) -> dict:
    client = tmp_path / "client"
    library = tmp_path / "lib"
    client.mkdir()
    library.mkdir()
    settings = {"enabled": True, "scan_library": False, "remove_source": False}
    settings.update(unlock)
    return {"library_dir": str(library), "client_download_dir": str(client), "unlock": settings}


def test_unlock_downloads_renames_by_metadata(tmp_path):
    cfg = _cfg(tmp_path)
    src = os.path.join(cfg["client_download_dir"], "12345678.ncm")
    with open(src, "wb") as fh:
        fh.write(make_ncm(AUDIO, title="测试歌名", artists=("歌手甲", "歌手乙")))

    result = sync_mod.SyncResult(time="t", platform="netease")
    sync_mod.unlock_downloads(cfg, result)

    files = os.listdir(cfg["library_dir"])
    assert files == ["歌手甲; 歌手乙 - 测试歌名.mp3"]
    assert len(result.unlocked) == 1
    assert not result.unlock_failed
    # 默认保留原加密文件
    assert os.path.exists(src)


def test_unlock_downloads_remove_source(tmp_path):
    cfg = _cfg(tmp_path, remove_source=True)
    src = os.path.join(cfg["client_download_dir"], "a.ncm")
    with open(src, "wb") as fh:
        fh.write(make_ncm(AUDIO, title="要删源的歌", artists=("歌手丙",)))

    result = sync_mod.SyncResult(time="t", platform="netease")
    sync_mod.unlock_downloads(cfg, result)

    assert os.listdir(cfg["library_dir"]) == ["歌手丙 - 要删源的歌.mp3"]
    assert not os.path.exists(src)


def test_unlock_downloads_disabled_does_nothing(tmp_path):
    cfg = _cfg(tmp_path, enabled=False)
    with open(os.path.join(cfg["client_download_dir"], "a.ncm"), "wb") as fh:
        fh.write(make_ncm(AUDIO))

    result = sync_mod.SyncResult(time="t", platform="netease")
    sync_mod.unlock_downloads(cfg, result)

    assert not result.unlocked
    assert os.listdir(cfg["library_dir"]) == []


def test_unlock_downloads_hints_unsupported(tmp_path):
    cfg = _cfg(tmp_path)
    with open(os.path.join(cfg["client_download_dir"], "x.mflac"), "wb") as fh:
        fh.write(b"fLaC" + b"\0" * 32)

    result = sync_mod.SyncResult(time="t", platform="netease")
    sync_mod.unlock_downloads(cfg, result)

    assert result.unlock_hint == ["x.mflac"]
    assert not result.unlocked


def test_unlock_downloads_reports_failure(tmp_path):
    cfg = _cfg(tmp_path)
    with open(os.path.join(cfg["client_download_dir"], "broken.ncm"), "wb") as fh:
        fh.write(b"NOTANCM" + b"\0" * 64)

    result = sync_mod.SyncResult(time="t", platform="netease")
    sync_mod.unlock_downloads(cfg, result)

    assert not result.unlocked
    assert len(result.unlock_failed) == 1
    assert "broken.ncm" in result.unlock_failed[0]
