"""原生 CDP 客户端：枚举并连接所有 target。

为什么不用 Playwright
--------------------
实测 `connect_over_cdp()` 后 `browser.contexts[].pages` 只返回**外壳页面**
（file:// 开头，只有 3 个窗口按钮），拿不到真正的聊天界面。

原因是聊天界面在 `https://seller.goofish.com/`，属于**独立浏览器上下文**
（Electron WebContentsView / 不同 site instance），Playwright 的高层
`pages` 列表不会包含它，必须通过 CDP 的 `Target.getTargets` 枚举，
再用 `Target.attachToTarget` 逐个附着。

本模块提供最小可用的 CDP 通信能力，仅依赖标准库
（WebSocket 用标准库实现，不引入额外依赖）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sys
import struct
import urllib.request
from typing import Any


# ---------------------------------------------------------------- HTTP

def http_get_json(url: str, timeout: float = 8.0) -> Any:
    """GET 一个 JSON 接口。

    Args:
        url: 目标地址。
        timeout: 超时秒数。

    Returns:
        解析后的 JSON。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_targets(port: int) -> list[dict]:
    """列出全部 CDP target。

    Args:
        port: 调试端口。

    Returns:
        target 字典列表。
    """
    return http_get_json(f"http://127.0.0.1:{port}/json/list")


# ------------------------------------------------------------ WebSocket

class WSClient:
    """极简 WebSocket 客户端（仅实现客户端所需的最小功能）。

    自己实现而不引入 websocket-client 依赖，是为了让采集工具
    在任何 Python 3.10+ 环境开箱可用。
    """

    def __init__(self, url: str, timeout: float = 30.0):
        """建立连接。

        Args:
            url: ws:// 形式的地址。
            timeout: 读写超时秒数。
        """
        assert url.startswith("ws://"), f"仅支持 ws://，收到: {url}"
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port_s = hostport.partition(":")
        port = int(port_s) if port_s else 80

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._handshake(host, port, path)

    def _handshake(self, host: str, port: int, path: str) -> None:
        """完成 HTTP Upgrade 握手。

        Args:
            host: 主机名。
            port: 端口。
            path: 请求路径。
        """
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())

        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手失败：连接被关闭")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket 握手失败: {head[:80]!r}")
        self._buf = rest

    def _recv_exact(self, n: int) -> bytes:
        """精确读取 n 字节。

        Args:
            n: 字节数。

        Returns:
            读到的数据。
        """
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("连接被关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, payload: str) -> None:
        """发送文本帧。

        Args:
            payload: 文本内容。
        """
        data = payload.encode("utf-8")
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        """接收一个完整文本帧。

        Returns:
            文本内容。
        """
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length)
            if opcode == 0x8:            # close
                raise ConnectionError("对端关闭连接")
            if opcode == 0x9:            # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode in (0x1, 0x2):     # text / binary
                return payload.decode("utf-8", errors="replace")

    def close(self) -> None:
        """关闭连接。"""
        try:
            self.sock.close()
        except Exception:
            pass


# ------------------------------------------------------------- CDP 会话

class CDPSession:
    """一条已附着的 CDP 连接。"""

    def __init__(self, ws_url: str, session_id: str | None = None,
                 timeout: float = 30.0):
        """附着到目标。

        Args:
            ws_url: 目标 WebSocket 地址。
            session_id: 会话 id（attach 到已有 target 时必填）。
            timeout: 超时秒数。
        """
        self.ws = WSClient(ws_url, timeout=timeout)
        self.session_id = session_id
        self._msg_id = 0
        self._enabled = False

    def send(self, method: str, params: dict | None = None,
             timeout: float = 25.0) -> dict:
        """发送命令并等待响应。

        Args:
            method: CDP 方法名。
            params: 参数。
            timeout: 超时秒数。

        Returns:
            响应的 result 字段。

        Raises:
            TimeoutError: 等待超时。
        """
        self._msg_id += 1
        mid = self._msg_id
        msg: dict = {"id": mid, "method": method, "params": params or {}}
        if self.session_id:
            msg["sessionId"] = self.session_id
        self.ws.send(json.dumps(msg))

        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            resp = json.loads(self.ws.recv())
            if resp.get("id") == mid:
                if "error" in resp:
                    raise RuntimeError(f"{method} 失败: {resp['error']}")
                return resp.get("result", {})
        raise TimeoutError(f"{method} 超时 {timeout}s")

    def evaluate(self, expression: str, timeout: float = 25.0) -> Any:
        """在页面中执行 JS 并按值返回。

        关键：必须先调``Runtime.enable``。未enable 时，
        ``Runtime.evaluate`` 在部分Electron 版本上会返回空的
        ``result`` 对象（表现为Python 侧收到 ``{}``），
        导致看起来像「页面里什么都没取到」。

        返回值统一用 ``returnByValue`` + JSON 字符串中转，
        因为复杂对象在 ``returnByValue`` 下可能无法正确序列化。

        Args:
            expression: JS 源码。
            timeout: 超时秒数。

        Returns:
            JS 返回的值。复杂结构以字符串形式返回，需自行 ``json.loads``。
        """
        if not self._enabled:
            self.send("Runtime.enable", {}, timeout=timeout)
            self._enabled = True

        # 关键：传箭头函数源码时，CDP 会把「函数本身」当作求值结果
        # 返回（type: "function"），Python 侧收到空 dict。
        # 必须包装成 IIFE 立即执行，才能拿到真正的返回值。
        expr = expression.strip()
        if expr.startswith("() =>") or expr.startswith("function"):
            expr = f"({expr})()"

        res = self.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            desc = det.get("exception", {}).get("description") or det.get("text")
            raise RuntimeError(f"JS 异常: {desc}")
        if os.environ.get("CDP_DEBUG"):
            print("[cdp raw]", json.dumps(res, ensure_ascii=False)[:600], file=sys.stderr)

        result = res.get("result", {})
        value = result.get("value")

        # 复杂对象在 returnByValue 下可能丢成空 dict，
        # 此时结果里会有 objectId，说明需要改用 JSON 字符串中转。
        if isinstance(value, dict) and not value:
            obj_id = result.get("objectId")
            if obj_id:
                dumped = self.send(
                    "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": "function(){return JSON.stringify(this);}",
                        "objectId": obj_id,
                        "returnByValue": True,
                    },
                    timeout=timeout,
                )
                raw = dumped.get("result", {}).get("value")
                if isinstance(raw, str):
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return raw
        return value

    def mouse_click(self, x: float, y: float, timeout: float = 30.0) -> bool:
        """在页面视口坐标 (x, y) 处派发一次真实的鼠标点击。

        为什么需要它
        -------------
        脚本里的 ``el.click()`` / ``elementFromPoint().click()`` 是合成事件，
        在部分 React 虚拟列表上会**被应用忽略**（表现为界面毫无反应）。
        CDP 的 ``Input.dispatchMouseEvent`` 走的是浏览器输入管线，
        与真人点击无法区分，是最可靠的点击方式。

        坐标是**视口相对**的 CSS 像素，与 ``getBoundingClientRect()`` 同一坐标系。

        Args:
            x: 视口内横坐标。
            y: 视口内纵坐标。
            timeout: 超时秒数。

        Returns:
            是否派发成功。
        """
        for etype in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", {
                "type": etype,
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
                "buttons": 1 if etype == "mousePressed" else 0,
            }, timeout=timeout)
        return True

    def mouse_move(self, x: float, y: float, timeout: float = 30.0) -> bool:
        """把鼠标移动到视口坐标 (x, y)。"""
        self.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": 0,
        }, timeout=timeout)
        return True

    def mouse_wheel(self, x: float, y: float, delta_y: float,
                    timeout: float = 30.0) -> bool:
        """在 (x, y) 处派发一次鼠标滚轮事件。

        为什么必须用滚轮而不是直接改 ``scrollTop``
        -----------------------------------------
        左侧会话列表是 rc-virtual-list 虚拟滚动，它靠监听**原生滚动事件**
        同步内部偏移。用 JS 直接赋值 ``scrollTop`` 偶尔能让容器动，
        但组件的内部状态会失联——表现为容器 ``scrollTop`` 一直在涨，
        渲染出来的却永远是同一批会话（实测滚到 7008/7616 时界面纹丝不动），
        后续所有「按昵称查找」全部失败。
        真实滚轮事件走浏览器输入管线，组件自己处理，不会失联。

        Args:
            x: 视口内横坐标（鼠标需位于列表区域上）。
            y: 视口内纵坐标。
            delta_y: 滚动量，正数向下、负数向上（单位像素）。
            timeout: 超时秒数。

        Returns:
            是否派发成功。
        """
        self.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y,
            "deltaX": 0, "deltaY": delta_y,
        }, timeout=timeout)
        return True

    def close(self) -> None:
        """关闭连接。"""
        self.ws.close()