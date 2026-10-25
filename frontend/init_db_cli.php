<?php
/**
 * 命令行初始化数据库（闲鱼卖家客服网页端）
 *
 * 用法：
 *   php init_db_cli.php          正常初始化（库已存在则跳过，幂等安全）
 *   php init_db_cli.php reset    清空并重建数据库（会丢失卡密/发货/会话等数据）
 *
 * 设计要点：
 *   - 直接复用网页端 db.php 的 xy_ensure_installed()，保证与网页逻辑完全一致；
 *   - 输出统一转 GBK，方便在 Windows 控制台（代码页 936）正确显示中文。
 */

// 输出助手：UTF-8 源 -> GBK 控制台
function out($msg = '') {
    if ($msg === '') { echo "\n"; return; }
    echo mb_convert_encoding($msg, 'GBK', 'UTF-8') . "\n";
}

out("========================================");
out("  闲鱼卖家客服 · 数据库初始化工具");
out("========================================");

$reset = false;
if (isset($argv[1]) && strtolower($argv[1]) === 'reset') {
    $reset = true;
    out("检测到 reset 参数：将清空并重建数据库（卡密/发货记录等会丢失）。");
}

// 载入网页端同一套连接与建库逻辑
require __DIR__ . '/db.php';

if (!xy_db_reachable()) {
    out("✗ 无法连接 MySQL（" . XY_HOST . ":" . XY_PORT . "）。");
    out("  请先在 phpStudy 中启动 MySQL，再运行本脚本。");
    exit(2);
}
out("• MySQL 端口可达：" . XY_HOST . ":" . XY_PORT);

if ($reset) {
    $c = xy_raw_connect(null);
    if ($c) {
        mysqli_query($c, "DROP DATABASE IF EXISTS " . XY_DB);
        out("• 已删除旧库 " . XY_DB . "（如存在）。");
        mysqli_close($c);
    }
}

out("• 正在建库建表并写入种子数据 ...");
$ok = xy_ensure_installed();
if (!$ok) {
    out("✗ 初始化过程中有 SQL 执行失败，请查看 PHP 控制台错误输出 / error_log。");
    exit(1);
}

// 校验结果
$c = xy_raw_connect(XY_DB);
if (!$c) {
    out("✗ 建表后无法连接数据库，请检查权限。");
    exit(1);
}
$tables = [];
$r = mysqli_query($c, "SHOW TABLES");
while ($row = mysqli_fetch_row($r)) $tables[] = $row[0];
$ruleN = (int)mysqli_fetch_row(mysqli_query($c, "SELECT COUNT(*) FROM rules"))[0];
$setN  = (int)mysqli_fetch_row(mysqli_query($c, "SELECT COUNT(*) FROM settings"))[0];
$prodN = (int)mysqli_fetch_row(mysqli_query($c, "SELECT COUNT(*) FROM card_products"))[0];
mysqli_close($c);

out("✓ 初始化完成！");
out("  数据库：" . XY_DB);
out("  数据表：" . count($tables) . " 张");
out("  规则种子：" . $ruleN . " 条");
out("  设置行数：" . $setN . " 行");
out("  卡密商品：" . $prodN . " 个");
out("");
out("现在刷新网页 http://localhost/xianyu/ 即可使用");
out("（若走 8080 自带服务器，则为 http://localhost:8080/xianyu/）");
exit(0);
