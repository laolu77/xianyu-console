"""批量发布商品：扫描工作目录 → 解析每个商品文件夹 → 逐个上传到闲鱼。

目录约定（用户自建，层级固定两层）：

    <工作目录>/
    ├── 我的世界模型-Nog毛绒玩具/      ← 一个商品一个文件夹
    │   ├── 商品信息.xlsx            ← 表格（.xlsx/.xls/.csv 都行，名字随意）
    │   ├── 1.jpg
    │   └── 2.jpg
    └── 另一个商品/
        ├── 信息.csv
        └── ...

表格两种写法都支持（自动识别）：

  【横表】第一行是表头，第二行是值
      商品名称 | 价格 | 原价 | 库存 | 描述 | 卡密
      xxx     | 6    | 9.9  | 999 | ...  | ...

  【竖表】A 列是字段名，B 列是值
      商品名称 | xxx
      价格     | 6

用法：
    python tools/batch_publish.py --scan "D:/闲鱼批量上传"
        只扫描并输出 JSON（供网页预览），不改任何东西
    python tools/batch_publish.py --run "D:/闲鱼批量上传" [--limit N]
        真正逐个上传；会先把每个商品落盘到 data/publish/ 再调 publish_item.py

设计取舍：
- **每个商品单独起一个 publish_item.py 子进程**，而不是在同一个进程里连着传。
  闲鱼发布页是 SPA，同进程连续操作容易残留上一单的表单状态（今天已经因为这个
  发出过 9 张图的商品）；独立进程 + 每次 reload 最干净，单笔失败也不影响后续。
- 表格解析**宁可宽松**（列名别名、横竖表、CSV 编码），因为用户手写表格千奇百怪；
  但**必填项缺失就跳过该商品并报错**，绝不猜价格。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from _common import log as _c_log, write_progress as _c_write

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "publish"
BACKEND = ROOT / "backend"
PY = sys.executable
PUBLISH_SCRIPT = ROOT / "tools" / "publish_item.py"
PROGRESS = ROOT / "data" / "batch_progress.json"
# 持久化队列：支持关网页/隔天重启后续跑，不重复发也不漏发
QUEUE = ROOT / "data" / "batch_queue.json"
# 已上传成功的商品文件夹会被移动到这里（下划线开头，scan 自动跳过）
ARCHIVE = "_已上传"
# 上一次 scan 被判为"测试文件夹"而跳过的名字（供进度/前端展示）
_LAST_SKIPPED_TEST: list[str] = []

# 字段别名：key 是内部字段，value 是表格里可能出现的列名（归一化后比较）
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("商品名称", "宝贝名称", "标题", "商品标题", "名称", "title", "name"),
    "price": ("价格", "售价", "卖价", "price"),
    "origin": ("原价", "划线价", "市场价", "origin", "originprice", "oldprice"),
    "stock": ("库存", "数量", "stock", "qty", "quantity"),
    "desc": ("描述", "详情", "简介", "商品描述", "宝贝描述", "desc", "description"),
    "code": ("卡密", "卡密内容", "内容", "兑换码", "链接", "code", "content"),
}
REQUIRED = ("title", "price")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
TABLE_EXT = {".xlsx", ".xlsm", ".csv"}


# 日志与进度写盘统一走 tools/_common.py
def log(msg: str) -> None:
    _c_log(msg, "batch")


def write_progress(**kw) -> None:
    kw["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _c_write(PROGRESS, kw)


def norm_key(s: str) -> str:
    """归一化列名：去空格/冒号/星号，转小写。"""
    return re.sub(r"[\s:：*　]+", "", str(s or "")).lower()


def pick_field(raw: dict[str, str], field: str) -> str:
    """从已归一化的行字典里按别名取值。"""
    for alias in FIELD_ALIASES[field]:
        v = raw.get(norm_key(alias))
        if v not in (None, ""):
            return str(v).strip()
    return ""


def read_table(path: Path) -> dict[str, str]:
    """读表格，返回 {归一化列名: 值}。支持横表/竖表、xlsx/csv。

    Args:
        path: 表格文件路径。

    Returns:
        归一化后的键值对；解析失败返回空字典。
    """
    rows: list[list[str]] = []
    if path.suffix.lower() == ".csv":
        # 编码探测：闲鱼导出的 CSV 常带 BOM；中文表格常见 GBK
        for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
            try:
                with open(path, encoding=enc, newline="") as f:
                    rows = [r for r in csv.reader(f)]
                break
            except (UnicodeDecodeError, LookupError):
                continue
    else:
        try:
            from openpyxl import load_workbook
        except ImportError:
            log("缺少 openpyxl，无法读取 .xlsx：pip install openpyxl")
            return {}
        try:
            wb = load_workbook(path, data_only=True, read_only=True)
            ws = wb.active
            for row in ws.iter_rows(values_only=True):
                rows.append(["" if c is None else str(c).strip() for c in row])
            wb.close()
        except Exception as exc:  # noqa: BLE001
            log(f"读取 {path.name} 失败：{exc}")
            return {}

    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return {}

    # 判横表还是竖表（2026-10-20 实测踩坑：只看"第一列命中字段名"会把横表误判成竖表，
    # 因为横表的表头行本身就是一堆字段名，导致价格被读成"原价"这种错位）。
    # 正确特征：
    #   横表 → **第一行**里有 >=2 个字段名（那就是表头），且后续行第一列基本不是字段名；
    #   竖表 → **第一列**里有 >=2 个字段名。
    known = {norm_key(a) for group in FIELD_ALIASES.values() for a in group}
    header_hits = sum(1 for h in rows[0] if norm_key(h) in known)
    col_hits = sum(1 for r in rows[1:] if len(r) >= 2 and norm_key(r[0]) in known)
    is_vertical = col_hits >= 2 and col_hits > header_hits
    if is_vertical and not (header_hits >= 2 and header_hits >= col_hits):
        out: dict[str, str] = {}
        for r in rows:
            if len(r) >= 2 and norm_key(r[0]) in known:
                out[norm_key(r[0])] = str(r[1]).strip()
        return out

    # 横表：第一行当表头，取第一条数据行
    header = [norm_key(h) for h in rows[0]]
    body = rows[1] if len(rows) > 1 else []
    return {h: (str(body[i]).strip() if i < len(body) else "")
            for i, h in enumerate(header) if h}


def list_images(folder: Path) -> list[Path]:
    """取商品文件夹里的图片，按文件名自然排序（1.jpg < 2.jpg < 10.jpg）。"""
    imgs = [p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXT]
    def key(p: Path):
        m = re.search(r"(\d+)", p.stem)
        return (int(m.group(1)) if m else 999999, p.name.lower())
    return sorted(imgs, key=key)


def parse_item_folder(folder: Path) -> dict:
    """解析一个商品文件夹，返回扫描结果（不写库、不上传）。

    Returns:
        {"ok": bool, "dir": 名称, "title"/"price"/..., "images": [...], "error": str}
    """
    res: dict = {"ok": False, "dir": folder.name, "folder": str(folder)}
    tables = [p for p in folder.iterdir()
              if p.is_file() and p.suffix.lower() in TABLE_EXT]
    if not tables:
        res["error"] = "文件夹里没有表格（.xlsx/.xls/.csv）"
        return res
    # 有多个表格时取第一个（按名字排序，稳定）
    tables.sort(key=lambda p: p.name)
    raw = read_table(tables[0])
    if not raw:
        res["error"] = f"表格解析为空：{tables[0].name}"
        return res

    imgs = list_images(folder)
    res.update({
        "table": tables[0].name,
        "title": pick_field(raw, "title"),
        "price": pick_field(raw, "price"),
        "origin": pick_field(raw, "origin"),
        "stock": pick_field(raw, "stock") or "999",
        "desc": pick_field(raw, "desc"),
        "code": pick_field(raw, "code"),
        "images": [p.name for p in imgs],
        "image_count": len(imgs),
    })
    missing = [f for f in REQUIRED if not res.get(f)]
    if missing:
        res["error"] = "表格缺少必填项：" + "、".join(
            {"title": "商品名称", "price": "价格"}[f] for f in missing)
        return res
    if not imgs:
        res["error"] = "文件夹里没有图片"
        return res
    res["ok"] = True
    return res


def is_test_folder(name: str) -> bool:
    """文件夹名是否像"测试用"的。

    为什么要有这条：调试时造的测试商品很容易忘在目录里，一旦被批量上传
    就真的挂到店铺上了（今天已经发生过几次，每次都要人工删除）。
    所以默认**凡是名字含「测试」「test」的一律不上传**，
    只有显式加 `--allow-test` 才放行。

    Args:
        name: 文件夹名。

    Returns:
        看起来是测试文件夹返回 True。
    """
    low = (name or "").lower()
    return ("测试" in low) or ("test" in low)


def scan(root: Path, allow_test: bool = False) -> list[dict]:
    """扫描工作目录下的一级子文件夹（每个就是一个商品）。

    跳过规则：
      · `_` `.` `~$` 开头 —— 放模板、备份、Excel 临时文件；
      · 名字含「测试」/「test」 —— 防调试数据被误发（除非 allow_test=True）。

    Args:
        root: 工作目录。
        allow_test: True 时才把测试文件夹也当成商品（仅调试用）。

    Returns:
        解析结果列表；被当成测试跳过的文件夹名记在 `_skipped_test` 里
        （由调用方取出展示，不混进商品列表）。
    """
    if not root.is_dir():
        return []
    out = []
    skipped_test: list[str] = []
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        if d.name[:1] in ("_", ".") or d.name.startswith("~$"):
            continue
        if not allow_test and is_test_folder(d.name):
            skipped_test.append(d.name)
            log(f"跳过测试文件夹：{d.name}")
            continue
        out.append(parse_item_folder(d))
    if skipped_test:
        _LAST_SKIPPED_TEST[:] = skipped_test
    return out


def stage_to_publish(item: dict) -> str:
    """把商品文件夹落盘成 data/publish/<dir>/（复制图片 + 写 manifest）。

    复用现有 publish 结构，好处是上传、留证、事后核对都走同一套路径。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", item["title"])[:20] or "item"
    dirname = f"batch_{stamp}_{safe}"
    base = DATA / dirname
    base.mkdir(parents=True, exist_ok=True)

    src = Path(item["folder"])
    written = []
    for i, name in enumerate(item["images"], start=1):
        s = src / name
        ext = s.suffix.lower().lstrip(".")
        ext = "jpg" if ext in ("jpeg", "jpg") else ext
        dst = base / f"{i:02d}.{ext}"
        shutil.copyfile(s, dst)
        written.append(dst.name)

    manifest = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending_upload",
        "title": item["title"],
        "price": item["price"],
        "origin_price": item.get("origin") or "",
        "stock": item.get("stock") or "999",
        "desc": item.get("desc") or "",
        "is_virtual": True,
        "bundle_id": 0,
        "images": written,
        "source_folder": str(src),
        "code": item.get("code") or "",
        "history": [{"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "event": "staged_from_folder"}],
    }
    (base / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return dirname


def _db():
    """惰性导入 backend/web_config（避免 --scan 模式也依赖数据库）。"""
    sys.path.insert(0, str(BACKEND))
    import web_config  # noqa: PLC0415
    return web_config


def engine_is_running() -> bool:
    """引擎是否真在跑：以心跳 <15s 为准（与 PHP 端 xy_engine_alive 同口径）。"""
    try:
        db = _db()
        c = db.conn().cursor()
        c.execute("SELECT state,last_heartbeat FROM engine_status WHERE id=1")
        row = c.fetchone()
        if not row or row[0] != "running" or not row[1]:
            return False
        return (datetime.now() - row[1]).total_seconds() < 15
    except Exception:  # noqa: BLE001
        return False


def set_engine(run: bool) -> None:
    """暂停/恢复自动发货引擎。

    批量上传会长时间占用闲鱼页面，引擎的定时扫描每轮都要 goto 切路由，
    两边同时操作会把发布表单冲掉（今天就因此发错过图）。
    所以批量开始先停引擎，结束时恢复——与网页「发布商品」的做法一致。
    """
    try:
        db = _db()
        conn = db.conn()
        c = conn.cursor()
        c.execute("INSERT INTO sw_control (`key`,val,updated_at) VALUES ('engine',%s,NOW())"
                  " ON DUPLICATE KEY UPDATE val=VALUES(val), updated_at=NOW()",
                  ("run" if run else "stop",))
        if not run:
            c.execute("INSERT INTO commands (cmd) VALUES ('stop')")
        conn.commit()
        log(f"引擎已{'恢复' if run else '暂停'}")
    except Exception as exc:  # noqa: BLE001
        log(f"引擎{'恢复' if run else '暂停'}失败（继续上传）: {exc}")


def _limits() -> tuple[int, int]:
    """读批量上传的节流参数（网页设置页可改）。

    Returns:
        (间隔分钟, 每日上限)。读库失败时用安全默认值 60 分钟 / 5 个，
        宁可慢也不要在参数读不到时一口气把商品全发出去。
    """
    try:
        db = _db()
        s = db.load_settings()
        interval = max(1, int(s.get("batch_interval_min") or 60))
        daily = max(1, int(s.get("batch_daily_max") or 5))
        return interval, daily
    except Exception as exc:  # noqa: BLE001
        log(f"读取限速设置失败，用默认 60 分钟 / 5 个：{exc}")
        return 60, 5


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_queue() -> dict:
    """读持久化队列（支持中断续跑）。"""
    try:
        if QUEUE.exists():
            d = json.loads(QUEUE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_queue(q: dict) -> None:
    try:
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log(f"队列保存失败: {exc}")


def _sleep_until(ts: float, q: dict, why: str, total: int) -> None:
    """分段等待到指定时间戳，期间不断刷新进度（前端才能看到倒计时）。"""
    while True:
        left = int(ts - time.time())
        if left <= 0:
            return
        hh, mm = left // 3600, (left % 3600) // 60
        write_progress(status="waiting", current=q.get("done_count", 0), total=total,
                       ok=q.get("ok", 0), fail=q.get("fail", 0),
                       next_at=datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                       message=f"{why}，还剩 {hh} 小时 {mm} 分（{total - q.get('done_count', 0)} 个待传）")
        time.sleep(min(30, left))


def run_batch(root: Path, limit: int = 0, fresh: bool = False,
              allow_test: bool = False) -> int:
    """批量上传（队列 + 限速）：按设置里的间隔和每日上限逐个发布。

    为什么做成队列而不是"一口气传完"：
    用户要求「间隔默认 1 小时、每天最多 5 个」——就是模拟人工节奏，避免短时间
    集中发商品被平台风控。所以队列必须**持久化**：关网页、断网、隔天重启，
    都能从上次的进度接着跑，不重复发也不漏发。

    Args:
        root: 工作目录（一个商品一个子文件夹）。
        limit: 本次最多处理几个（调试用，0 = 不限）。
        fresh: True 表示重建队列（忽略旧进度）。

    Returns:
        0 全部成功 / 3 有失败 / 1 无可上传 / 4 参数错误
    """
    interval_min, daily_max = _limits()
    q = {} if fresh else load_queue()

    # 队列里还有待传的，直接续跑（不重新扫目录，避免路径/内容变化导致错配）
    if not q.get("pending"):
        items = scan(root, allow_test=allow_test)
        if not items:
            write_progress(status="error", message=f"目录下没有商品文件夹：{root}")
            log("没有可上传的商品")
            return 1
        ok_items = [i for i in items if i.get("ok")]
        if not ok_items:
            write_progress(status="error", message="没有可上传的商品（全部解析失败）")
            return 1
        if limit > 0:
            ok_items = ok_items[:limit]
        q = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pending": ok_items,
            "done": [],
            "day_count": {},
            "next_at": 0,
            "done_count": 0,
            "ok": 0,
            "fail": 0,
            "skipped": len(items) - len(ok_items),
        }
        save_queue(q)
        log(f"新建队列：{len(ok_items)} 个待传，间隔 {interval_min} 分钟，每日上限 {daily_max} 个")
    else:
        log(f"续跑已有队列：剩 {len(q['pending'])} 个待传")

    total = q.get("done_count", 0) + len(q["pending"])
    # 注意：**引擎的暂停/恢复放在 _upload_one 里**，只包住"真正上传的那几十秒"。
    # 等待上传间隔（可能长达 1 小时）期间引擎必须保持运行，否则那段时间买家
    # 付款没人自动发货（2026-10-20 用户指出：时间检测是时间检测，不该一直关引擎）。
    today = _today()
    day_count = q.setdefault("day_count", {})
    used = int(day_count.get(today, 0))

    while q["pending"]:
        today = _today()
        used = int(day_count.get(today, 0))
        # 1) 今日配额
        if used >= daily_max:
            nxt = (datetime.now().replace(hour=0, minute=5, second=0, microsecond=0)
                   + timedelta(days=1))
            log(f"今日已传 {used} 个（上限 {daily_max}），等到明天 {nxt:%H:%M} 继续")
            write_progress(status="waiting", current=q.get("done_count", 0), total=total,
                           ok=q.get("ok", 0), fail=q.get("fail", 0),
                           next_at=nxt.strftime("%Y-%m-%d %H:%M:%S"),
                           message=f"今日已达上限 {daily_max} 个，明天 00:05 继续"
                                   f"（剩 {len(q['pending'])} 个待传）")
            # 配额用完就结束进程，明天用户再点一次「开始上传」会续跑
            return 0

        # 2) 上传间隔
        next_at = float(q.get("next_at") or 0)
        if next_at > time.time():
            log(f"距下次上传还有 {int(next_at - time.time())} 秒")
            _sleep_until(next_at, q, "等待上传间隔", total)

        it = q["pending"][0]
        title = it["title"]
        write_progress(status="running", current=q.get("done_count", 0), total=total,
                       ok=q.get("ok", 0), fail=q.get("fail", 0), current_title=title,
                       message=f"正在上传：{title}"
                               f"（今日 {used}/{daily_max}）")

        ok, msg, dirname = _upload_one(it)
        if ok:
            q["ok"] = q.get("ok", 0) + 1
        else:
            q["fail"] = q.get("fail", 0) + 1
        q["done_count"] = q.get("done_count", 0) + 1
        q["done"] = (q.get("done", []) + [{
            "title": title, "ok": ok, "dir": dirname, "message": msg,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}])[-50:]
        day_count[today] = used + 1
        q["next_at"] = time.time() + interval_min * 60
        q["pending"].pop(0)
        save_queue(q)

        write_progress(status="running", current=q["done_count"], total=total,
                       ok=q.get("ok", 0), fail=q.get("fail", 0),
                       message=f"已完成 {q['done_count']}/{total}"
                               f"（成功 {q.get('ok', 0)}，失败 {q.get('fail', 0)}）",
                       details=q["done"][-20:])
        log(f"[{q['done_count']}/{total}] {'OK' if ok else 'FAIL'} {title} | {msg}")

    write_progress(status="done", current=q.get("done_count", 0), total=total,
                   ok=q.get("ok", 0), fail=q.get("fail", 0),
                   message=f"全部完成：成功 {q.get('ok', 0)}，失败 {q.get('fail', 0)}",
                   details=q["done"][-20:])
    log(f"队列已清空：成功 {q.get('ok', 0)}，失败 {q.get('fail', 0)}")
    return 0 if not q.get("fail") else 3


def _upload_one(it: dict) -> tuple[bool, str, str]:
    """上传单个商品：落盘 → **调唯一的上传实现 publish_item.py** → 建卡卷绑定。

    批量不自己实现上传：`publish_item.py` 是全项目唯一的上传入口
    （单个发布、批量发布、命令行调试都走它）。批量这边只负责三件事：
      1. 时间调度（间隔 / 每日上限，见 run_batch 的队列循环）
      2. 把商品文件夹落盘成 data/publish/<dir>（stage_to_publish）
      3. 传完后按表里的卡密建同名卡卷并绑定（create_and_bind_bundle）

    调 CLI 子进程而不是 import 函数：SPA 表单会残留，每单必须独立进程。

    **引擎只在这里暂停**，而且只覆盖"落盘 + 上传"这一小段（约 30-60 秒）。
    等待上传间隔的几十分钟里引擎照常跑，买家下单仍会正常自动发货。

    Returns:
        (是否成功, 说明, publish 目录名)
    """
    title = it["title"]
    was_running = engine_is_running()
    if was_running:
        set_engine(False)
        for _ in range(20):          # 最多等 10s 让它真的退出
            time.sleep(0.5)
            if not engine_is_running():
                break
    try:
        return _do_upload(it)
    finally:
        if was_running:
            set_engine(True)


def _do_upload(it: dict) -> tuple[bool, str, str]:
    """实际的上传动作（被 _upload_one 包在引擎暂停区间内）。"""
    title = it["title"]
    try:
        dirname = stage_to_publish(it)
    except Exception as exc:  # noqa: BLE001
        return False, f"落盘失败：{exc}", ""

    cmd = [PY, str(PUBLISH_SCRIPT), "--dir", dirname, "--yes",
           "--progress", str(DATA / dirname / "_progress.json")]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300)
        rc = r.returncode
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
    except subprocess.TimeoutExpired:
        return False, "超时（>300s）", dirname
    except Exception as exc:  # noqa: BLE001
        return False, str(exc), dirname

    if rc != 0:
        return False, " | ".join(tail)[-200:], dirname

    if not it.get("code"):
        msg = "表格未提供卡密，仅发布商品"
    else:
        item_id = ""
        pf = DATA / dirname / "_progress.json"
        try:
            if pf.exists():
                item_id = json.loads(pf.read_text(encoding="utf-8")).get("item_id") or ""
        except Exception:  # noqa: BLE001
            pass
        try:
            manifest = json.loads((DATA / dirname / "manifest.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            manifest = {}
        msg = create_and_bind_bundle(title, it["code"], item_id, manifest)

    # 发布成功后把源文件夹移进「_已上传」，下次扫描就不会再捞到它。
    # 这样"防重复上传"完全靠文件系统本身，不需要在数据库里维护已上传清单
    # （2026-10-20 用户提出的做法，简单且不会因为队列丢失而重发）。
    archived = archive_item(it)
    if archived:
        msg += f"；已移入 {ARCHIVE}/"
    return True, msg, dirname


def archive_item(item: dict) -> str:
    """把已上传的商品文件夹移动到工作目录下的 `_已上传/`。

    为什么用文件移动而不是数据库去重：文件系统就是唯一真相源 —— 移走了就
    永远不会再被扫描到，程序重装/换电脑/队列丢失都不会导致重复发布。

    Args:
        item: parse_item_folder() 的结果，需含 "folder"。

    Returns:
        移动后的路径（失败返回空串，不影响发布结果）。
    """
    try:
        src = Path(item["folder"])
        if not src.is_dir():
            return ""
        dest_root = src.parent / ARCHIVE
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / src.name
        if dest.exists():   # 同名（同名商品发过两次）→ 加时间戳，不覆盖
            dest = dest_root / f"{src.name}_{datetime.now():%Y%m%d_%H%M%S}"
        shutil.move(str(src), str(dest))
        return str(dest)
    except Exception as exc:  # noqa: BLE001
        log(f"移动已上传文件夹失败（不影响发布结果）: {exc}")
        return ""

def create_and_bind_bundle(title: str, content: str, item_id: str,
                           manifest: dict) -> str:
    """为刚发布的商品创建同名卡卷并绑定。

    为什么用商品名当卡卷名：用户明确「卡卷名称就是商品名称」，这样网页上一眼能对上，
    重复上传同一商品时也能复用同一张卡卷（同名已存在就更新内容，不再新建）。

    为什么要顺带补一条 shop_items：刚发布的商品在数据库里还不存在（商品表靠采集同步），
    没有 item 就绑不上卡卷，自动发货也就用不了。这里按商品名补录，让"发布→可发货"闭环。

    Args:
        title: 商品名称（同时作为卡卷名）。
        content: 卡密/网盘内容。
        item_id: 闲鱼商品 ID（发布脚本返回，可能为空）。
        manifest: 落盘的商品信息（取价格/描述/图片）。

    Returns:
        给人看的结果说明。
    """
    sys.path.insert(0, str(BACKEND))
    try:
        import web_config as db  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return f"卡卷创建失败（导入 db 失败：{exc}）"
    try:
        conn = db.conn()
        c = conn.cursor()
        # 1) 卡卷：同名复用
        c.execute("SELECT id FROM card_bundles WHERE name=%s LIMIT 1", (title,))
        row = c.fetchone()
        if row:
            bid = int(row[0])
            c.execute("UPDATE card_bundles SET content=%s, updated_at=NOW() WHERE id=%s",
                      (content, bid))
        else:
            c.execute("INSERT INTO card_bundles (name, content, enabled, created_at, updated_at)"
                      " VALUES (%s,%s,1,NOW(),NOW())", (title[:128], content))
            bid = int(c.lastrowid)

        # 2) 商品：按闲鱼 item_id 或标题补录/查找
        iid_db = None
        if item_id:
            c.execute("SELECT id FROM shop_items WHERE item_id=%s LIMIT 1", (item_id,))
            r = c.fetchone()
            if r:
                iid_db = int(r[0])
        if iid_db is None:
            c.execute("SELECT id FROM shop_items WHERE title=%s LIMIT 1", (title,))
            r = c.fetchone()
            if r:
                iid_db = int(r[0])
            elif item_id:
                c.execute(
                    "INSERT INTO shop_items (item_id,title,price,origin_price,img,descr,"
                    "gallery,is_physical,created_at,updated_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,0,NOW(),NOW())",
                    (item_id, title[:255], str(manifest.get("price") or ""),
                     str(manifest.get("origin_price") or ""), "", manifest.get("desc") or "",
                     json.dumps(manifest.get("images") or [], ensure_ascii=False)))
                iid_db = int(c.lastrowid)
        if iid_db is None:
            conn.commit()
            return "卡卷已创建，但商品未入库（缺少闲鱼商品ID），可到网页手动绑定"

        # 3) 绑定（联合主键，忽略重复）
        c.execute("INSERT IGNORE INTO item_bundles (item_id, bundle_id, created_at)"
                  " VALUES (%s,%s,NOW())", (iid_db, bid))
        conn.commit()
        return "已创建同名卡卷并自动绑定，可直接参与自动发货"
    except Exception as exc:  # noqa: BLE001
        return f"卡卷创建/绑定失败：{exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", default="", help="只扫描目录并输出 JSON")
    ap.add_argument("--run", default="", help="执行批量上传")
    ap.add_argument("--limit", type=int, default=0, help="只传前 N 个（调试用）")
    ap.add_argument("--fresh", action="store_true", help="重建队列（忽略上次进度）")
    ap.add_argument("--allow-test", action="store_true",
                    help="允许上传名字含「测试」的文件夹（仅调试用）")
    args = ap.parse_args()

    if args.scan:
        root = Path(args.scan)
        items = scan(root)
        print(json.dumps({"root": str(root), "exists": root.is_dir(),
                          "rows": items, "skipped_test": _LAST_SKIPPED_TEST},
                         ensure_ascii=False))
        return 0

    if args.run:
        return run_batch(Path(args.run), args.limit, args.fresh, args.allow_test)

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
