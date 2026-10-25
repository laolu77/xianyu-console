#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内网穿透 · 服务端（部署在你的公网服务器上，零第三方依赖）。

============================================================================
它做什么
============================================================================
    外网浏览器  --HTTP-->  [本程序:8080]  --WebSocket-->  [你家里的客户端]
                                                                  |
                                                          http://127.0.0.1:8082
                                                          （闲鱼控制台网页）

本程序跑在有公网 IP 的服务器上；你家里的电脑跑 tunnel_client.py 主动连出来
（NAT 出站，不需要公网 IP、不需要路由器端口映射）。

============================================================================
为什么坚持零依赖
============================================================================
服务器上不一定有 pip / 不一定是你能控制的镜像环境，`pip install websockets`
可能直接失败。所以 WebSocket 握手和帧编解码全部手写（标准库 socket + hashlib）。
**单文件、拷上去就能 python tunnel_server.py 跑**。

============================================================================
安全（重要，必读）
============================================================================
网页控制台能操作你的店铺（发货、改价、删除商品），暴露到公网**必须加锁**：

1. `--token`：客户端连接凭证。不知道 token 的人连不上隧道。
2. `--user` / `--pass`：外网访问时的 HTTP Basic 认证，浏览器会先弹登录框。

两个都要设，且不要再用默认值。用 HTTPS（nginx 反代 + 证书）会更安全，
本程序自己不做 TLS——后面有 nginx 配置示例。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import uuid
from typing import Optional

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEAD_BYTES = 64 * 1024
REQUEST_TIMEOUT = 60          # 单个 HTTP 请求等客户端返回的最长时间（秒）
HEARTBEAT_INTERVAL = 25       # 向客户端发 ping 的间隔


# ===========================================================================
# WebSocket 编解码（RFC 6455 最小实现：text / binary / ping / pong / close）
# ===========================================================================
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """读满 n 字节；连接断开时抛 ConnectionError。"""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已断开")
        buf += chunk
    return buf


def ws_accept_key(key: str) -> str:
    """按 RFC 计算 Sec-WebSocket-Accept。"""
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_read_frame(sock: socket.socket) -> tuple[int, bytes]:
    """读一帧，返回 (opcode, payload)。

    Args:
        sock: 已握手的 socket。

    Returns:
        (opcode, payload)；opcode 8=close, 9=ping, 10=pong, 1=text, 2=binary。
    """
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


def ws_write_frame(sock: socket.socket, payload: bytes, opcode: int = 1,
                   mask: bool = False) -> None:
    """写一帧。服务端发出的帧不 mask，客户端发出的必须 mask。"""
    header = bytes([0x80 | opcode])
    n = len(payload)
    flag = 0x80 if mask else 0
    if n < 126:
        header += bytes([flag | n])
    elif n < 65536:
        header += bytes([flag | 126]) + struct.pack(">H", n)
    else:
        header += bytes([flag | 127]) + struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        header += key
        payload = bytes(payload[i] ^ key[i % 4] for i in range(len(payload)))
    sock.sendall(header + payload)


# ===========================================================================
# 隧道状态
# ===========================================================================
class Tunnel:
    """管理唯一一条客户端隧道连接 + 请求/响应配对。"""

    def __init__(self) -> None:
        self.sock: Optional[socket.socket] = None
        self.send_lock = threading.Lock()      # sendall 不是线程安全的
        self.pending: dict[str, dict] = {}     # request_id -> {event, data}
        self.lock = threading.Lock()
        self.connected_at: Optional[float] = None
        self.remote = ""
        self.stats = {"requests": 0, "errors": 0}

    # ---- 连接管理 ----
    def attach(self, sock: socket.socket, remote: str) -> None:
        """挂上新隧道；旧连接存在时踢掉（只允许一个客户端）。"""
        with self.lock:
            old = self.sock
            self.sock = sock
            self.connected_at = time.time()
            self.remote = remote
        if old:
            try:
                old.close()
            except OSError:
                pass
        print(f"[tunnel] 客户端已连接 {remote}", flush=True)

    def detach(self, sock: socket.socket) -> None:
        with self.lock:
            if self.sock is sock:
                self.sock = None
                self.connected_at = None
        # 唤醒所有等待中的请求，避免它们卡到超时
        with self.lock:
            for item in self.pending.values():
                item["data"] = {"error": "tunnel_closed"}
                item["event"].set()

    def is_alive(self) -> bool:
        return self.sock is not None

    # ---- 请求转发 ----
    def forward(self, method: str, path: str, headers: dict,
                body: bytes) -> dict:
        """把一个 HTTP 请求通过隧道发出去，阻塞等响应。

        Args:
            method: HTTP 方法。
            path: 路径 + 查询串。
            headers: 请求头（原样透传，客户端会剔除 hop-by-hop 头）。
            body: 请求体。

        Returns:
            {"status": int, "headers": dict, "body": bytes} 或 {"error": str}。
        """
        if not self.is_alive():
            return {"error": "no_client"}

        rid = uuid.uuid4().hex
        event = threading.Event()
        with self.lock:
            self.pending[rid] = {"event": event, "data": None}
        msg = json.dumps({
            "type": "request",
            "id": rid,
            "method": method,
            "path": path,
            "headers": headers,
            "body": base64.b64encode(body).decode(),
        }, ensure_ascii=False).encode()

        try:
            with self.send_lock:
                ws_write_frame(self.sock, msg)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.pending.pop(rid, None)
            print(f"[tunnel] 发送失败: {exc}", flush=True)
            return {"error": "send_failed"}

        event.wait(REQUEST_TIMEOUT)
        with self.lock:
            item = self.pending.pop(rid, None)
        if not item or item["data"] is None:
            self.stats["errors"] += 1
            return {"error": "timeout"}

        data = item["data"]
        if isinstance(data, dict) and data.get("error"):
            self.stats["errors"] += 1
            return data
        self.stats["requests"] += 1
        try:
            return {
                "status": int(data.get("status", 200)),
                "headers": data.get("headers") or {},
                "body": base64.b64decode(data.get("body") or ""),
            }
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            return {"error": "bad_response"}

    def resolve(self, rid: str, data: dict) -> None:
        """客户端回来了：唤醒对应请求。"""
        with self.lock:
            item = self.pending.get(rid)
        if item:
            item["data"] = data
            item["event"].set()


TUNNEL = Tunnel()


# ===========================================================================
# HTTP 处理
# ===========================================================================
def read_http_head(sock: socket.socket) -> tuple[str, dict, bytes]:
    """读 HTTP 请求头，返回 (请求行, headers 小写字典, 已读到的 body 前缀)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("连接已断开")
        buf += chunk
        if len(buf) > MAX_HEAD_BYTES:
            raise ValueError("请求头过大")
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    first = lines[0]
    headers: dict[str, str] = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return first, headers, rest


def send_http(sock: socket.socket, status: int, headers: dict, body: bytes) -> None:
    """回写一个 HTTP 响应。"""
    reason = {200: "OK", 401: "Unauthorized", 403: "Forbidden",
              404: "Not Found", 502: "Bad Gateway", 503: "Service Unavailable",
              504: "Gateway Timeout"}.get(status, "OK")
    out = [f"HTTP/1.1 {status} {reason}"]
    hdrs = dict(headers)
    hdrs.pop("Transfer-Encoding", None)     # 统一带 Content-Length
    hdrs.pop("Content-Encoding", None)      # 客户端已解压过，别再声明白压缩
    hdrs["Content-Length"] = str(len(body))
    hdrs.setdefault("Connection", "close")
    for k, v in hdrs.items():
        out.append(f"{k}: {v}")
    raw = ("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + body
    try:
        sock.sendall(raw)
    except OSError:
        pass


def check_basic_auth(headers: dict, user: str, password: str) -> bool:
    """校验 HTTP Basic 认证。"""
    if not user:
        return True
    got = headers.get("authorization", "")
    if not got.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(got.split(" ", 1)[1]).decode("utf-8", "replace")
        u, _, p = raw.partition(":")
        return u == user and p == password
    except Exception:  # noqa: BLE001
        return False


def handle_client(sock: socket.socket, addr: tuple, args) -> None:
    """处理一个来自公网的连接：可能是 WS 隧道，也可能是普通 HTTP 请求。"""
    try:
        first, headers, rest = read_http_head(sock)
        parts = first.split(" ")
        if len(parts) < 2:
            sock.close()
            return
        method, target = parts[0], parts[1]

        # ---- 客户端来连隧道（WebSocket Upgrade） ----
        if headers.get("upgrade", "").lower() == "websocket":
            # token 校验：从 ?token= 里取，也接受 X-Tunnel-Token 头
            token = ""
            if "?" in target:
                for kv in target.split("?", 1)[1].split("&"):
                    if kv.startswith("token="):
                        token = kv.split("=", 1)[1]
            if not token:
                token = headers.get("x-tunnel-token", "")
            if args.token and token != args.token:
                print(f"[tunnel] 拒绝：token 不对 来自 {addr[0]}", flush=True)
                send_http(sock, 403, {"Content-Type": "text/plain; charset=utf-8"},
                          "invalid token".encode())
                sock.close()
                return
            key = headers.get("sec-websocket-key", "")
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {ws_accept_key(key)}\r\n\r\n"
            )
            sock.sendall(resp.encode())
            TUNNEL.attach(sock, f"{addr[0]}:{addr[1]}")
            _tunnel_loop(sock)
            return

        # ---- 普通 HTTP 请求：先过 Basic 认证 ----
        if not check_basic_auth(headers, args.user, args.password):
            send_http(sock, 401,
                      {"WWW-Authenticate": 'Basic realm="xianyu-console"',
                       "Content-Type": "text/html; charset=utf-8"},
                      "<h3>需要登录</h3><p>请输入访问账号密码。</p>".encode())
            sock.close()
            return

        if not TUNNEL.is_alive():
            send_http(sock, 503, {"Content-Type": "text/html; charset=utf-8"},
                      "<h3>隧道未连接</h3><p>家里的客户端没连上，"
                      "请确认 tunnel_client.py 正在运行。</p>".encode())
            sock.close()
            return

        # 读完整 body（按 Content-Length）
        body = rest
        try:
            need = int(headers.get("content-length", "0") or 0)
        except ValueError:
            need = 0
        while len(body) < need:
            chunk = sock.recv(min(65536, need - len(body)))
            if not chunk:
                break
            body += chunk

        res = TUNNEL.forward(method, target, headers, body)
        if res.get("error") == "timeout":
            send_http(sock, 504, {"Content-Type": "text/html; charset=utf-8"},
                      "<h3>请求超时</h3><p>家里电脑没在 60 秒内响应。</p>".encode())
        elif res.get("error"):
            send_http(sock, 502, {"Content-Type": "text/html; charset=utf-8"},
                      f"<h3>隧道错误</h3><p>{res['error']}</p>".encode())
        else:
            send_http(sock, res["status"], res["headers"], res["body"])

    except (ConnectionError, ValueError):
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[tunnel] 处理连接异常 {addr}: {type(exc).__name__}: {exc}", flush=True)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _tunnel_loop(sock: socket.socket) -> None:
    """隧道连接的消息循环：收 response/pong，处理 close。"""
    sock.settimeout(None)
    try:
        while True:
            opcode, payload = ws_read_frame(sock)
            if opcode == 8:            # close
                break
            if opcode == 9:            # ping → pong
                ws_write_frame(sock, payload, opcode=10)
                continue
            if opcode == 10:           # pong，忽略
                continue
            if opcode in (1, 2):
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if msg.get("type") == "response":
                    TUNNEL.resolve(msg.get("id", ""), msg)
    except (ConnectionError, OSError):
        pass
    finally:
        TUNNEL.detach(sock)
        print("[tunnel] 客户端已断开", flush=True)
        try:
            sock.close()
        except OSError:
            pass


def heartbeat_loop() -> None:
    """定期 ping，及早发现断线（NAT 会悄悄掐掉空闲连接）。"""
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        sock = TUNNEL.sock
        if not sock:
            continue
        try:
            with TUNNEL.send_lock:
                ws_write_frame(sock, b"", opcode=9)
        except Exception:  # noqa: BLE001
            try:
                sock.close()
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="内网穿透服务端（零依赖）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认所有网卡）")
    ap.add_argument("--port", type=int, default=9000, help="监听端口（默认 8080）")
    ap.add_argument("--token", default="", help="客户端连接令牌（必填，自己设一串随机字符）")
    ap.add_argument("--user", default="", help="外网访问 HTTP Basic 用户名（建议设置）")
    ap.add_argument("--password", default="", help="外网访问 HTTP Basic 密码（建议设置）")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if not args.token:
        print("[!] 警告：没有设置 --token，任何人都能接入隧道。")
        print("    正式使用请加：--token 一串随机字符")
    if not args.user:
        print("[!] 警告：没有设置 --user/--password，外网任何人可直接打开控制台。")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(64)
    print(f"[tunnel] 服务端已启动 {args.host}:{args.port}", flush=True)
    if args.user:
        print(f"[tunnel] 外网访问需登录：用户 {args.user}", flush=True)

    threading.Thread(target=heartbeat_loop, daemon=True).start()

    try:
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=handle_client, args=(conn, addr, args),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\n[tunnel] 收到停止信号，退出")
    finally:
        srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
