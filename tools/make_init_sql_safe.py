"""把 init.sql 改成"安全可重复执行"版本。

背景（2026-10-29 极高风险）：
  init.sql 原本每张表前面都有 `DROP TABLE IF EXISTS xxx;`，
  且 CREATE TABLE 不带 IF NOT EXISTS，开头还有硬编码的
  `CREATE DATABASE xianyu_autoreply; USE xianyu_autoreply;`。

  而 web_engine.run() 每次启动都会调 ensure_schema() → 执行整个 init.sql。
  以前因为 _INIT_SQL 路径写错（指向 backend/init.sql），这个脚本从未真正执行，
  所以没人发现它其实是"清库脚本"。一旦修好路径，引擎下次启动就会
  把 13 张表全部 DROP 重建 —— 卡密/商品/订单/规则全部清空。

改法：
  1. 删除所有 `DROP TABLE IF EXISTS ...;`
  2. `CREATE TABLE` → `CREATE TABLE IF NOT EXISTS`
  3. 删除 `CREATE DATABASE ...;` 和 `USE ...;`（库名由连接决定，
     硬编码会导致用户填了别的库名时，表建到错误的库里去）
  4. `INSERT INTO engine_status` → `INSERT IGNORE INTO`（避免重复键报错）

用法：python tools/make_init_sql_safe.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "frontend" / "init.sql"

BANNER = """-- 闲鱼卖家客服 · 网页端数据库初始化（安全版，可重复执行）
-- 数据库：使用连接当前选中的库（不再硬编码 xianyu_autoreply，
--         避免用户填了别的库名时把表建到错误的库里）
-- 字符集：utf8mb4（支持 emoji 等 4 字节字符）
-- 适用：MySQL 5.7.26（phpStudy）
--
-- 【重要】本脚本不含任何 DROP 语句：重复执行不会删除已有数据。
--         如需彻底重建，请手动 DROP DATABASE。
"""


def main() -> int:
    if not SQL.is_file():
        print(f"找不到 {SQL}")
        return 1

    src = SQL.read_text(encoding="utf-8")
    orig_len = len(src)

    # 1) 删掉开头的 CREATE DATABASE / USE（连同其后的空行）
    src = re.sub(r"^\s*CREATE DATABASE[^;]*;\s*$", "", src, flags=re.M)
    src = re.sub(r"^\s*USE\s+[A-Za-z0-9_]+;\s*$", "", src, flags=re.M)

    # 2) 删掉所有 DROP TABLE IF EXISTS
    drops = len(re.findall(r"^\s*DROP TABLE IF EXISTS[^;]*;\s*$", src, flags=re.M))
    src = re.sub(r"^\s*DROP TABLE IF EXISTS[^;]*;\s*$", "", src, flags=re.M)

    # 3) CREATE TABLE → CREATE TABLE IF NOT EXISTS
    src = re.sub(r"\bCREATE TABLE\s+(?!IF NOT EXISTS)", "CREATE TABLE IF NOT EXISTS ", src)

    # 4) engine_status 的初始行用 INSERT IGNORE，避免重复执行时报主键冲突
    src = re.sub(r"\bINSERT INTO engine_status\b", "INSERT IGNORE INTO engine_status", src)

    # 5) 压缩多余空行（4 个以上换行压成 2 个）
    src = re.sub(r"\n{3,}", "\n\n", src)

    # 6) 换上新头部（保留原有的表定义部分）
    body = src.split("-- ============ 全局设置", 1)
    tail = "-- ============ 全局设置" + body[1] if len(body) == 2 else src
    src = BANNER + "\n" + tail

    SQL.write_text(src, encoding="utf-8")

    print(f"已改写 {SQL.name}")
    print(f"  删除 DROP 语句: {drops} 条")
    print(f"  CREATE TABLE → IF NOT EXISTS")
    print(f"  删除硬编码 CREATE DATABASE / USE")
    print(f"  文件大小: {orig_len} → {len(src)} 字节")

    # 自检：不应再有 DROP / USE / 裸 CREATE TABLE
    left_drop = len(re.findall(r"^\s*DROP TABLE", src, flags=re.M))
    left_use = len(re.findall(r"^\s*USE\s+", src, flags=re.M))
    bare = len(re.findall(r"\bCREATE TABLE\s+(?!IF NOT EXISTS)", src))
    print(f"  自检 → DROP:{left_drop}  USE:{left_use}  裸CREATE_TABLE:{bare}")
    return 0 if (left_drop == left_use == bare == 0) else 2


if __name__ == "__main__":
    sys.exit(main())
