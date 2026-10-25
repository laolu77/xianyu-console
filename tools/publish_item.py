"""把本地商品文件夹（data/publish/<dir>/）提交到闲鱼「发布闲置」页面。

设计要点（2026-10-20）：

1. **闲鱼发布页没有独立「标题」字段**（实测：只有 宝贝图片 / 宝贝视频 /
   宝贝描述 / 商品规格 / 价格·原价·库存）。商品标题由描述内容自动截取，
   所以这里把标题拼在描述最前面，保证搜索结果里能看到卖点。

2. 描述是 **contenteditable 富文本**（class `editor--*`），不是 textarea。
   Playwright 的 `fill()` 对它无效，必须 click 后用 keyboard 输入——
   直接改 innerText 不会触发 React 的 onChange，提交时会判为空。

3. 图片控件是**隐藏的 input[type=file]**，可以直接 set_input_files；
   上传后要等缩略图渲染完，否则接着点发布会提示"请上传图片"。

4. 默认 **dry 模式**：填完所有字段但**不点发布**，只截图留证。真发布必须
   显式传 --yes。这条是安全底线——本脚本连着卖家的真实店铺。

用法：
    python tools/publish_item.py --dir <目录名>            # 只填表，不发布
    python tools/publish_item.py --dir <目录名> --yes      # 真的发布

退出码：0 成功；2 填表失败；3 发布失败；4 参数/环境错误
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from _common import log as _c_log, write_progress as _c_write

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "publish"
PUBLISH_URL = "https://seller.goofish.com/?site=COMMONPRO#/seller-item/publish"

IMG_INPUT = 'input[type=file][accept*="image"]'
DESC_EDITOR = '[contenteditable="true"]'
PRICE_INPUT = 'input[placeholder="0.00"]'
STOCK_INPUT = 'input[placeholder="1"]'


# 日志与进度写盘统一走 tools/_common.py（原来每个脚本各写一份）
def log(msg: str) -> None:
    _c_log(msg, "publish")


def write_progress(path: Path | None, payload: dict) -> None:
    """把进度写给 PHP/前端轮询（失败不影响主流程）。"""
    if path is None:
        return
    _c_write(path, {**payload, "at": time.strftime("%Y-%m-%d %H:%M:%S")})


def find_page(browser):
    """挑一个可用的标签页并返回。

    优先级：登录态卖家工作台 → 其他 goofish 页面 → 任意 http(s) 页面。

    **必须排除 `file://` 页面**：这个客户端里除了卖家工作台，还常驻一个
    Electron 内部页（`file:///...app.asar/...`）。兜底时如果选到它，
    `goto` 到闲鱼发布页会失败或渲染不出表单，表现为
    `set_input_files: Timeout ... waiting for locator(input[type=file])`
    （2026-10-20 实测踩坑：工作台标签恰好停在 #/login，兜底就选中了内部页）。
    """
    pages = [p for p in browser.contexts[0].pages]

    def usable(p) -> bool:
        url = p.url or ""
        return url.startswith("http") and "#/login" not in url

    for page in pages:
        url = page.url or ""
        if "seller.goofish.com" in url and usable(page):
            return page
    for page in pages:
        if "goofish.com" in (page.url or "") and usable(page):
            return page
    for page in pages:
        if usable(page):
            return page
    # 实在没有可用页：宁可返回带登录页的那个，也不返回 file:// 内部页
    for page in pages:
        if (page.url or "").startswith("http"):
            return page
    return None


def wait_images(pg, want: int, timeout: float = 60.0) -> int:
    """等图片真正上传完（缩略图渲染出来）。返回实际识别到的图片数。"""
    deadline = time.time() + timeout
    last = 0
    while time.time() < deadline:
        try:
            n = int(pg.evaluate(COUNT_IMGS_JS) or 0)
        except Exception:  # noqa: BLE001
            n = 0
        last = max(last, n)
        if n >= want:
            return n
        time.sleep(1.0)
    return last


# 统计**已上传到表单的图片**数量。
#
# 不能按 `[class*="upload-item"]` 数：那个类同时用于「添加首图/添加细节图/
# 添加视频」这些**空占位框**，它们内部也有一个图标 <img>，会把 0 张误判成
# 2 张（2026-10-20 踩坑，导致脚本误以为"有残留"而中止）。
# 可靠特征：真实缩略图渲染在顶部图片区，尺寸 ≥80px 且 src 是 http 地址。
COUNT_IMGS_JS = r"""() => {
  let n = 0;
  for (const img of document.querySelectorAll('img')) {
    const r = img.getBoundingClientRect();
    if (r.width < 80 || r.height < 80) continue;   // 排除占位图标 / 头像
    if (r.y > 430) continue;                       // 只看顶部图片区
    const src = img.src || '';
    if (!src || src.indexOf('data:') === 0) continue;
    n++;
  }
  return n;
}"""


def reset_publish_page(pg) -> int:
    """把发布页彻底重置成空表单，返回重置后残留的图片数。

    为什么必须 reload（2026-10-20 重大事故）：
    发布页是 **hash 路由**，`goto` 到同一个 URL 属于同文档导航，SPA **不会重新
    加载**，之前留在表单里的图片（上次测试上传的）会原样保留。于是新图被
    **追加**在旧图后面 —— 用户那笔 5 张图的商品实际发出去 9 张，前 4 张是
    我测试时传的截图。必须 `reload()` 才能拿到干净表单。
    """
    pg.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    pg.reload(wait_until="domcontentloaded", timeout=40000)
    # 等表单真正渲染出来（描述编辑器出现）再判断残留
    for _ in range(30):
        time.sleep(0.5)
        try:
            if pg.evaluate("() => !!document.querySelector('[contenteditable=\"true\"]')"):
                break
        except Exception:  # noqa: BLE001
            pass
    time.sleep(1.5)
    try:
        return int(pg.evaluate(COUNT_IMGS_JS) or 0)
    except Exception:  # noqa: BLE001
        return 0


def fill_desc(pg, text: str) -> None:
    """往 contenteditable 描述框里输入文本（必须走真实键盘，否则 React 收不到）。"""
    ed = pg.locator(DESC_EDITOR).first
    ed.click()
    time.sleep(0.3)
    # 清空已有内容（全选删除）
    pg.keyboard.press("Control+A")
    pg.keyboard.press("Delete")
    time.sleep(0.2)
    # 大段文本用 insert_text 一次性插入（逐字 type 太慢且易被编辑器拦截）
    pg.keyboard.insert_text(text)
    time.sleep(0.5)


def upload_one(dir_name: str, yes: bool = False,
               cdp: str = "http://127.0.0.1:9222",
               progress_path: str = "") -> int:
    """上传**一个**商品（唯一的上传实现）。

    这是整个项目里"把商品发到闲鱼"的唯一入口，调用方有三处，全都走它：
      1. 网页「发布商品」→ api.php `publish_upload` → 本文件 CLI → 这里
      2. 网页「📚 批量上传」→ batch_publish.py 到点后 → 本文件 CLI → 这里
      3. 命令行手动调试 → 本文件 CLI → 这里

    为什么批量也走 CLI 子进程而不是直接 import 本函数：闲鱼发布页是 SPA，
    同进程连着发多个商品会残留上一单的表单状态（2026-10-20 实测：5 张图
    的商品发出去 9 张）。每单一个独立进程 + 每次强制 reload，才不会串。

    Args:
        dir_name: `data/publish/` 下的商品目录名。
        yes: True 才真正点「发布」；False 只填表（dry run）。
        cdp: 浏览器调试端口地址。
        progress_path: 进度 JSON 输出路径（空=不写）。

    Returns:
        0 成功 / 2 表单或上传异常 / 3 校验拦截 / 4 前置条件不满足。
    """
    # 兼容下面沿用 args.xxx 的老写法，避免动 200 行核心逻辑
    args = argparse.Namespace(dir=dir_name, yes=yes, cdp=cdp, progress=progress_path)

    base = DATA / args.dir
    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        log(f"未找到 manifest：{manifest_path}")
        return 4
    try:
        mf = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(mf, dict):
            raise ValueError("manifest 不是对象")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        # 历史踩坑：PHP 侧 json_encode 失败时把 manifest 写成了 0 字节，
        # 这里若不捕获就是一大坨 JSONDecodeError 栈，看不出真正原因。
        log(f"manifest.json 损坏或为空：{exc}")
        write_progress(Path(args.progress) if args.progress else None,
                       {"state": "error", "step": "读取商品信息",
                        "message": "manifest.json 损坏或为空，请重新保存该商品"})
        return 4
    imgs = [str(base / n) for n in mf.get("images", [])]
    if not imgs:
        log("商品没有图片，闲鱼不允许发布")
        return 4

    progress = Path(args.progress) if args.progress else None
    write_progress(progress, {"state": "running", "step": "连接浏览器"})

    title = (mf.get("title") or "").strip()
    desc = (mf.get("desc") or "").strip()
    # 闲鱼没有标题字段 → 标题并进描述首行
    full_desc = f"{title}\n\n{desc}" if desc and title not in desc[:len(title) + 2] else (desc or title)
    full_desc = full_desc[:1500]

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(args.cdp)
        except Exception as exc:  # noqa: BLE001
            log(f"连接 CDP 失败：{exc}")
            write_progress(progress, {"state": "error", "step": "连接浏览器", "message": str(exc)})
            return 4

        pg = find_page(browser)
        if pg is None:
            log("没找到登录态的卖家工作台标签页")
            write_progress(progress, {"state": "error", "step": "找页面",
                                      "message": "未找到登录态工作台标签页"})
            return 4

        write_progress(progress, {"state": "running", "step": "重置发布页"})
        leftover = reset_publish_page(pg)
        if leftover:
            log(f"首次重置后仍有 {leftover} 张残留图，再刷一次")
            leftover = reset_publish_page(pg)
        if leftover:
            log(f"发布页无法清空（残留 {leftover} 张图），为避免发错图已中止")
            write_progress(progress, {"state": "error", "step": "重置发布页",
                                      "message": f"发布页有 {leftover} 张残留图片，无法清空"})
            return 2
        log("发布页已重置为空表单")

        # ---- 1) 上传图片 ----
        write_progress(progress, {"state": "running", "step": f"上传图片 0/{len(imgs)}"})
        try:
            pg.set_input_files(IMG_INPUT, imgs)
        except Exception as exc:  # noqa: BLE001
            log(f"选择图片失败：{exc}")
            write_progress(progress, {"state": "error", "step": "上传图片", "message": str(exc)})
            return 2
        got = wait_images(pg, len(imgs), timeout=max(20.0, len(imgs) * 6.0))
        log(f"图片已提交 {len(imgs)} 张（页面识别到 {got} 个缩略图）")
        write_progress(progress, {"state": "running", "step": f"图片已提交 {len(imgs)} 张"})
        if got == 0:
            write_progress(progress, {"state": "error", "step": "上传图片",
                                      "message": "图片没有上传成功（可能格式/尺寸不被接受）"})
            return 2
        # **数量校验**：页面上必须正好是本次的图片数。
        # 多出来 = 有历史残留（会把无关图一起发出去，2026-10-20 事故）；
        # 少 = 有图没传成功。两种情况都不许继续。
        if got != len(imgs):
            log(f"图片数量不符：期望 {len(imgs)}，页面 {got}，中止发布避免发错图")
            write_progress(progress, {"state": "error", "step": "校验图片",
                                      "message": f"图片数量不符（期望 {len(imgs)}，实际 {got}）"})
            return 2

        # 图片上传后闲鱼会异步做"智能识别属性"，稍等一下再填表，避免表单重渲染冲掉输入
        time.sleep(3)

        # ---- 2) 描述（含标题）----
        write_progress(progress, {"state": "running", "step": "填写描述"})
        try:
            fill_desc(pg, full_desc)
        except Exception as exc:  # noqa: BLE001
            log(f"填写描述失败：{exc}")
            write_progress(progress, {"state": "error", "step": "填写描述", "message": str(exc)})
            return 2

        # ---- 3) 价格 / 原价 / 库存 ----
        # 原价铁律（2026-10-20 用户明确）：**没填就一个字节都不动**。
        # 不点、不聚焦、不补 0、不写占位——闲鱼发布页原价本来就是选填，
        # 留空即视为"不设置原价"。填 0 反而会触发校验/被当成划线价 0 元。
        write_progress(progress, {"state": "running", "step": "填写价格与库存"})
        try:
            price = str(mf.get("price") or "").strip()
            origin_raw = str(mf.get("origin_price") or "").strip()
            # "0" / "0.00" 等同没填（0 元原价无意义，且闲鱼会拦）
            try:
                origin = "" if not origin_raw or float(origin_raw) <= 0 else origin_raw
            except ValueError:
                origin = ""            # 非数字也当作没填，别把脏值塞进输入框
            stock = str(mf.get("stock") or "1").strip()
            boxes = pg.locator(PRICE_INPUT)
            if price and boxes.count() >= 1:
                boxes.nth(0).fill(price)
            if origin and boxes.count() >= 2:
                boxes.nth(1).fill(origin)      # 只有真的有值才写
            else:
                log("原价未填写，保持闲鱼默认（不动该选项）")
            if stock:
                pg.locator(STOCK_INPUT).first.fill(stock)
        except Exception as exc:  # noqa: BLE001
            log(f"填写价格失败：{exc}")
            write_progress(progress, {"state": "error", "step": "填写价格", "message": str(exc)})
            return 2

        time.sleep(1)
        shot = base / "_form_filled.png"
        try:
            # 先滚到顶部再整页截图：只截视口会漏掉图片区和价格区，
            # 留证要看的是"整张表单填成了什么样"
            pg.evaluate("() => window.scrollTo(0, 0)")
            time.sleep(0.6)
            pg.screenshot(path=str(shot), full_page=True, timeout=15000)
        except Exception as exc:  # noqa: BLE001
            log(f"截图失败（不影响流程）：{exc}")
        log(f"表单已填好，截图：{shot.name}")

        if not args.yes:
            log("dry 模式：不点击发布。确认表单无误后加 --yes 才会真正发布")
            write_progress(progress, {"state": "dry_done", "step": "表单已填好（未发布）"})
            return 0

        # ---- 4) 点发布 ----
        write_progress(progress, {"state": "running", "step": "提交发布"})
        before_url = pg.url
        try:
            pg.get_by_role("button", name="发布", exact=True).first.click(timeout=8000)
        except Exception as exc:  # noqa: BLE001
            log(f"点发布失败：{exc}")
            write_progress(progress, {"state": "error", "step": "提交发布", "message": str(exc)})
            return 3

        # ---- 5) 等结果 ----
        # 成功特征：**路由跳到 `publish/success?itemId=xxx`**（实测成功页确实
        # 还带着 "publish" 字样，早期用「url 不含 publish」判断会把成功误判成
        # 失败 —— 2026-10-20 首次真发就踩了这个坑，商品已上架却报失败）。
        # 失败特征：出现 .ant-form-item-explain-error 校验红字。
        js = """() => {
          const errs = [...document.querySelectorAll('.ant-form-item-explain-error')]
            .map(e => (e.innerText || '').trim()).filter(Boolean);
          const toast = [...document.querySelectorAll('.ant-message-notice-content, [class*="toast"]')]
            .map(e => (e.innerText || '').trim()).filter(t => t && t.length < 40)[0] || '';
          return JSON.stringify({url: location.hash || '', errs: errs, toast: toast});
        }"""
        ok, msg, item_id = False, "", ""
        for _ in range(60):          # 30s
            time.sleep(0.5)
            try:
                st = json.loads(pg.evaluate(js))
            except Exception:  # noqa: BLE001
                continue
            u = st.get("url") or ""
            if st.get("errs"):
                msg = "；".join(st["errs"][:3])
                break
            if "/success" in u or "itemId=" in u:
                ok = True
                import re as _re
                m = _re.search(r"itemId=(\d+)", u)
                item_id = m.group(1) if m else ""
                msg = (st.get("toast") or "发布成功") + (f"（商品ID {item_id}）" if item_id else "")
                break
            if "成功" in (st.get("toast") or ""):
                ok = True
                msg = st["toast"]
                break

        if ok:
            log(f"发布成功：{msg}")
            write_progress(progress, {"state": "done", "step": "发布成功",
                                      "message": msg, "item_id": item_id})
            return 0
        log(f"发布失败：{msg or '未检测到成功标志（可能被风控或校验拦截）'}")
        try:
            pg.screenshot(path=str(base / "_publish_failed.png"), timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        write_progress(progress, {"state": "error", "step": "提交发布",
                                  "message": msg or "未检测到成功标志"})
        return 3


def main() -> int:
    """命令行入口：只做参数解析，逻辑全在 upload_one()。"""
    ap = argparse.ArgumentParser(
        description="把 data/publish/<dir> 的商品发布到闲鱼（全项目唯一上传实现）")
    ap.add_argument("--dir", required=True, help="data/publish 下的商品目录名")
    ap.add_argument("--yes", action="store_true", help="真正点击「发布」（默认只填表）")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--progress", default="", help="进度 JSON 输出路径")
    args = ap.parse_args()
    return upload_one(args.dir, yes=args.yes, cdp=args.cdp,
                      progress_path=args.progress)


if __name__ == "__main__":
    raise SystemExit(main())
