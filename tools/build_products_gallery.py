"""build_products_gallery.py - 下载采集到的商品图片并生成 HTML 看板。

输入：data/collected_products.json（由 tools/collect_shop_products.py 产出）
输出：data/product_images/<item_id>/cover.webp + gN.* （图片原图）
      data/products_gallery.html（本地看板，相对路径引用图片）

用法：
    python tools/build_products_gallery.py
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
JSON_PATH = DATA / "collected_products.json"
IMG_DIR = DATA / "product_images"
OUT_HTML = DATA / "products_gallery.html"

# 实物商品关键词（用户作为卖家卖的电脑硬件等；与虚拟卡密业务无关）
PHYSICAL_HINTS = ("5060", "1060", "显卡", "七彩虹", "影驰", "R5 7500")


def fetch(url: str, dest: Path) -> int:
    """下载单张图片，返回字节数。"""
    req = urllib.request.Request(
        url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://www.goofish.com/",
        }
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        blob = r.read()
    dest.write_bytes(blob)
    return len(blob)


def download_all(items: list[dict]) -> tuple[int, int]:
    """并发下载全部主图与图集，返回 (成功, 失败)。"""
    tasks: list[tuple[str, Path]] = []
    for it in items:
        d = IMG_DIR / it["item_id"]
        d.mkdir(parents=True, exist_ok=True)
        if it.get("img"):
            tasks.append((it["img"], d / "cover.webp"))
        for i, u in enumerate(it.get("gallery") or []):
            ext = ".webp" if ".webp" in u.lower() else ".jpg"
            tasks.append((u, d / f"g{i + 1}{ext}"))
    ok = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch, u, p): (u, p) for u, p in tasks}
        for f in concurrent.futures.as_completed(futs):
            try:
                f.result()
                ok += 1
            except Exception:  # noqa: BLE001
                fail += 1
    return ok, fail


def is_physical(it: dict) -> bool:
    text = (it.get("title") or "") + (it.get("detail_title") or "")
    return any(h in text for h in PHYSICAL_HINTS)


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def card_html(it: dict) -> str:
    iid = it["item_id"]
    d = IMG_DIR / iid
    cover = d / "cover.webp"
    cover_rel = f"product_images/{iid}/cover.webp" if cover.exists() else ""
    gallery = sorted(d.glob("g*.*"))
    phys = is_physical(it)
    badge = ('<span class="badge phys">实物</span>' if phys
             else '<span class="badge virt">虚拟</span>')
    price = it.get("price") or ""
    origin = it.get("origin_price_num")
    origin_html = (f'<span class="origin">￥{origin:g}</span>'
                   if origin and it.get("price_num") and origin > it["price_num"]
                   else "")
    gal_thumbs = "".join(
        f'<img loading="lazy" src="product_images/{iid}/{g.name}" alt="">'
        for g in gallery[:8]
    )
    desc = esc(it.get("desc") or "").replace("\n", "<br>")
    return f"""<div class="card{' phys' if phys else ''}">
  <div class="cover">{f'<img loading="lazy" src="{cover_rel}" alt="">' if cover_rel else '<div class="nocover">无图</div>'}</div>
  <div class="body">
    <div class="title">{esc(it.get('detail_title') or it.get('title'))} {badge}</div>
    <div class="price">{esc(price)} {origin_html}</div>
    <div class="meta">ID {iid} · 图 {len(gallery)} 张 · <a href="https://www.goofish.com/item?id={iid}" target="_blank">原页</a></div>
    <div class="desc">{desc}</div>
    <div class="gal">{gal_thumbs}</div>
  </div>
</div>"""


def build_html(items: list[dict]) -> str:
    virt = [it for it in items if not is_physical(it)]
    phys = [it for it in items if is_physical(it)]
    cards = "\n".join(card_html(it) for it in virt + phys)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>闲鱼在售商品采集 · {len(items)} 个</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #f5f6f8; color: #222; padding: 24px; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
           gap: 16px; }}
  .card {{ background: #fff; border-radius: 10px; overflow: hidden;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); display: flex; flex-direction: column; }}
  .card.phys {{ opacity: .75; }}
  .cover {{ height: 170px; background: #eef0f3; display: flex;
            align-items: center; justify-content: center; }}
  .cover img {{ width: 100%; height: 100%; object-fit: cover; }}
  .nocover {{ color: #999; font-size: 13px; }}
  .body {{ padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; }}
  .title {{ font-size: 14px; font-weight: 600; line-height: 1.4; }}
  .badge {{ font-size: 11px; padding: 1px 6px; border-radius: 4px; font-weight: 400;
            vertical-align: middle; }}
  .badge.virt {{ background: #e6f7e9; color: #1a7f37; }}
  .badge.phys {{ background: #f0f0f0; color: #888; }}
  .price {{ font-size: 18px; font-weight: 700; color: #d4380d; }}
  .origin {{ font-size: 12px; color: #999; text-decoration: line-through; font-weight: 400; }}
  .meta {{ font-size: 12px; color: #999; }}
  .meta a {{ color: #576b95; }}
  .desc {{ font-size: 12.5px; color: #444; line-height: 1.55; max-height: 150px;
           overflow: auto; background: #fafafa; border-radius: 6px; padding: 8px; }}
  .gal {{ display: flex; gap: 6px; overflow-x: auto; }}
  .gal img {{ width: 56px; height: 56px; object-fit: cover; border-radius: 4px;
              flex-shrink: 0; border: 1px solid #eee; }}
</style>
</head>
<body>
<h1>闲鱼在售商品采集</h1>
<div class="sub">共 {len(items)} 个（虚拟 {len(virt)} · 实物 {len(phys)}）·
  数据源 www.goofish.com/personal · 采集时间 2026-10-17 · 图片已下载到 data/product_images/</div>
<div class="grid">
{cards}
</div>
</body>
</html>"""


def main() -> int:
    items = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    print(f"[*] {len(items)} 个商品，开始下载图片 ...")
    ok, fail = download_all(items)
    print(f"[*] 图片下载: 成功 {ok} 失败 {fail}")
    html = build_html(items)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"[OK] 看板已生成: {OUT_HTML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
