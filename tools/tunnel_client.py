#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内网穿透 · 客户端（跑在你自己的电脑上，零第三方依赖）。

============================================================================
它做什么
============================================================================
主动连到公网服务器上的 tunnel_server.py（出站连接，所以不需要公网 IP、
不用在路由器上做端口映射），然后把服务器收到的每个 HTTP 请求转发到本机
`http://127.0.0.1:8082`（闲鱼控制台网页），再把响应原路送回去。

============================================================================
典型用法
============================================================================
    python tools/tunnel_client.py --server 你的服务器IP --port 8080 --token 你的令牌

连上之后，外网访问 http://你的服务器IP:8080/ 就等于访问家里的控制台。

============================================================================
为什么自己也手写 WebSocket
============================================================================
服务端要求零依赖（服务器上不一定能 pip install），客户端如果依赖第三方库，
两边行为容易不一致。所以共用同一套最小实现——客户端方向要注意：**客户端
发出的帧必须 mask**（RFC 6455 强制），否则服务端会直接断开。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import struct
import sys
import time
from urllib.parse import urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOCAL_TIMEOUT = 55

# 逐跳头不能转发（RFC 7230 §6.1）；Accept-Encoding 必须去掉并要求不压缩，
# 否则 gzip 过的 body 被原样回传、头却被服务端清掉，浏览器会收到乱码。
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "accept-encoding", "content-length",
}


def log(msg: str) -> None:
    print(f"[client] {msg}", flush=True)


# ===========================================================================
# WebSocket（客户端方向：发出的帧必须 mask）
# ===========================================================================
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已断开")
        buf += chunk
    return buf


def ws_read_frame(sock: socket.socket) -> tuple[int, bytes]:
    b1, b2 = _recv_exact(sock, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked and payload:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, payload


def ws_write_frame(sock: socket.socket, payload: bytes, opcode: int = 1) -> None:
    """客户端发帧：一律 mask=True。"""
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    key = os.urandom(4)
    header += key
    sock.sendall(header + bytes(payload[i] ^ key[i % 4] for i in range(len(payload))))


def ws_connect(host: str, port: int, token: str, use_tls: bool = False,
               timeout: float = 15.0) -> socket.socket:
    """手写 WebSocket 握手，返回已升级的 socket。

    Args:
        host: 服务器地址。
        port: 服务器端口。
        token: 与服务端 --token 一致。
        use_tls: True 时用 TLS（配 nginx 反代 + 证书时用 wss）。
        timeout: 连接超时。

    Returns:
        已握手的 socket。

    Raises:
        ConnectionError: 握手失败（含 token 不对）。
    """
    raw = socket.create_connection((host, port), timeout=timeout)
    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock = raw
    if use_tls:
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(raw, server_hostname=host)

    key = base64.b64encode(os.urandom(16)).decode()
    path = f"/?token={token}" if token else "/"
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())

    # 读响应头（到空行为止）
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("握手时连接被关闭")
        buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    status_line = head.split("\r\n", 1)[0]
    if "101" not in status_line:
        raise ConnectionError(f"握手失败：{status_line}")
    return sock


# ===========================================================================
# 把请求转发到本地网页
# ===========================================================================
def forward_local(method: str, path: str, headers: dict, body: bytes,
                  local_host: str, local_port: int) -> dict:
    """把请求打到本机网页，返回 {status, headers, body(base64)}。

    Args:
        method: HTTP 方法。
        path: 路径 + 查询串。
        headers: 外网请求头（会剔除逐跳头）。
        body: 请求体。
        local_host: 本机地址（默认 127.0.0.1）。
        local_port: 本机网页端口（默认 8082）。

    Returns:
        可直接回传给服务端的响应字典。
    """
    try:
        conn = http.client.HTTPConnection(local_host, local_port,
                                          timeout=LOCAL_TIMEOUT)
        hdrs = {k: v for k, v in headers.items() if k.lower() not in HOP_HEADERS}
        hdrs["Accept-Encoding"] = "identity"     # 要明文，别让本地 gzip
        conn.request(method, path, body=body or None, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        out_headers = {}
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk in ("transfer-encoding", "connection", "content-length",
                      "content-encoding", "keep-alive"):
                continue
            # 同名头（如多个 Set-Cookie）用 \n 分隔，服务端会原样回写多行
            out_headers[k] = (out_headers[k] + "\n" + v) if k in out_headers else v
        conn.close()
        return {
            "type": "response",
            "status": resp.status,
            "headers": out_headers,
            "body": base64.b64encode(data).decode(),
        }
    except Exception as exc:  # noqa: BLE001
        log(f"转发本地失败 {method} {path}: {type(exc).__name__}: {exc}")
        msg = f"本地网页无响应：{type(exc).__name__}: {exc}".encode()
        return {
            "type": "response",
            "status": 502,
            "headers": {"Content-Type": "text/plain; charset=utf-8"},
            "body": base64.b64encode(msg).decode(),
        }


# ===========================================================================
# 主循环
# ===========================================================================
def run_once(args) -> None:
    """建立一次隧道连接并服务，直到断开。"""
    sock = ws_connect(args.server, args.port, args.token, use_tls=args.tls)
    log(f"已连接 {args.server}:{args.port}，开始转发到 "
        f"{args.local_host}:{args.local_port}")
    try:
        while True:
            opcode, payload = ws_read_frame(sock)
            if opcode == 8:
                raise ConnectionError("服务端关闭了连接")
            if opcode == 9:                      # ping → pong
                ws_write_frame(sock, payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (1, 2):
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if msg.get("type") != "request":
                continue

            method = msg.get("method", "GET")
            path = msg.get("path", "/")
            headers = msg.get("headers") or {}
            try:
                body = base64.b64decode(msg.get("body") or "")
            except Exception:  # noqa: BLE001
                body = b""

            res = forward_local(method, path, headers, body,
                                args.local_host, args.local_port)
            res["id"] = msg.get("id", "")
            try:
                ws_write_frame(sock, json.dumps(res, ensure_ascii=False).encode())
            except Exception as exc:  # noqa: BLE001
                raise ConnectionError(f"回传失败：{exc}") from exc
            if args.verbose:
                log(f"{method} {path} -> {res.get('status')}")
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="内网穿透客户端（零依赖）")
    ap.add_argument("--server", required=True, help="服务端地址（IP 或域名）")
    ap.add_argument("--port", type=int, default=8080, help="服务端端口（默认 8080）")
    ap.add_argument("--token", required=True, help="与服务端一致的口令")
    ap.add_argument("--local-host", default="127.0.0.1", help="本机网页地址")
    ap.add_argument("--local-port", type=int, default=8082, help="本机网页端口")
    ap.add_argument("--tls", action="store_true", help="用 wss（配好证书时）")
    ap.add_argument("--verbose", action="store_true", help="打印每个请求")
    ap.add_argument("--once", action="store_true", help="断开后不重连（调试用）")
    args = ap.parse_args()

    delay = 3
    while True:
        try:
            run_once(args)
            delay = 3
        except KeyboardInterrupt:
            log("已停止")
            return 0
        except Exception as exc:  # noqa: BLE001
            log(f"连接异常：{type(exc).__name__}: {exc}")
            if args.once:
                return 1
        if args.once:
            return 0
        log(f"{delay} 秒后重连…")
        time.sleep(delay)
        delay = min(delay * 2, 30)     # 指数退避，别把服务器打爆


if __name__ == "__main__":
    sys.exit(main())
