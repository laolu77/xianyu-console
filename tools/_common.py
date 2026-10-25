"""采集/导出/批量脚本共用的小工具（消除重复实现）。

为什么单独成文件：`collect_shop_products.py` 和 `export_runner.py` 各自
复制了一份「单实例检查」逻辑，两边只有 docstring 里的"采集"/"导出"不同。
改一处忘另一处，就会出现"采集能防重、导出不能"的怪现象（2026-10-17 踩过）。
放这里之后只有一份实现，谁要改都改这一处。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# ---- 统一路径：所有脚本都在 tools/ 下，相对位置固定，不必各自算 ----
ROOT = Path(__file__).resolve().parents[1]      # xianyu-software/
DATA = ROOT / "data"
BACKEND = ROOT / "backend"


def log(msg: str, tag: str = "") -> None:
    """统一日志输出（带模块前缀，flush 保证管道里能实时看到）。

    Args:
        msg: 日志正文。
        tag: 模块标签，如 "batch" / "publish"；空则不打印前缀。
    """
    print(f"[{tag}] {msg}" if tag else msg, flush=True)


def write_progress(path: Path, data: dict) -> None:
    """原子地写进度文件；失败静默（进度写不出来不该拖垮主任务）。

    各脚本的进度字段不同（updated_at / at / trigger…），时间戳由调用方
    自己塞进 data，这里只负责"建目录 + 写 JSON + 不抛异常"。

    Args:
        path: 进度 JSON 路径。
        data: 要写入的内容（会被 json 序列化）。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def progress_is_running(path: Path, stale_sec: int) -> bool:
    """进度文件显示"正在跑"且心跳未过期时返回 True。

    Args:
        path: 进度 JSON 路径。
        stale_sec: 超过这个秒数没更新就认为进程已死（可重新启动）。

    Returns:
        True = 真的还有任务在跑。
    """
    try:
        if not path.exists():
            return False
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("status") != "running":
            return False
        ts = datetime.strptime(d.get("updated_at", ""), "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - ts).total_seconds() <= stale_sec
    except Exception:  # noqa: BLE001
        # 文件损坏/时间格式异常：当作没在跑，让用户能重试
        return False


def check_single_instance(path: Path, stale_sec: int) -> bool:
    """没有别的任务在跑时返回 True（可以启动）。

    Args:
        path: 该任务的进度 JSON 路径。
        stale_sec: 心跳过期阈值（秒）。

    Returns:
        True = 可以启动；False = 已有任务在跑，别重复启动。
    """
    return not progress_is_running(path, stale_sec)
