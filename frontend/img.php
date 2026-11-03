<?php
/**
 * 商品封面图输出（本地缓存代理）。
 *
 * 为什么单独成文件、且**不做登录校验**：
 *   1. 商品列表页的 <img> 直接指向这里，如果走 api.php 会被 auth_require_api()
 *      拦成 401（<img> 加载失败 → 又是空白），而且会话过期时整列图会消失；
 *   2. 商品封面是店铺公开信息，不含隐私，没必要鉴权；
 *   3. 独立文件可以放心加 Cache-Control，浏览器缓存后翻页不再重复请求。
 *
 * 用法：img.php?item_id=1081420483948
 *
 * 背景（2026-11-03）：页面原来直接用 img.alicdn.com 的 CDN 链接，部署机器
 * 访问不了阿里 CDN，图片整片空白。改成本地文件后不再依赖外网。
 */

$iid = preg_replace('/[^0-9A-Za-z]/', '', (string)($_GET['item_id'] ?? ''));
if ($iid === '') {
    http_response_code(400);
    header('Content-Type: text/plain; charset=utf-8');
    echo 'missing item_id';
    exit;
}

// 只允许 data/product_images 下的直接子目录，杜绝 ../ 穿越
$base = dirname(__DIR__) . DIRECTORY_SEPARATOR . 'data' . DIRECTORY_SEPARATOR
      . 'product_images' . DIRECTORY_SEPARATOR . $iid;

$mimes = array(
    'webp' => 'image/webp',
    'jpg'  => 'image/jpeg',
    'jpeg' => 'image/jpeg',
    'png'  => 'image/png',
);

foreach (array('cover.webp', 'cover.jpg', 'cover.png') as $fn) {
    $f = $base . DIRECTORY_SEPARATOR . $fn;
    if (!is_file($f)) {
        continue;
    }
    $ext = strtolower(pathinfo($f, PATHINFO_EXTENSION));
    header('Content-Type: ' . ($mimes[$ext] ?? 'application/octet-stream'));
    header('Content-Length: ' . filesize($f));
    header('Cache-Control: public, max-age=604800');   // 7 天
    header('Last-Modified: ' . gmdate('D, d M Y H:i:s', filemtime($f)) . ' GMT');
    readfile($f);
    exit;
}

// 本地还没有这张图：返回 404，前端会自动回退到 CDN 链接
http_response_code(404);
header('Content-Type: text/plain; charset=utf-8');
echo 'no local cover';
