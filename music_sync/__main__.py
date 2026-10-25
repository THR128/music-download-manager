# -*- coding: utf-8 -*-
"""支持 ``python -m music_sync``。"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
