# -*- coding: utf-8 -*-
"""
兼容 CLI 调度入口（已统一委托至 cli.main:main）
注意：旧版 argparse 子命令参数格式（如 benchmark --dataset ...）已替换为新版分组命令树，
本文件仅保留路径入口兼容。常用调用方式:
  python backend/cli.py system capabilities
  python backend/cli.py benchmark tasks
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from cli.main import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
