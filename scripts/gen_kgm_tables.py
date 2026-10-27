# -*- coding: utf-8 -*-
"""把 @jixun/kugou-crypto 的掩码表转成 Python 字面量（一次性生成器）。

掩码表是算法的一部分（酷狗格式的固定数据），来源：
- unlock-music  https://github.com/unlock-music/unlock-music  (MIT)
- @jixun/kugou-crypto  https://git.unlock-music.dev/um/kugou-crypto  (MIT)
"""
from __future__ import annotations

import re

SRC = r"C:\Users\Administrator\Desktop\unlock-music\node_modules\@jixun\kugou-crypto\dist\data\tables.js"

NAMES = ["table1", "table2", "tableV2", "vprKey"]


def parse(text: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for name in NAMES:
        start = text.index("exports.%s = [" % name) + len("exports.%s = [" % name)
        end = text.index("];", start)
        body = text[start:end]
        values = [int(v, 16) for v in re.findall(r"0x[0-9a-fA-F]{1,2}", body)]
        out[name] = values
    return out


def render(name: str, values: list[int]) -> str:
    lines = []
    for i in range(0, len(values), 16):
        chunk = ", ".join("0x%02X" % v for v in values[i:i + 16])
        lines.append("    " + chunk + ",")
    return "%s: tuple[int, ...] = (\n%s\n)\n" % (name.replace("table", "TABLE_").replace("vprKey", "VPR_KEY"),
                                                 "\n".join(lines))


def main() -> int:
    with open(SRC, encoding="utf-8") as fh:
        data = parse(fh.read())
    for name in NAMES:
        print("# %s：%d 字节" % (name, len(data[name])))
        print(render(name, data[name]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
