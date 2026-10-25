"""闲鱼软件控制台日志查看器（解决 Windows 控制台中文乱码）。

问题背景
--------
闲鱼卖家客服是Electron 应用，其主进程调试日志直接写到 stdout，
字节流是 **UTF-8 无 BOM**（实测验证）。而中文 Windows 的
cmd 控制台默认代码页是 GBK/936，UTF-8 字节被按 GBK 解释，
于是「准备播放音频」显示成「鍑嗗鏃鎾鏅捐闊抽」。

为什么不能在 bat 里用 chcp 65001 解决
------------------------------------
cmd.exe 是**逐行**按当前代码页解析 bat 文件的。若在 bat 开头
执行 chcp 65001，其后所有 GBK 编码的中文行都会被当成 UTF-8
解析，反而产生更多乱码，属于按下葫芦浮起瓢。

本模块的解法
------------
1. 启动器把软件 stdout/stderr **重定向到 UTF-8 文本文件**，
   控制台不再被污染。
2. 本查看器读取该文件时显式按 UTF-8 解码，再按控制台实际
   代码页（cp936）转输出，中文即正常显示。
3. 日志若确为 UTF-8，直接显示；万一混入了非 UTF-8 字节，
   用 errors="replace" 兜底，绝不抛异常。

用法：
    python logview.py            查看最新日志末尾 40 行
    python logview.py -n 200     查看末尾 200 行
    python logview.py 启动       查看包含“启动”的日志文件
    python logview.py -f         持续跟踪输出（类似 tail -f）
"""

from __future__ import annotations

# 必须在其他 import 之前：锁定标准输出编码，保证中文正确显示。
# 详见 win10_compat 模块顶部的说明——这里不能靠 reconfigure
# 纠正 UTF-8 模式，必须让控制台代码页与 Python 输出编码对齐。
import win10_compat

win10_compat.setup()

import argparse
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

#软件主进程控制台日志文件名（由启动器重定向生成）
CONSOLE_LOG = LOG_DIR / "xianyu_console.log"

# 其他可能包含中文的日志，按优先级排序
LOG_CANDIDATES = (
    CONSOLE_LOG,
    *sorted(LOG_DIR.glob("autoreply_*.log"), reverse=True),
)


def find_log(keyword: str | None) -> Path | None:
    """定位要查看的日志文件。

    Args:
        keyword: 文件名关键字。为 None 时按默认优先级选择最新的一个。

    Returns:
        日志文件路径；没有可用日志时返回 None。
    """
    candidates = [p for p in LOG_CANDIDATES if p.is_file()]
    if not candidates:
        # logs 目录里可能有别的名字，兜底扫描
        candidates = sorted(LOG_DIR.glob("*.log"), reverse=True)
    if not candidates:
        return None

    if keyword:
        matches = [p for p in candidates if keyword in p.name]
        if not matches:
            return None
        candidates = matches

    return max(candidates, key=lambda p: p.stat().st_mtime)


def _chinese_ratio(text: str) -> float:
    """统计文本中CJK 汉字的占比。

    用途：GBK 字节流有相当概率能被 UTF-8「成功」解码但得到无意义字符，
    单靠 try/except 判断不出对错，必须用内容合理性做校验。

    Args:
        text: 待检查文本。

    Returns:
        汉字占比（0.0 ~ 1.0）。若文本里没有任何中日韩统一表意文字，
        说明不适合作为中文文本，返回 0.0。
    """
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk == 0:
        return 0.0
    return cjk / len(text)


def detect_encoding(path: Path) -> tuple[str, str]:
    """读取日志并判定其编码。

    背景：Electron 原生输出是 UTF-8，但 bat 里设置了
    PYTHONIOENCODING=gbk，某些情况下日志文件会是 GBK 字节。
    两种编码都可能遇到，且 GBK 字节有相当概率能被 UTF-8
    「成功」解码成无意义字符，因此必须用中文合理性来打分，
    不能只看能否解码。

    Args:
        path: 日志文件路径。

    Returns:
        (编码名, 解码后的文本) 二元组。
    """
    raw = path.read_bytes()

    best_enc = "utf-8"
    best_text = raw.decode("utf-8", errors="replace")
    best_score = _chinese_ratio(best_text)

    for enc in ("gbk",):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        score = _chinese_ratio(text)
        # 只在明显更优时才替换，避免无中文的纯英文日志被误判
        if score > best_score + 0.05:
            best_enc, best_text, best_score = enc, text, score

    return best_enc, best_text


def read_text(path: Path) -> str:
    """读取日志文本，自动识别 UTF-8 与 GBK。

    Args:
        path: 日志文件路径。

    Returns:
        解码后的文本内容。
    """
    return detect_encoding(path)[1]


def tail(text: str, lines: int) -> str:
    """取文本末尾若干行。

    Args:
        text: 完整文本。
        lines: 保留的行数。

    Returns:
        末尾 lines 行组成的字符串。
    """
    parts = text.splitlines()
    if len(parts) <= lines:
        return "\n".join(parts)
    return "\n".join(parts[-lines:])


# 跟随模式使用的编码，由 detect_encoding 填充
_log_encoding: list = ["utf-8"]


def follow(path: Path, lines: int) -> None:
    """持续跟踪日志新增内容。

    Args:
        path: 日志文件路径。
        lines: 首次显示的末尾行数。
    """
    print(f"[跟随模式] 正在跟踪 {path.name}，按 Ctrl+C 退出\n")
    pos = path.stat().st_size
    # 先输出已有内容的末尾若干行
    existing = tail(read_text(path), lines)
    if existing:
        print(existing)
    try:
        while True:
            time.sleep(1.0)
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size < pos:  # 日志被截断或重建
                pos = 0
            if size == pos:
                continue
            with path.open("rb") as fh:
                fh.seek(pos)
                chunk = fh.read()
            pos += len(chunk)
            # 新增片段沿用整份日志的编码判定结果，
            # 不能固定按 UTF-8 解（文件可能是 GBK）
            sys.stdout.write(chunk.decode(_log_encoding[0], errors="replace"))
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n[已退出跟随模式]")


def main() -> int:
    """查看器入口。

    Returns:
        进程退出码；0 表示成功，1 表示未找到日志。
    """
    parser = argparse.ArgumentParser(description="闲鱼软件控制台日志查看器（UTF-8 正确解码）")
    parser.add_argument("keyword", nargs="?", default=None, help="日志文件名关键字")
    parser.add_argument("-n", "--lines", type=int, default=40, help="显示末尾行数，默认 40")
    parser.add_argument("-f", "--follow", action="store_true", help="持续跟踪输出")
    args = parser.parse_args()

    path = find_log(args.keyword)
    if path is None:
        print("没有找到日志文件。")
        print(f"日志目录: {LOG_DIR}")
        print()
        print("请先双击 启动软件.bat 运行一次，软件的控制台日志会写入:")
        print(f"  {CONSOLE_LOG}")
        return 1

    print("=" * 70)
    print(f"日志文件: {path}")
    print(f"大小: {path.stat().st_size} 字节    修改时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(path.stat().st_mtime))}")
    print("=" * 70)

    if args.follow:
        follow(path, args.lines)
        return 0

    text = read_text(path)
    if not text.strip():
        print("\n（日志为空，软件可能尚未产生输出）")
        return 0

    print()
    print(tail(text, args.lines))
    print()
    print("-" * 70)
    print(f"共 {len(text.splitlines())} 行，已显示末尾 {min(args.lines, len(text.splitlines()))} 行")
    print("提示: 加 -f 参数可持续跟踪，加 -n 500 可看更多历史")
    return 0


if __name__ == "__main__":
    sys.exit(main())