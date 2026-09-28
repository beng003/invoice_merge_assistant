#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发票合并小助手 —— 透传包装（macOS / Linux / Windows 通用）。

只做两件事：找到同目录的 invoice_merge.py、把参数原样交给当前解释器执行。
**运行本脚本必须显式带上 Python 路径**（`<python路径> run.py <参数>`），
用哪个解释器由调用方（agent）自己指定 —— 本脚本不做任何解释器探测，
拉起它的那个 Python 就是最终干活的解释器，请自行确认它已装 PyMuPDF
（没装会报 ModuleNotFoundError，装依赖是调用方的事）。

本 skill 完全自包含：工具本体就是同目录下的 invoice_merge.py，不依赖任何外部仓库或目录结构，
整个 skill 目录可以原样拷到别的机器。

用法与 invoice_merge.py 的命令行完全一致：
  python run.py --folder ./某次出差 --dry-run
  python run.py --folder ./某次出差 -o /tmp/合并.pdf

注意：invoice_merge.py 不带参数会打开图形界面并阻塞，本脚本对无参调用直接拒绝并提示。
可用环境变量 INVOICE_MERGE_PY 改用别的 invoice_merge.py（默认用同目录自带的那份）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = sys.argv[1:]
    if not args:
        print("必须带参数运行（不带参数 invoice_merge.py 会打开图形界面并阻塞）。"
              "示例：python run.py --folder <发票目录>", file=sys.stderr)
        return 2

    target = os.environ.get("INVOICE_MERGE_PY") or str(SCRIPT_DIR / "invoice_merge.py")
    if not Path(target).is_file():
        print("找不到 invoice_merge.py（当前找的是 %s）。它应与本脚本在同一目录；"
              "也可用 INVOICE_MERGE_PY 指定路径。" % target, file=sys.stderr)
        return 3

    # 解释器就是拉起本脚本的那一个（sys.executable），由调用方显式选定，不做任何探测。
    if not sys.executable:
        print("无法确定当前解释器路径（sys.executable 为空）。", file=sys.stderr)
        return 4

    # Windows 下管道输出默认走本地编码（GBK），给子进程统一成 UTF-8，免得调用方读到乱码。
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        proc = subprocess.run([sys.executable, target] + args, env=env)
    except KeyboardInterrupt:
        return 130
    rc = proc.returncode
    # POSIX 上子进程被信号杀死时 returncode 为负，折算成 shell 惯例的 128+n。
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    raise SystemExit(main())
