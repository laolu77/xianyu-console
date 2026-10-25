"""collect_shop_products.py - 采集闲鱼个人主页的在售商品列表。

数据来源：www.goofish.com/personal（无参数 = 自己的主页），商品卡片是
``a[href*='item?id=']``。注意 class 名带 hash 后缀（CSS 模块化），
选择器一律用 ``[class*='...']`` 子串匹配。

进度上报（2026-10-17 网页端「获取商品」按钮驱动）：
边采边把进度写入 data/collect_progress.json，前端轮询该文件渲染进度条：
    {"status": "running|done|error", "current": N, "total": M,
     "message": "...", "updated_at": "..."}
单实例保护：progress 为 running 且 10 分钟内刷新过，则拒绝重复启动。

用法（客户端须以 --remote-debugging-port=9222 运行）：
    python tools/collect_shop_products.py            # 采集列表 -> data/collected_products.json
    python tools/collect_shop_products.py --details  # 列表 + 逐个进详情页采简介全文

依赖：playwright（sync API，connect_over_cdp 附加到已运行的客户端，
不新起浏览器、不动引擎正在用的聊天页）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from _common import check_single_instance as _check_single_instance, write_progress as _c_write

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent.parent
OUT_JSON = BASE / "data" / "collected_products.json"
PROGRESS_JSON = BASE / "data" / "collect_progress.json"
CDP = "http://127.0.0.1:9222"
SELLER = "<用户名>"
# 进度文件 freshness 窗口：超过则视为上次采集已死，允许重新开始
STALE_SEC = 600


def write_progress(status: str, current: int = 0, total: int = 0,
                   message: str = "") -> None:
    """写采集进度（前端进度条数据源）。"""
    data = {
        "status": status,
        "current": current,
        "total": total,
        "message": message,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _c_write(PROGRESS_JSON, data)  # 写失败静默，不影响采集本身


# 单实例检查统一走 tools/_common.py（原来这里和 export_runner.py 各抄了一份）
def check_single_instance() -> bool:
    """已有鲜活采集在跑时返回 False（防重复）。"""
    return _check_single_instance(PROGRESS_JSON, STALE_SEC)

# 列表卡片提取：子串 class 匹配，规避 CSS 模块化 hash 后缀。
# sold 检测（2026-10-17 用户规则「显示卖掉了就不采集」）：已售卡片的外层
# 容器里盖着印章遮罩（div.maskWrap + img.itemStatus，纯图片无文本，
# innerText 扫不到），与商品卡 <a> 是兄弟节点——故从 <a> 向上找 1-3 层
# 父节点再查后代。实测已售卡片（影驰1060）有、未售（28 个样本）均无。
CARD_JS = """() => {
  const out = [];
  document.querySelectorAll("a[href*='item?id=']").forEach(a => {
    const img = a.querySelector('img');
    const titleEl = a.querySelector("[class*='main-title']");
    const priceWrap = a.querySelector("[class*='price-wrap']");
    const priceDesc = a.querySelector("[class*='price-desc']");
    const seller = a.querySelector("[class*='seller-text']");
    const m = a.href.match(/id=(\\d+)/);
    // 已售/下架印章：结构为 div.cardWarp > (div.maskWrap[>img.itemStatus] + a.卡片)，
    // 印章是卡片的亲兄弟。只查 (a) 自身内部 + (b) 直接外层容器的其他子节点——
    // 绝不能用祖先节点 querySelector：会跨卡片匹配到列表里任一已售卡的印章，
    // 把整列商品全误判成已售（2026-10-17 实测踩坑）。
    let sold = false;
    if (a.querySelector("[class*='itemStatus'], [class*='maskWrap']")) {
      sold = true;   // 印章在卡片内部（结构变体兜底）
    }
    if (!sold && a.parentElement) {
      for (const child of a.parentElement.children) {
        if (child === a) continue;
        const cls = (child.className || '').toString();
        if (cls.includes('itemStatus') || cls.includes('maskWrap')) { sold = true; break; }
      }
    }
    out.push({
      item_id: m ? m[1] : null,
      title: titleEl ? (titleEl.getAttribute('title') || titleEl.innerText).trim() : '',
      price: priceWrap ? priceWrap.innerText.replace(/\\s+/g, '') : '',
      origin_price: priceDesc ? (priceDesc.getAttribute('title') || priceDesc.innerText).trim() : '',
      img: img ? (img.currentSrc || img.src || '') : '',
      seller: seller ? seller.innerText.trim() : '',
      sold: sold
    });
  });
  return out;
}"""

# 详情页提取：标题 + 描述全文 + 图集。
# 实测（2026-10-17）：详情描述容器是页面第一个 [class*='desc'] 元素；
# 描述默认折叠，末尾有「展开」文字按钮，需先点击再取 innerText。
DETAIL_JS = """() => {
  const descEl = document.querySelector("[class*='desc']");
  const desc = descEl ? descEl.innerText.trim() : '';
  const imgs = [];
  if (descEl) {
    descEl.querySelectorAll('img').forEach(im => {
      const src = im.currentSrc || im.src || '';
      if (src.includes('img.alicdn.com') && !imgs.includes(src)) imgs.push(src);
    });
  }
  // 主图轮播区（描述文本上方的大图）
  for (const s of ["[class*='carousel']", "[class*='mainPic']", "[class*='gallery']"]) {
    try {
      document.querySelectorAll(s + ' img').forEach(im => {
        const src = im.currentSrc || im.src || '';
        if (src.includes('img.alicdn.com') && !imgs.includes(src)) imgs.push(src);
      });
    } catch (e) {}
  }
  const h1 = document.querySelector('h1');
  return {
    title: document.title || '',
    h1: h1 ? h1.innerText.trim() : '',
    desc: desc,
    imgs: imgs.slice(0, 20)
  };
}"""


def find_personal_page(browser):
    """找已打开的 personal 页；没有就经聊天页 window.open 开一个。

    Electron 客户端禁用了 Target.createTarget（/json/new 与 playwright
    new_page 均被拒），但 window.open 实际能开出页面（JS 拿到 null 而已）。
    实测开窗+导航延迟不稳定（10s~40s+），故：总等待 60s，每 10s 重试
    一次开窗，防止单次开窗被客户端静默吞掉。
    """
    def _scan():
        for ctx in browser.contexts:
            for p in ctx.pages:
                if "goofish.com/personal" in p.url:
                    return p
        return None

    hit = _scan()
    if hit:
        return hit
    # 没有 -> 借聊天页 window.open（带重试）
    chat = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "im-desktop" in p.url:
                chat = p
    if chat is None:
        return None
    for attempt in range(6):
        try:
            chat.evaluate(
                "() => window.open('https://www.goofish.com/personal', '_blank')"
            )
        except Exception:  # noqa: BLE001
            pass
        for _ in range(10):
            time.sleep(1)
            hit = _scan()
            if hit:
                return hit
    return None


def scroll_load_all(page, max_rounds: int = 20) -> None:
    """滚轮加载懒渲染的商品卡片，直到数量稳定。"""
    page.evaluate("() => window.scrollTo(0, 0)")
    time.sleep(1.0)
    last, stable = -1, 0
    for _ in range(max_rounds):
        page.mouse.wheel(0, 1600)
        time.sleep(1.0)
        n = page.locator("a[href*='item?id=']").count()
        if n == last:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last = n


def collect_list(page) -> tuple[list[dict], int]:
    """提取商品卡片：过滤他人推荐商品、按 item_id 去重、**跳过已售**
    （用户规则：图片盖「卖掉了」印章的不采集，2026-10-17）。

    Returns:
        (在售商品列表, 跳过的已售数量)
    """
    raw = page.evaluate(CARD_JS)
    seen: set[str] = set()
    mine: list[dict] = []
    skipped_sold = 0
    for it in raw:
        if it.get("sold"):
            skipped_sold += 1
            continue
        if it.get("seller") != SELLER or not it.get("item_id"):
            continue  # 过滤「为你推荐」的他人商品
        if it["item_id"] in seen:
            continue
        seen.add(it["item_id"])
        if it["img"].startswith("//"):
            it["img"] = "https:" + it["img"]
        # 价格清洗：'¥1.50' / '¥10' 去掉货币符号空格
        it["price_num"] = _num(it["price"])
        it["origin_price_num"] = _num(it["origin_price"])
        mine.append(it)
    return mine, skipped_sold


def _num(s: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", s or "")
    return float(m.group(1)) if m else None


def collect_details(page, items: list[dict]) -> None:
    """逐个进入商品详情页，补采简介全文与图集（原地更新 items）。"""
    for i, it in enumerate(items):
        url = f"https://www.goofish.com/item?id={it['item_id']}"
        try:
            page.goto(url, timeout=30000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(2.5)
            # 描述默认折叠：点 desc 容器内的「展开」按钮再取全文
            try:
                page.evaluate("""() => {
                    const descEl = document.querySelector("[class*='desc']");
                    if (!descEl) return;
                    descEl.querySelectorAll('*').forEach(el => {
                        if (el.children.length === 0
                                && el.innerText && el.innerText.trim() === '展开') {
                            el.click();
                        }
                    });
                }""")
                time.sleep(0.8)
            except Exception:  # noqa: BLE001
                pass
            d = page.evaluate(DETAIL_JS)
            desc = (d.get("desc") or "").strip()
            # 去掉折叠按钮残留的尾行
            desc = re.sub(r"\n*展开\s*$", "", desc).strip()
            it["detail_title"] = d.get("h1") or d.get("title", "")
            it["desc"] = desc
            it["gallery"] = d.get("imgs", [])
            write_progress("running", i + 1, len(items),
                           f"采集中：{it['title'][:24]}")
            print(f"  [{i+1}/{len(items)}] {it['item_id']} "
                  f"desc={len(it['desc'])}字 imgs={len(it['gallery'])}")
        except Exception as exc:  # noqa: BLE001
            it["detail_title"] = it.get("title", "")
            it["desc"] = ""
            it["gallery"] = []
            write_progress("running", i + 1, len(items),
                           f"失败（继续）：{it['item_id']} {exc}")
            print(f"  [{i+1}/{len(items)}] {it['item_id']} FAILED: {exc}")


def collect(allow_details: bool = True) -> int:
    """采集在售商品（核心逻辑，唯一实现）。

    与 publish_item.upload_one() 同样的思路：逻辑放函数里，main() 只解析参数。
    这样采集既能被命令行调用，也能被别的脚本/测试直接调用。

    Args:
        allow_details: True = 逐个进详情页补描述与图集（默认）；False = 只采列表。

    Returns:
        0 成功 / 1 失败（已有任务在跑、找不到页面、没有商品）。
    """
    if not check_single_instance():
        write_progress("error", message="已有采集任务在进行，勿重复启动")
        print("[x] 已有采集任务在进行中")
        return 1

    write_progress("running", 0, 0, "正在连接卖家客户端…")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(CDP)
            page = find_personal_page(browser)
            if page is None:
                write_progress("error", message="找不到 personal 页且无法新开（客户端需以调试端口运行）")
                print("[x] 找不到 personal 页且无法新开（客户端需以调试端口运行）")
                return 1
            # bring_to_front 只是把页面提到前台（避免后台标签被浏览器节流）。
            # 它失败不该判整个采集失败——实测客户端重启/切页时会抛
            # `Target page, context or browser has been closed`，把一次本可以
            # 完成的采集直接打成"采集失败"（2026-10-20 用户报错）。
            try:
                page.bring_to_front()
            except Exception as exc:  # noqa: BLE001
                print(f"[!] 页面置前失败（忽略，继续采集）: {exc}")
                page = find_personal_page(browser) or page
            if "goofish.com/personal" not in page.url:
                page.goto("https://www.goofish.com/personal", timeout=30000)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(4)
            print(f"[*] 主页: {page.title()} | {page.url}")
            write_progress("running", 0, 0, "正在加载商品列表…")

            scroll_load_all(page)
            items, skipped_sold = collect_list(page)
            print(f"[*] 采集到 {len(items)} 个在售商品"
                  + (f"（跳过 {skipped_sold} 个已售）" if skipped_sold else ""))
            write_progress("running", 0, len(items),
                           f"列表就绪：{len(items)} 个在售"
                           + (f"，跳过 {skipped_sold} 个已售" if skipped_sold else ""))
            if not items:
                write_progress("error", message="未采集到任何在售商品")
                return 1

            # 默认就采详情：控制台拉起脚本时没有传 --details（app_launcher.run_collect），
            # 导致采回来的商品 desc/gallery 全是空的——网页上看着"采集完成"，
            # 实际商品没有描述和图集（2026-10-20 用户报"采集失败"）。
            # 改成默认采详情、用 --no-details 显式关闭，这样不用重新打包 exe 也生效。
            if allow_details:
                write_progress("running", 0, len(items), "正在逐个采集商品详情…")
                print("[*] 逐个进详情页采简介 ...")
                collect_details(page, items)

            OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
            OUT_JSON.write_text(
                json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # 采完把页面停回个人主页：下次采集可直接命中，无需再开窗
            try:
                page.goto("https://www.goofish.com/personal", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            write_progress("done", len(items), len(items),
                           f"采集完成，共 {len(items)} 个商品")
            print(f"[OK] 已写入 {OUT_JSON}")
    except Exception as exc:  # noqa: BLE001
        write_progress("error", message=f"采集失败：{exc}")
        print(f"[x] 采集失败: {exc}")
        return 1
    return 0




def main() -> int:
    """命令行入口：只做参数解析。"""
    ap = argparse.ArgumentParser(description="采集闲鱼在售商品（唯一采集实现）")
    ap.add_argument("--details", action="store_true",
                    help="(兼容旧参数；采集详情现在就是默认行为)")
    ap.add_argument("--no-details", action="store_true",
                    help="只采列表，不进每个商品详情页（快，但没有描述与图集）")
    args = ap.parse_args()
    return collect(allow_details=not args.no_details)


if __name__ == "__main__":
    raise SystemExit(main())
