"""网页端数据层：连接 MySQL 并提供所有读写接口。

所有网页端与 Python 引擎共享同一份 MySQL（xianyu_autoreply 库）。
这里集中封装：建表、设置、规则、卡密商品/库存、消息、发货记录、
运行日志、命令队列、引擎心跳。PHP 端通过同一份 init.sql 建表，
Python 端启动时也会按需补建，保证两端任意先启动都能用。

仅依赖 pymysql（已装入项目 venv）。
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Optional

import pymysql

# ============ 数据库连接配置 ============ #
# 优先读 data/db_config.json（网页「设置 → 数据库连接」写的就是它，可切本地/远程）。
# 文件不存在或损坏时用下面这套内置默认（= 本地 phpStudy 的 root 账号）。
_DB_DEFAULTS: dict[str, dict[str, Any]] = {
    # 兜底默认值：**故意不写密码**。真实连接信息由安装向导写入
    # data/db_config.json，这里只在文件缺失时用，写死作者密码等于把密码
    # 分发给所有用户（2026-10-23 清理）。
    "local": dict(host="127.0.0.1", port=3306, user="root",
                  password="", database="xianyu_autoreply"),
    "remote": dict(host="", port=3306, user="", password="", database=""),
}
_DB_CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "db_config.json"


def _read_db_config() -> dict[str, Any]:
    """读数据目录里的连接配置，返回 {mode, local, remote}。

    为什么同时保留 local / remote 两套、只用 mode 切换：远程库连不上时
    可以把 mode 改回 local 立刻恢复；只存一套的话，写错地址就把自己锁死了。

    Returns:
        归一化后的配置字典。
    """
    cfg: dict[str, Any] = {
        "mode": "local",
        "local": dict(_DB_DEFAULTS["local"]),
        "remote": dict(_DB_DEFAULTS["remote"]),
    }
    try:
        if not _DB_CONFIG_PATH.is_file():
            return cfg
        raw = json.loads(_DB_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return cfg
        for key in ("local", "remote"):
            part = raw.get(key)
            if isinstance(part, dict):
                # 字段名兼容两套写法：PHP 侧用 pass/db，Python 侧用 password/database，
                # 配置文件是两边共用的，必须都认（2026-10-22 实测踩到：
                # 只认 password/database 时远程库名读成空串，连库直接失败）
                for field, aliases in (
                    ("host", ("host",)),
                    ("user", ("user",)),
                    ("password", ("password", "pass")),
                    ("database", ("database", "db")),
                ):
                    for alias in aliases:
                        if part.get(alias):
                            cfg[key][field] = str(part[alias])
                            break
                if part.get("port"):
                    cfg[key]["port"] = int(part["port"])
        if raw.get("mode") == "remote":
            cfg["mode"] = "remote"
    except Exception:  # noqa: BLE001
        return cfg
    return cfg


def _build_db_cfg(ch: dict[str, Any]) -> dict[str, Any]:
    """把一份连接参数转成 pymysql.connect 的关键字参数。"""
    return dict(
        host=ch.get("host") or "127.0.0.1",
        port=int(ch.get("port") or 3306),
        user=ch.get("user") or "root",
        password=ch.get("password") or "",
        database=ch.get("database") or "xianyu_autoreply",
        # utf8mb4，绝不能写成 "utf8"（2026-10-19 用户报「卡密里的 emoji 变问号」）：
        # MySQL 的 "utf8" 是 3 字节的 utf8mb3，读不出 4 字节 emoji（📁 📦 等），
        # 会把它们转成 '?' 返回——卡密存进去是好的（库/表/列早就是 utf8mb4），
        # 坏在**读的那条连接**。PHP 端 db.php 已是 utf8mb4，这里必须对齐。
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5,
    )


_DBC = _read_db_config()
DB_CFG: dict[str, Any] = _build_db_cfg(_DBC[_DBC["mode"]])
# 远程连不上时回退用的一套（仅当 mode=remote 才启用）
_FALLBACK_CFG: Optional[dict[str, Any]] = (
    _build_db_cfg(_DBC["local"]) if _DBC["mode"] == "remote" else None)

_HERE = Path(__file__).resolve().parent
_INIT_SQL = _HERE / "init.sql"

_conn: Optional[pymysql.connections.Connection] = None

# 远程库刚失败的标记文件（30 秒内不再重试远程，避免每次查询都等超时）
_REMOTE_DOWN_FLAG = _DB_CONFIG_PATH.parent / ".db_remote_down"


def _remote_down_recent() -> bool:
    """30 秒内远程库失败过吗（失败就走本地，别反复等超时）。"""
    try:
        if not _REMOTE_DOWN_FLAG.is_file():
            return False
        return time.time() - float(_REMOTE_DOWN_FLAG.read_text().strip() or 0) < 30
    except Exception:  # noqa: BLE001
        return False


def _mark_remote_down(down: bool) -> None:
    try:
        if down:
            _REMOTE_DOWN_FLAG.write_text(str(int(time.time())), encoding="utf-8")
        elif _REMOTE_DOWN_FLAG.exists():
            _REMOTE_DOWN_FLAG.unlink()
    except Exception:  # noqa: BLE001
        pass


def _tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """快速探测端口通不通（不通就别让 pymysql 白等 5 秒 × 2 次）。"""
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def conn() -> pymysql.connections.Connection:
    """获取（必要时重建）一个 MySQL 连接，带自动重连与远程故障回退。

    配置成远程库但连不上时**自动回退本地**：否则网络一断，引擎就整个停摆，
    网页也进不去改配置，只能手动改文件。失败后 30 秒内不再重试远程，
    否则每条查询都要等满 connect_timeout，引擎会被拖到没法用。
    """
    global _conn
    cfg = DB_CFG
    if _FALLBACK_CFG is not None:
        # 远程刚失败过，或端口根本不通 → 直接用本地，不浪费超时
        if _remote_down_recent() or not _tcp_reachable(cfg["host"], cfg["port"]):
            cfg = _FALLBACK_CFG
            _mark_remote_down(True)
    try:
        if _conn is None or not _conn.open:
            _conn = pymysql.connect(**cfg)
        _conn.ping(reconnect=True)
        return _conn
    except Exception:
        try:
            if _conn:
                _conn.close()
        except Exception:
            pass
        try:
            _conn = pymysql.connect(**cfg)
            if cfg is DB_CFG and _FALLBACK_CFG is not None:
                _mark_remote_down(False)
            return _conn
        except Exception:
            # 远程不通 → 退本地（并记下失败，30 秒内不再试远程）
            if _FALLBACK_CFG is not None and cfg is not _FALLBACK_CFG:
                _mark_remote_down(True)
                _conn = pymysql.connect(**_FALLBACK_CFG)
                return _conn
            raise


def _jload(s: Any) -> Any:
    if s is None or s == "":
        return [] if s is None else {}
    if isinstance(s, (list, dict)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return s


def _jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


# ============ 建表 ============ #
def ensure_schema() -> bool:
    """按 init.sql 建表（已存在则跳过）。返回是否成功。"""
    if not _INIT_SQL.exists():
        return False
    sql = _INIT_SQL.read_text(encoding="utf-8")
    # init.sql 不含存储过程，按 ';' 切分即可安全执行。
    stmts = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]
    c = conn().cursor()
    try:
        for st in stmts:
            c.execute(st)
        _migrate_settings_columns(c)
        return True
    except Exception as exc:  # noqa: BLE001
        # 主键/表已存在等属于可忽略错误
        err = str(exc)
        if "already exists" in err or "Duplicate" in err or "1050" in err or "1062" in err:
            try:
                _migrate_settings_columns(c)
            except Exception:  # noqa: BLE001
                pass
            return True
        raise
    finally:
        c.close()


def _migrate_settings_columns(c) -> None:
    """老库幂等补列：init.sql 新增的 settings 列在此补齐。"""
    c.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'settings'"
    )
    have = {row[0] for row in c.fetchall()}
    # (列名, 列定义) —— 新增可调频率参数也在此追加
    wanted = [
        ("rate_limit_enabled", "TINYINT(1) NOT NULL DEFAULT 1"),
        ("rate_limit_per_user_hour", "INT NOT NULL DEFAULT 3"),
        ("rate_limit_window_sec", "INT NOT NULL DEFAULT 120"),
        # 定时扫描（静默买家捞单）：开关 / 范围（0=全部，N=前 N 个会话）/ 间隔秒
        ("scan_enabled", "TINYINT(1) NOT NULL DEFAULT 0"),
        ("scan_scope", "INT NOT NULL DEFAULT 10"),
        ("scan_interval", "INT NOT NULL DEFAULT 180"),
        # 登录失败限流开关：连续输错 5 次锁 5 分钟。默认开启，
        # 用户在「设置」里可关掉（2026-10-25 用户要求做成可开关）。
        ("login_guard_enabled", "TINYINT(1) NOT NULL DEFAULT 1"),
    ]
    for col, ddl in wanted:
        # 注意：不能写成 `if have and col not in have` —— 一旦 information_schema
        # 读不到（权限/视图差异），have 为空就整批跳过，新列永远补不上
        # （2026-10-25 实测：login_guard_enabled 因此没建出来）。
        if col in have:
            continue
        try:
            c.execute(f"ALTER TABLE settings ADD COLUMN {col} {ddl}")
        except Exception:  # noqa: BLE001
            pass  # 列已存在 / 并发加列，忽略


# ============ 全局设置 ============ #
def load_settings() -> dict:
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute("SELECT * FROM settings WHERE id=1")
        row = c.fetchone()
        if not row:
            ensure_schema()
            c.execute("SELECT * FROM settings WHERE id=1")
            row = c.fetchone()
        if not row:
            # settings 表被清空（例如在 phpMyAdmin 里误执行 TRUNCATE）时，
            # 旧代码下面直接 row.get() 会抛 'NoneType' object has no attribute 'get'，
            # 把整个引擎打崩（2026-10-20 12:43 实测）。这里补回默认行：
            # 表上每列都有默认值，插一条空记录即可恢复出厂配置。
            c.execute("INSERT INTO settings (id) VALUES (1)")
            conn().commit()
            c.execute("SELECT * FROM settings WHERE id=1")
            row = c.fetchone()
        if not row:
            # 连插入都失败：给内存默认值，宁可参数保守也不能让引擎崩
            row = {"id": 1, "poll_interval": 3, "max_replies_per_hour": 60,
                   "dry_run": 0, "rate_limit_enabled": 1, "selectors": None}
        row["selectors"] = _jload(row.get("selectors"))
        return row
    finally:
        c.close()


def save_settings(**fields) -> None:
    allowed = {
        "cdp_port", "poll_interval", "max_replies_per_hour",
        "dry_run", "selectors", "engine_running",
        "rate_limit_enabled", "rate_limit_per_user_hour", "rate_limit_window_sec",
        "scan_enabled", "scan_scope", "scan_interval",
        "batch_interval_min", "batch_daily_max",
        "login_guard_enabled",
    }
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "selectors":
            v = _jdump(v)
        sets.append(f"{k}=%s")
        args.append(v)
    if not sets:
        return
    c = conn().cursor()
    try:
        c.execute(f"UPDATE settings SET {','.join(sets)} WHERE id=1", args)
    finally:
        c.close()


# ============ 自动回复规则 ============ #
def list_rules() -> list[dict]:
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute("SELECT * FROM rules ORDER BY priority DESC, id ASC")
        rows = c.fetchall()
        for r in rows:
            r["keywords"] = _jload(r["keywords"])
        return rows
    finally:
        c.close()


def add_rule(keywords: list[str], reply: str, priority: int = 0, enabled: int = 1) -> int:
    c = conn().cursor()
    try:
        c.execute(
            "INSERT INTO rules (keywords, reply, priority, enabled) VALUES (%s,%s,%s,%s)",
            (_jdump(keywords), reply, priority, enabled),
        )
        return c.lastrowid
    finally:
        c.close()


def update_rule(rid: int, **fields) -> None:
    allowed = {"keywords", "reply", "priority", "enabled"}
    sets, args = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "keywords":
            v = _jdump(v)
        sets.append(f"{k}=%s")
        args.append(v)
    if not sets:
        return
    args.append(rid)
    c = conn().cursor()
    try:
        c.execute(f"UPDATE rules SET {','.join(sets)} WHERE id=%s", args)
    finally:
        c.close()


def delete_rule(rid: int) -> None:
    c = conn().cursor()
    try:
        c.execute("DELETE FROM rules WHERE id=%s", (rid,))
    finally:
        c.close()


def inc_rule_hit(rid: int) -> None:
    c = conn().cursor()
    try:
        c.execute("UPDATE rules SET hit_count=hit_count+1 WHERE id=%s", (rid,))
    finally:
        c.close()


def match_rule(text: str) -> Optional[dict]:
    """按优先级匹配规则；命中返回规则 dict，否则 None。"""
    t = (text or "").lower()
    for r in list_rules():
        if not r["enabled"]:
            continue
        for kw in r["keywords"]:
            if (kw or "").lower() in t:
                return r
    return None


# ============ 卡密商品 / 库存 ============ #
def list_products() -> list[dict]:
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute("SELECT * FROM card_products ORDER BY id ASC")
        rows = c.fetchall()
        for r in rows:
            r["keywords"] = _jload(r["keywords"])
            # c2 必须用 DictCursor：普通光标的 fetchone() 返回元组，
            # 下面按 stat["total"] 字典下标取值会抛
            # 「tuple indices must be integers or slices, not str」
            # （2026-10-17 用户实测：引擎每轮轮询到此必崩，自动发货全灭）。
            c2 = conn().cursor(pymysql.cursors.DictCursor)
            c2.execute(
                "SELECT COUNT(*) AS total, SUM(used) AS used FROM card_inventory WHERE product_id=%s",
                (r["id"],),
            )
            stat = c2.fetchone()
            c2.close()
            r["total"] = stat["total"] or 0
            r["used"] = stat["used"] or 0
            r["remain"] = r["total"] - r["used"]
        return rows
    finally:
        c.close()


def add_product(name: str, keywords: list[str]) -> int:
    c = conn().cursor()
    try:
        c.execute(
            "INSERT INTO card_products (product_name, keywords) VALUES (%s,%s)",
            (name, _jdump(keywords)),
        )
        return c.lastrowid
    finally:
        c.close()


def delete_product(pid: int) -> None:
    c = conn().cursor()
    try:
        c.execute("DELETE FROM card_inventory WHERE product_id=%s", (pid,))
        c.execute("DELETE FROM card_products WHERE id=%s", (pid,))
    finally:
        c.close()


def add_codes(pid: int, codes: list[str]) -> int:
    codes = [c.strip() for c in codes if c and c.strip()]
    if not codes:
        return 0
    c = conn().cursor()
    try:
        c.executemany(
            "INSERT INTO card_inventory (product_id, code) VALUES (%s,%s)",
            [(pid, code) for code in codes],
        )
        return len(codes)
    finally:
        c.close()


def consume_code(pid: int, buyer: str) -> Optional[str]:
    """从某商品取一条未用卡密并立即核销，返回卡密；无库存返回 None。"""
    c = conn().cursor()
    try:
        c.execute(
            "SELECT id, code FROM card_inventory WHERE product_id=%s AND used=0 "
            "ORDER BY id ASC LIMIT 1 FOR UPDATE",
            (pid,),
        )
        row = c.fetchone()
        if not row:
            return None
        cid, code = row
        c.execute(
            "UPDATE card_inventory SET used=1, used_at=NOW(), used_by=%s WHERE id=%s",
            (buyer, cid),
        )
        return code
    finally:
        c.close()


def match_product(order_text: str) -> Optional[dict]:
    """用订单文案匹配卡密商品（关键词任一命中）。"""
    t = (order_text or "").lower()
    for p in list_products():
        for kw in p["keywords"]:
            if (kw or "").lower() in t:
                return p
    return None


# ============ 新卡卷系统（shop_items + card_bundles + item_bundles） ============ #
# 网页端「商品列表 / 卡卷管理」维护的就是这套表；一条卡卷 = 名称 + 内容全文
# （夸克网盘链接等），商品与卡卷多对多绑定。引擎自动发货优先走这套，
# 老卡密系统（card_products/card_inventory）保留为兜底。

def normalize_title(s: str) -> str:
    """标题归一化：去掉所有空白字符并转小写，用于容错匹配。"""
    return re.sub(r"\s+", "", s or "").lower()


def list_item_bundles() -> list[dict]:
    """所有 [商品 -> 已启用卡卷] 绑定对。

    返回字段：item_id（shop_items 自增主键）、xianyu_item_id（闲鱼商品ID）、
    item_title、bundle_id、bundle_name、content（发货全文）。
    卡卷被停用（enabled=0）或商品/卡卷被删的绑定自然消失（JOIN 过滤）。
    """
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute(
            """SELECT s.id AS item_id, s.item_id AS xianyu_item_id,
                      s.title AS item_title, b.id AS bundle_id,
                      b.name AS bundle_name, b.content
               FROM item_bundles ib
               JOIN shop_items s ON s.id = ib.item_id
               JOIN card_bundles b ON b.id = ib.bundle_id
               WHERE b.enabled = 1
               ORDER BY s.id ASC"""
        )
        return c.fetchall()
    finally:
        c.close()


def find_bundle_for_title(title: str) -> Optional[dict]:
    """按商品标题找已绑定的启用卡卷，未绑定返回 None。

    匹配策略（从严到宽，宁严勿错——发错货比不发货更糟）：
    1. 归一化后完全相等；
    2. 归一化后互相包含（较短一方长度 >= 4，防 "UI" 之类误命中；
       多个候选时取标题最长的，即最具体的）。
    没有任何绑定命中时返回 None，由调用方提示「未绑定卡卷」。
    """
    t = normalize_title(title)
    if not t:
        return None
    binds = list_item_bundles()
    if not binds:
        return None
    for b in binds:
        if normalize_title(b["item_title"]) == t:
            return b
    best: Optional[dict] = None
    best_len = 0
    for b in binds:
        it = normalize_title(b["item_title"])
        if len(it) < 4:
            continue
        if it in t or t in it:
            if len(it) > best_len:
                best, best_len = b, len(it)
    return best


# ============ 消息 / 会话 / 发货 ============ #
def upsert_conversation(buyer: str, preview: str = "", unread: int = 0,
                        last_msg: str = "", last_active=None) -> None:
    c = conn().cursor()
    try:
        c.execute(
            "INSERT INTO conversations (buyer_name, preview, unread, last_msg, last_active) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE preview=VALUES(preview), unread=VALUES(unread), "
            "last_msg=VALUES(last_msg), last_active=VALUES(last_active), updated_at=NOW()",
            (buyer, preview, unread, last_msg, last_active),
        )
    finally:
        c.close()


def record_message(buyer: str, direction: str, content: str,
                   is_order: int = 0, conversation_id: int = None) -> None:
    c = conn().cursor()
    try:
        c.execute(
            "INSERT INTO messages (conversation_id, buyer_name, direction, content, is_order) "
            "VALUES (%s,%s,%s,%s,%s)",
            (conversation_id, buyer, direction, content, is_order),
        )
    finally:
        c.close()


def record_delivery(buyer: str, code: str, product_id=None, product_name=None,
                    order_id=None, status: str = "sent", note=None,
                    platform_status=None) -> None:
    if platform_status is None:
        c = conn().cursor()
        try:
            c.execute(
                "INSERT INTO deliveries (order_id, buyer_name, product_id, product_name, code, status, note) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (order_id, buyer, product_id, product_name, code, status, note),
            )
        finally:
            c.close()
    else:
        c = conn().cursor()
        try:
            c.execute(
                "INSERT INTO deliveries (order_id, buyer_name, product_id, product_name, code, status, "
                "platform_status, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (order_id, buyer, product_id, product_name, code, status,
                 platform_status, note),
            )
        finally:
            c.close()


def recent_delivery_for(buyer: str, within_sec: int = 600) -> Optional[dict]:
    """查买家最近是否已发过卡密，避免重复发货。"""
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute(
            "SELECT * FROM deliveries WHERE buyer_name=%s AND "
            "created_at >= NOW() - INTERVAL %s SECOND ORDER BY id DESC LIMIT 1",
            (buyer, within_sec),
        )
        return c.fetchone()
    finally:
        c.close()


def list_undelivered_confirmed(limit: int = 10) -> list[dict]:
    """列出「平台已确认发货、但卡密从未送达」的订单（待补发）。

    背景（2026-10-18 20:21 实测踩坑）：平台点「确认发货」成功后订单立刻
    移出「待发货」列表，`ship_pending_from_manage` 再也扫不到它。若那轮
    卡密发送失败（会话没定位到/发送报错），就**永远没人重试** —— 钱已
    过户、买家拿不到卡密。这类订单必须靠本函数兜底补发。

    只取 order_id 非空（无订单号无法定位会话）且 status != 'sent' 的记录。

    Args:
        limit: 最多返回多少笔（按平台确认时间从新到旧）。

    Returns:
        deliveries 记录列表。
    """
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute(
            "SELECT * FROM deliveries "
            "WHERE platform_status='confirmed' "
            "AND status <> 'sent' AND order_id IS NOT NULL AND order_id <> '' "
            "ORDER BY id DESC LIMIT %s",
            (int(limit),),
        )
        return list(c.fetchall() or [])
    finally:
        c.close()


def delivery_for_order(order_id: str) -> Optional[dict]:
    """查某订单号是否已有**成功**发货记录（按闲鱼订单编号精确去重）。

    聊天里的订单卡会一直留在会话里，仅靠买家名+时间窗去重，买家过几天
    再聊同一笔订单会被重复发货；订单号级去重彻底堵住这个口子。
    """
    if not order_id:
        return None
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute(
            "SELECT * FROM deliveries WHERE order_id=%s AND status='sent' "
            "ORDER BY id DESC LIMIT 1",
            (str(order_id),),
        )
        return c.fetchone()
    finally:
        c.close()


def latest_delivery_for_order(order_id: str) -> Optional[dict]:
    """查某订单号最新一条发货记录（不限 status，用于判断平台确认进度）。"""
    if not order_id:
        return None
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute(
            "SELECT * FROM deliveries WHERE order_id=%s ORDER BY id DESC LIMIT 1",
            (str(order_id),),
        )
        return c.fetchone()
    finally:
        c.close()


def record_platform_result(order_id: str, buyer: str, product_name: str,
                           ok: bool, note: str = None) -> Optional[int]:
    """平台「去发货」确认结果落库，返回新记录 id。

    ok=True: status='pending'（平台已确认、卡密待发）+ platform_status='confirmed'
    ok=False: status='failed' + platform_status='failed'（钱不会到账，禁止发卡密）
    """
    c = conn().cursor()
    try:
        if ok:
            c.execute(
                "INSERT INTO deliveries (order_id, buyer_name, product_name, code, status, "
                "platform_status, platform_confirmed_at, note) "
                "VALUES (%s,%s,%s,'','pending','confirmed',NOW(),%s)",
                (str(order_id), buyer, product_name, note or "平台已确认，待发卡密"),
            )
        else:
            c.execute(
                "INSERT INTO deliveries (order_id, buyer_name, product_name, code, status, "
                "platform_status, note) VALUES (%s,%s,%s,'','failed','failed',%s)",
                (str(order_id), buyer, product_name, note or "平台发货确认失败"),
            )
        return c.lastrowid
    finally:
        c.close()


def mark_platform_confirmed(delivery_id: int) -> None:
    """把既有记录标记为平台已确认（上轮失败、本轮重试成功的场景）。

    只更新平台字段，不动 status——status='sent' 的历史记录（卡密早已
    发过、只补平台确认）不能被误打回 pending。
    """
    c = conn().cursor()
    try:
        c.execute(
            "UPDATE deliveries SET platform_status='confirmed', "
            "platform_confirmed_at=NOW() WHERE id=%s",
            (delivery_id,),
        )
    finally:
        c.close()


def update_delivery(delivery_id: int, **fields) -> None:
    """按 id 更新发货记录字段（白名单校验，防字段名注入）。"""
    allowed = {"status", "code", "product_id", "product_name", "note",
               "platform_status"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(k + "=%s")
            vals.append(v)
    if not sets:
        return
    vals.append(delivery_id)
    c = conn().cursor()
    try:
        c.execute("UPDATE deliveries SET " + ", ".join(sets) + " WHERE id=%s", vals)
    finally:
        c.close()


# ============ 运行日志 ============ #
def log_activity(level: str, message: str) -> None:
    try:
        c = conn().cursor()
        c.execute(
            "INSERT INTO activity_log (level, message) VALUES (%s,%s)",
            (level, message),
        )
        c.close()
    except Exception:  # noqa: BLE001
        pass  # 日志写入失败不应中断引擎


def recent_activity(limit: int = 100) -> list[dict]:
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT %s", (limit,))
        return c.fetchall()
    finally:
        c.close()


# ============ 命令队列（网页 -> 引擎） ============ #
def push_command(cmd: str, payload: dict = None) -> int:
    c = conn().cursor()
    try:
        c.execute(
            "INSERT INTO commands (cmd, payload) VALUES (%s,%s)",
            (cmd, _jdump(payload or {})),
        )
        return c.lastrowid
    finally:
        c.close()


def pop_commands() -> list[dict]:
    """取出待处理命令并标记为已处理。"""
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute("SELECT * FROM commands WHERE status='pending' ORDER BY id ASC")
        rows = c.fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            c.execute(
                "UPDATE commands SET status='done', processed_at=NOW() WHERE id IN %s",
                (ids,),
            )
            for r in rows:
                r["payload"] = _jload(r["payload"])
        return rows
    finally:
        c.close()


# ============ 引擎心跳 ============ #
def set_engine_status(state: str = "running", pid: int = None,
                      heartbeat: bool = False, last_activity=None) -> None:
    c = conn().cursor()
    try:
        if state in ("stopped", "error"):
            c.execute(
                "INSERT INTO engine_status (id, state) VALUES (1,'stopped') "
                "ON DUPLICATE KEY UPDATE state='stopped', pid=NULL"
            )
            return
        # running：保证行存在并刷新状态
        sets = ["state='running'"]
        args: list[Any] = []
        if pid is not None:
            sets.append("pid=%s")
            args.append(pid)
        if heartbeat:
            sets.append("last_heartbeat=NOW()")
        if last_activity is not None:
            sets.append("last_activity=%s")
            args.append(last_activity)
        sets.append("started_at=IFNULL(started_at, NOW())")
        c.execute(
            f"INSERT INTO engine_status (id, state, pid) VALUES (1,'running',%s) "
            f"ON DUPLICATE KEY UPDATE {','.join(sets)}",
            [pid] + args,
        )
    finally:
        c.close()


def get_engine_status() -> dict:
    c = conn().cursor(pymysql.cursors.DictCursor)
    try:
        c.execute("SELECT * FROM engine_status WHERE id=1")
        row = c.fetchone()
        if not row:
            row = {"state": "stopped", "pid": None,
                   "last_heartbeat": None, "last_activity": None}
        return row
    finally:
        c.close()


def engine_alive() -> bool:
    """根据心跳判断引擎是否在跑（15 秒内算活着）。"""
    st = get_engine_status()
    if st.get("state") != "running":
        return False
    lb = st.get("last_heartbeat")
    if not lb:
        return False
    try:
        if isinstance(lb, str):
            lb = time.mktime(time.strptime(lb, "%Y-%m-%d %H:%M:%S"))
        return (time.time() - float(lb)) < 15
    except Exception:
        return False
