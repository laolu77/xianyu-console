"""Win10 中文显示兼容层。

问题背景
--------
中文Windows（Win10）控制台默认代码页是 GBK/936。Python 在这种环境下
**原生就会用 GBK 输出**，中文本身是正常显示的。

真正会出问题的只有两类场景：

1. **UTF-8 模式被打开**（``PYTHONUTF8=1`` 环境变量，或 Python 3.15+
   的默认行为）。此时 ``sys.stdout.encoding`` 变成 utf-8，但控制台仍是
   936，UTF-8 字节被按 GBK 解释，中文显示为「鍑嗗鏃」。
2. **输出被重定向到文件**。此时没有控制台，``encoding`` 可能是
   任意值，日志文件里会混进编码不确定的字节。

历史教训（务必保留）
--------------------
本模块曾尝试用 ``sys.stdout.reconfigure(encoding="cp936")`` 强行纠正。
**这是错的**：在 UTF-8 模式下 reconfigure 会与 Python 的 UTF-8 模式
逻辑冲突，导致不同终端表现不一致（有的正常、有的整片乱码）。
``reconfigure`` 只能改「编码」，改不了「模式」，治标不治本。

正确解法（按优先级）
--------------------
1. 若Python 已处于 UTF-8 模式，就把控制台代码页也切到 65001，
   让**两侧对齐**。这是唯一能让 UTF-8 模式显示正确的办法。
2. 若处于传统模式（默认），**什么都不做**。Python 原生就是 GBK，
   与控制台一致，中文正常。
3. 若输出无控制台（被重定向），强制 UTF-8 + errors="replace"，
   保证日志文件是确定的 UTF-8，且绝不抛异常。

用法（必须在任何其他 import 之前调用）::

    import win10_compat
    win10_compat.setup()

本模块幂等，重复调用无副作用。
"""

from __future__ import annotations

import ctypes
import sys

_done = False

# 常见的非UTF-8 中文代码页
_GBK_CPS = frozenset({936, 950, 134})

# 无控制台时统一使用的编码
_FILE_ENCODING = "utf-8"


def _is_windows() -> bool:
    """判断是否运行在 Windows 上。

    Returns:
        True 表示 Windows。
    """
    return sys.platform.startswith("win")


def _console_output_cp() -> int | None:
    """读取控制台输出代码页。

    Returns:
        代码页数值（如 936 / 65001）；无控制台时返回 None。
    """
    if not _is_windows():
        return None
    try:
        # GetConsoleOutputCP 在没有真实控制台时返回 0
        cp = int(ctypes.windll.kernel32.GetConsoleOutputCP())
        return cp or None
    except Exception:
        return None


def _is_redirected() -> bool:
    """判断标准输出是否被重定向到文件或管道。

    重要：早期用 sys.stdout.isatty() 判断，实测不可靠——
    通过subprocess 捕获输出时 isatty() 为 False，但那只说明
    「当前是管道」，不代表用户看不到。真正的判据是
    GetConsoleOutputCP()：只有拿不到控制台代码页时，
    才说明根本没有控制台。

    Returns:
        True 表示输出未连接控制台（已重定向）。
    """
    return _console_output_cp() is None


def _switch_console_to_utf8() -> bool:
    """把控制台代码页切换到 65001。

    仅当 Python 处于 UTF-8 模式时调用：既然 Python 决定输出 UTF-8 字节，
    控制台就必须按 UTF-8 解释，否则必然乱码。

    Returns:
        切换成功返回 True。
    """
    if not _is_windows():
        return False
    try:
        # SetConsoleOutputCP 需要先把输入也设成同一个值才稳定
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
        return True
    except Exception:
        return False


def _force_file_encoding() -> None:
    """输出无控制台时，强制标准流使用 UTF-8 且不抛异常。

    这样写出的日志文件一定是 UTF-8，可被logview.py 正确解码。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding=_FILE_ENCODING, errors="replace")
        except Exception:
            pass


def setup() -> None:
    """准备标准输出，确保中文正确显示。

    设计原则：**只做诊断与兜底，不与Python 的编码机制对抗**。

    历史教训（两次踩坑，必须遵守）：
    1. 早期用 reconfigure(encoding="cp936") 强行纠正 —— 方向错误。
       reconfigure 只能改「编码」，改不了 Python 的 **UTF-8 模式**；
       且会与 PYTHONIOENCODING 环境变量互相覆盖，导致
       「设了gbk 却输出 UTF-8」这类反直觉现象。
    2. 试图在 Python 内部统一一切 —— 不现实。
       编码选择的权威在 **启动进程的 bat**，那里已经用
       PYTHONUTF8=0 + PYTHONIOENCODING=gbk:replace 钉死。

    因此本模块现在只做三件事：
    - 无控制台（输出重定向到文件）时，强制 UTF-8，
      保证日志文件编码确定、可被 logview.py 正确解码；
    - UTF-8 模式下把控制台代码页也切到 6501，让两侧对齐；
    - 其余情况**完全不动**，尊重 Python 与环境变量的既有行为。

    幂等，重复调用只生效一次。
    """
    global _done
    if _done:
        return
    _done = True

    if not _is_windows():
        return  # 非 Windows 保持系统默认

    # 无控制台：输出重定向到文件，强制 UTF-8 让编码可预期
    if _is_redirected():
        _force_file_encoding()
        return

    # UTF-8 模式：Python 会输出 UTF-8 字节，控制台必须也切到 UTF-8。
    # 这里不改动 stdout 本身，只改控制台代码页。
    if getattr(sys.flags, "utf8_mode", 0):
        _switch_console_to_utf8()


def stream_encoding() -> str:
    """返回当前标准输出的实际编码，供诊断使用。

    Returns:
        编码名称字符串。
    """
    return getattr(sys.stdout, "encoding", "") or "unknown"


def diagnose() -> None:
    """打印当前编码环境，用于排查乱码问题。

    直接运行本模块即可查看：``python win10_compat.py``
    """
    setup()
    cp = _console_output_cp()
    print("=" * 60)
    print("中文显示环境诊断")
    print("=" * 60)
    print(f"操作系统      : {sys.platform}")
    print(f"Python 版本   : {sys.version.split()[0]}")
    print(f"UTF-8 模式    : {bool(getattr(sys.flags, 'utf8_mode', 0))}")
    print(f"输出已重定向  : {_is_redirected()}")
    print(f"控制台代码页  : {cp if cp else '无（输出已重定向）'}")
    print(f"stdout 编码   : {stream_encoding()}")
    print()
    print("中文显示测试：闲鱼卖家客服 - 包邮 / 运费 / 发货时间")
    print("=" * 60)


if __name__ == "__main__":
    diagnose()