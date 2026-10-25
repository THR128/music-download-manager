# -*- coding: utf-8 -*-
"""汽水音乐适配器 —— 只做占位，别指望它能用。

为什么不做（诚实说明）：
- 汽水音乐（字节跳动）只有 App，没有公开的 Web / 开放 API；
- 已知的第三方接口都带设备签名（签名头 / X-Bogus 之类），随版本变化，且拿不到公开文档；
- 所以本适配器 ``supports_download = False``：``check_login`` / ``list_tracks`` /
  ``download`` 一律给出明确的 Unsupported 说明，**不假装能用**，也不去破解客户端。

cookie 仅用于设置界面保持字段一致（每个平台都有 cookie 一栏），不参与任何请求。
"""
from __future__ import annotations

from .base import BaseAdapter, NotLoggedIn, Track, Unsupported


class SodaAdapter(BaseAdapter):
    """汽水音乐占位适配器：所有在线能力都抛 Unsupported。"""

    key = "soda"
    title = "汽水音乐"
    supports_download = False
    #: 不能下载，就没有"可选音质"这回事
    quality_levels = ()

    def ensure_login(self) -> None:
        """空 cookie 时按契约抛 NotLoggedIn（说明里带"cookie"），否则抛 Unsupported。"""
        raw = ((self.config or {}).get("cookie") or "").strip()
        if not raw:
            raise NotLoggedIn(
                "汽水音乐没有公开接口，不需要登录、也不能自动下载；请改用网易云 / QQ音乐 / 酷狗")
        raise Unsupported("汽水音乐没有公开 API，无法校验 cookie 登录态（仅占位支持）")

    def list_tracks(self, playlist: str) -> list[Track]:
        """汽水音乐没有公开 API，不假装能拉歌单。"""
        raise Unsupported("汽水音乐没有公开 API，无法拉取歌单（请改用网易云 / QQ音乐 / 酷狗）")

    def download(self, track: Track, dest_dir: str, quality: str = "320") -> str:
        """汽水音乐没有公开 API，不假装能下载。"""
        raise Unsupported("汽水音乐没有公开 API，无法下载（请改用网易云 / QQ音乐 / 酷狗）")

    def search(self, keyword: str, limit: int = 5) -> list[Track]:
        """换源时 sync 会调用 search；汽水音乐同样明确拒绝，而不是静默返回空列表。"""
        raise Unsupported("汽水音乐没有公开 API，无法搜索（换源请用其它平台）")
