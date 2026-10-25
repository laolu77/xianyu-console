#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用「后台脱离启动器」：悄悄把任意脚本拉起来，不留控制台窗口。

============================================================================
为什么需要
============================================================================
php 内置服务器用 proc_open 直接拉 python 脚本，有两个副作用：

1. **弹黑框**：python.exe 是 console 子系统程序，即使重定向了 stdio，
   Windows 仍会给它分配控制台 —— 界面上就会冒出一个黑色 cmd 窗口
   （2026-10-22 用户截图反馈：内网穿透启动时出现黑框）。
   解法是 `CREATE_NO_WINDOW`（0x08000000）：创建进程但不创建控制台。

2. **父进程被卡**：长驻子进程会继承 php 的 socket 句柄，导致 HTTP 请求
   永不返回（内网穿透实测卡 25 秒）。
   解法是让 PHP 只调用本文件（毫秒级退出），由本文件把真正的任务脱离出去。

============================================================================
用法（api.php 调用）
============================================================================
    python tools/detach_run.py --log data/logs/batch.log --pidfile data/batch.pid \\
        -- tools/batch_publish.py --run "D:/闲鱼批量上传"

`--` 之后是目标脚本及其参数，原样透传。标准输出只打印一行：目标进程 PID。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "runtime" / "python" / "Scripts" / "python.exe"

CREATE_NO_WINDOW = 0x08000000        # 不创建控制台窗口（黑框的根治）
CREATE_NEW_PROCESS_GROUP = 0x00000200


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--log", required=True, help="stdout/stderr 落盘路径")
    ap.add_argument("--pidfile", default="", help="写 PID 的文件（可选）")
    ap.add_argument("target", nargs=argparse.REMAINDER, help="-- 目标脚本及其参数")
    args = ap.parse_args()

    target = [x for x in args.target if x != "--"]
    if not target:
        print("ERR 缺少目标脚本")
        return 2

    script = Path(target[0])
    if not script.is_absolute():
        script = ROOT / script
    if not script.is_file():
        print(f"ERR 脚本不存在: {script}")
        return 2

    log_path = Path(args.log)
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "a", encoding="utf-8", errors="replace")

    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",     # 日志实时落盘，前端轮询才看得到进度
        "NO_PROXY": "127.0.0.1,localhost",
    })
    cmd = [str(PY if PY.is_file() else sys.executable), str(script), *target[1:]]
    try:
        proc = subprocess.Popen(
            cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, cwd=str(ROOT),
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            proc = subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, cwd=str(ROOT), creationflags=CREATE_NO_WINDOW,
                close_fds=True)
        except Exception as exc2:  # noqa: BLE001
            print(f"ERR 启动失败: {exc2}")
            return 1

    if args.pidfile:
        pf = Path(args.pidfile)
        if not pf.is_absolute():
            pf = ROOT / pf
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(proc.pid), encoding="utf-8")
    print(proc.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
