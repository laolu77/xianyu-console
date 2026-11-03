"""把 shop_items 里所有商品的封面图下载到本地，供网页直接显示。

背景（2026-11-03 用户部署机实测）：
  商品列表页原来直接用 `img.alicdn.com` 的 CDN 链接。部署机器的网络访问不了
  阿里 CDN（或很慢），图片就整片空白 —— 而数据库里 `img` 字段明明是完整的。
  把封面落到 `data/product_images/<item_id>/cover.<ext>` 之后，网页通过
  `api.php?action=product_image&item_id=...` 读本地文件，不再依赖外网。

用法：
    python tools/download_covers.py           # 只补缺失的
    python tools/download_covers.py --force   # 全部重下

依赖：仅标准库（urllib），不引入额外包。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# 本机/内网直连，绝不能走代理（环境里的 HTTP(S)_PROXY 会让下载 502）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

IMG_DIR = ROOT / "data" / "product_images"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _ext_from_url(url: str, ctype: str = "") -> str:
    """按内容类型优先判断扩展名，其次看 URL 尾巴。"""
    ctype = (ctype or "").lower()
    if "webp" in ctype:
        return ".webp"
    if "jpeg" in ctype or "jpg" in ctype:
        return ".jpg"
    if "png" in ctype:
        return ".png"
    low = url.lower()
    for e in (".webp", ".jpg", ".jpeg", ".png"):
        if low.endswith(e):
            return ".jpg" if e == ".jpeg" else e
    return ".jpg"


def fetch(url: str, timeout: float = 20.0) -> tuple[bytes, str]:
    """下载一张图，返回 (内容, Content-Type)。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.goofish.com/",
        "Accept": "image/webp,image/*,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), (r.headers.get("Content-Type") or "")


def _url_variants(url: str):
    """同一张图的可选 URL 形式。

    阿里 CDN 的图片链接带尺寸后缀，例如
      .../xxx.png_450x10000.jpg_.webp
    个别网络下这种后缀会 400 / 403，但去掉后缀的原图往往是通的。
    """
    yield url
    stripped = re.sub(r"_\d+x\d+[^/]*?\.webp$", "", url)
    if stripped and stripped != url:
        yield stripped
    no_webp = url[:-len("_.webp")] if url.endswith("_.webp") else ""
    if no_webp and no_webp not in (url, stripped):
        yield no_webp


def fetch_with_retry(url: str, tries: int = 3) -> tuple[bytes, str]:
    """下载图片：先重试同一个 URL，再换 URL 变体，全部失败才抛。

    为什么需要：部署机网络到 img.alicdn.com 不稳定，单次 12 秒超时就整张放弃，
    结果 data/product_images 里只落了一半（2026-11-03 实测 24 个只下了 15 个）。
    """
    last = None
    for attempt in range(tries):
        for u in _url_variants(url):
            try:
                data, ctype = fetch(u)
                if len(data) < 1024:
                    raise ValueError("内容过小(%dB)" % len(data))
                return data, ctype
            except Exception as exc:  # noqa: BLE001
                last = exc
        time.sleep(0.8 * (attempt + 1))
    raise last if last else RuntimeError("下载失败")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（调试用）")
    args = ap.parse_args()

    import web_config as db

    # web_config 没有 query_all 封装，直接借它的连接
    conn = db.conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT item_id, title, img FROM shop_items "
                "WHERE img IS NOT NULL AND img <> '' ORDER BY id DESC")
            rows = [dict(zip(("item_id", "title", "img"), r))
                    for r in cur.fetchall()]
    finally:
        conn.close()
    if args.limit:
        rows = rows[: args.limit]
    print(f"共 {len(rows)} 个商品带图片链接")

    ok = skip = fail = 0
    failed = []
    for r in rows:
        iid = str(r.get("item_id") or "").strip()
        url = str(r.get("img") or "").strip()
        title = (r.get("title") or "")[:24]
        if not iid or not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        d = IMG_DIR / iid
        exists = any((d / f"cover{e}").is_file()
                     for e in (".webp", ".jpg", ".png"))
        if exists and not args.force:
            skip += 1
            continue
        try:
            data, ctype = fetch_with_retry(url)
            if len(data) < 1024:
                raise ValueError(f"内容过小({len(data)}B)")
            d.mkdir(parents=True, exist_ok=True)
            ext = _ext_from_url(url, ctype)
            for old in ("cover.webp", "cover.jpg", "cover.png"):
                p = d / old
                if p.is_file() and p.suffix != ext:
                    p.unlink(missing_ok=True)
            (d / f"cover{ext}").write_bytes(data)
            ok += 1
            print(f"  ✓ {iid} {len(data)//1024}KB  {title}")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            failed.append((iid, type(exc).__name__, str(exc)[:50], title))
            print(f"  ✗ {iid} {type(exc).__name__}: {str(exc)[:60]}  {title}")

    print(f"\n完成：下载 {ok}，已有跳过 {skip}，失败 {fail}")
    if fail:
        print("失败清单（下次启动引擎会自动重试）：")
        for iid, name, msg, title in failed:
            print(f"  {iid}  {name}: {msg}  {title}")
        print("若一直失败，说明那台机器访问不了 img.alicdn.com —— "
              "可以在能上网的机器上跑一次，把 data/product_images 整个目录拷过去")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
