<?php
/**
 * 数据库连接与通用读写 helper。
 * 数据库：xianyu_autoreply（127.0.0.1:3306，root / <安装时填的密码>）
 * 首次访问会自动按同目录 init.sql 建表并写入种子数据。
 */

// 安全网：任何 PHP 警告/错误都不要输出到响应体，避免破坏 JSON 格式
ini_set('display_errors', '0');
error_reporting(E_ALL);

/**
 * 数据库配置：优先读 data/db_config.json（网页「设置 → 数据库连接」写的就是它）。
 *
 * 为什么同时存 local 和 remote 两套、只用 mode 切换：
 * 万一远程库连不上（断网/服务器挂了），把 mode 改回 local 就能立刻恢复。
 * 如果只存当前生效的那一套，用户把配置写成远程又连不上，网页就彻底打不开了
 * ——等于把自己锁在门外。
 *
 * @return array{mode:string, local:array, remote:array}
 */
function xy_db_config(): array {
    static $cache = null;
    if ($cache !== null) return $cache;

    $defaults = [
        'mode'   => 'local',
        'local'  => ['host' => '127.0.0.1', 'port' => 3306, 'user' => 'root',
                     'pass' => '', 'db' => 'xianyu_autoreply'],
        'remote' => ['host' => '', 'port' => 3306, 'user' => '',
                     'pass' => '', 'db' => ''],
    ];
    $f = __DIR__ . '/../data/db_config.json';
    if (!is_file($f)) return $cache = $defaults;

    $d = json_decode((string)@file_get_contents($f), true);
    if (!is_array($d)) return $cache = $defaults;

    // 逐字段合并，文件里缺什么就用默认值补
    foreach (['local', 'remote'] as $k) {
        if (isset($d[$k]) && is_array($d[$k])) {
            $defaults[$k] = array_merge($defaults[$k], array_filter(
                $d[$k], function ($v) { return $v !== null && $v !== ''; }));
            $defaults[$k]['port'] = (int)$defaults[$k]['port'];
        }
    }
    $defaults['mode'] = (($d['mode'] ?? 'local') === 'remote') ? 'remote' : 'local';
    return $cache = $defaults;
}

/** 当前生效的那套连接参数。 */
function xy_db_active(): array {
    $c = xy_db_config();
    return $c[$c['mode']] ?? $c['local'];
}

/** 是否配置成远程库（用于决定要不要回退）。 */
function xy_db_is_remote(): bool {
    return xy_db_config()['mode'] === 'remote';
}

$__xy_cfg = xy_db_active();
define('XY_HOST', (string)$__xy_cfg['host']);
define('XY_PORT', (int)$__xy_cfg['port']);
define('XY_USER', (string)$__xy_cfg['user']);
define('XY_PASS', (string)$__xy_cfg['pass']);
define('XY_DB',   (string)$__xy_cfg['db']);
unset($__xy_cfg);

/** 快速探测 MySQL 端口是否可达（1.5s 超时，避免阻塞单线程服务器） */
function xy_db_reachable($host = null, $port = null) {
    $host = $host ?? XY_HOST;
    $port = $port ?? XY_PORT;
    $fp = @fsockopen($host, (int)$port, $errno, $errstr, 1.0);
    if ($fp) { @fclose($fp); return true; }
    return false;
}

/**
 * 按给定参数建立连接（内部用）。
 *
 * @return mysqli|false
 */
function xy_connect_with(array $cfg, $dbname = null) {
    if (!xy_db_reachable($cfg['host'], $cfg['port'])) return false;
    $link = mysqli_init();
    if (!$link) return false;
    // 关键：MySQL 未启动/不可达时快速失败（3 秒），避免阻塞单线程服务器
    mysqli_options($link, MYSQLI_OPT_CONNECT_TIMEOUT, 3);
    mysqli_options($link, MYSQLI_OPT_READ_TIMEOUT, 5);
    $ok = @mysqli_real_connect($link, $cfg['host'], $cfg['user'], $cfg['pass'],
                               $dbname ?? $cfg['db'], (int)$cfg['port']);
    if (!$ok) { @mysqli_close($link); return false; }
    mysqli_set_charset($link, 'utf8mb4');
    return $link;
}

/**
 * 远程库是否刚失败过（30 秒内）。
 *
 * 为什么需要：远程不可达时，每个 API 请求都要先等 fsockopen 超时再回退，
 * 一个页面十几个请求就是几十秒的白屏（实测单请求 6 秒）。
 * 记一个时间戳，失败后 30 秒内直接走本地，只在过期后再探一次远程。
 *
 * @return bool
 */
function xy_db_remote_down(): bool {
    $f = __DIR__ . '/../data/.db_remote_down';
    if (!is_file($f)) return false;
    return (time() - (int)@file_get_contents($f)) < 30;
}

function xy_db_mark_remote_down(bool $down): void {
    $f = __DIR__ . '/../data/.db_remote_down';
    if ($down) { @file_put_contents($f, (string)time()); }
    else { @unlink($f); }
}

function xy_raw_connect($dbname = null) {
    $c = xy_db_config();

    // 远程刚失败过：不再试探，直接用本地（否则整页卡死）
    if ($c['mode'] === 'remote' && xy_db_remote_down()) {
        $link = xy_connect_with($c['local']);
        if ($link) { $GLOBALS['xy_db_fallback'] = true; }
        return $link;
    }

    $link = xy_connect_with($c[$c['mode']], $dbname);
    if ($link) {
        if ($c['mode'] === 'remote') xy_db_mark_remote_down(false);  // 恢复了，清标记
        return $link;
    }

    // 远程库连不上时自动回退本地 —— 否则用户改错地址就再也进不了网页改回来。
    // 前端靠 xy_db_fallback() 判断要不要显示"已回退本地"的警告。
    // 注意：回退必须用**本地配置的库名**（远程库名通常不一样，拿来连本地会失败）。
    if ($c['mode'] === 'remote') {
        xy_db_mark_remote_down(true);
        $link = xy_connect_with($c['local']);
        if ($link) { $GLOBALS['xy_db_fallback'] = true; }
    }
    return $link;
}

/** 本次请求是否发生了"远程失败 → 回退本地"。 */
function xy_db_fallback(): bool {
    return !empty($GLOBALS['xy_db_fallback']);
}


function xy_ensure_installed() {
    $c = xy_raw_connect(XY_DB);
    if ($c) {
        $r = @mysqli_query($c, "SHOW TABLES LIKE 'settings'");
        if ($r && mysqli_num_rows($r) > 0) { mysqli_close($c); return true; }
        mysqli_close($c);
    }
    // 建库建表（逐条执行：单条失败不影响其余，且能暴露具体错误）
    $c = xy_raw_connect(null);
    if (!$c) return false;
    mysqli_set_charset($c, 'utf8mb4');
    $sql = @file_get_contents(__DIR__ . '/init.sql');
    if ($sql === false) { mysqli_close($c); return false; }
    $stmts = preg_split('/;\s*/', $sql);
    $ok = true;
    foreach ($stmts as $raw) {
        $s = trim(preg_replace('/--[^\n]*/', '', $raw));   // 去 -- 行注释
        $s = trim(preg_replace('/\/\*.*?\*\//s', '', $s));  // 去 /* */ 块注释
        if ($s === '') continue;
        if (!@mysqli_query($c, $s)) {
            $ok = false;
            error_log('[xianyu] init.sql 执行失败: ' . mysqli_error($c) . ' | ' . mb_substr($s, 0, 120));
        }
    }
    mysqli_close($c);
    return $ok;
}

$_xy_conn = null;

/** 2026-10-17 v2 新表：幂等补建（现有库不会重跑 init.sql，这里兜底）。
 *  shop_items    店铺商品（采集落库）
 *  card_bundles  卡卷（一组卡密）
 *  item_bundles  商品-卡卷绑定（多对多）
 */
function xy_ensure_v2_tables() {
    static $done = false;
    if ($done) return true;
    $done = true;
    $ddl = [
        "CREATE TABLE IF NOT EXISTS shop_items (
          id            INT          NOT NULL AUTO_INCREMENT,
          item_id       VARCHAR(32)  NOT NULL COMMENT '闲鱼商品ID',
          title         VARCHAR(255) NOT NULL,
          price         VARCHAR(32)  NULL,
          origin_price  VARCHAR(32)  NULL,
          img           VARCHAR(512) NULL,
          descr         MEDIUMTEXT   NULL,
          gallery       JSON         NULL,
          is_physical   TINYINT(1)   NOT NULL DEFAULT 0,
          created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (id),
          UNIQUE KEY uk_item (item_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci",
        "CREATE TABLE IF NOT EXISTS card_bundles (
          id          INT          NOT NULL AUTO_INCREMENT,
          name        VARCHAR(128) NOT NULL,
          content     MEDIUMTEXT   NOT NULL,
          enabled     TINYINT(1)   NOT NULL DEFAULT 1,
          created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci",
        "CREATE TABLE IF NOT EXISTS item_bundles (
          item_id   INT NOT NULL,
          bundle_id INT NOT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (item_id, bundle_id),
          KEY idx_bundle (bundle_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci",
    ];
    $c = xy_raw_connect(XY_DB);
    if (!$c) return false;
    foreach ($ddl as $s) { @mysqli_query($c, $s); }
    mysqli_close($c);
    return true;
}

/** 2026-10-17 字符集迁移：把已存在的库/表从 utf8(3字节) 升级到 utf8mb4(4字节)，
 *  以支持 emoji（📁📌👉 等）。仅执行一次（_xy_meta 标记表），不阻塞后续请求。 */
function xy_ensure_utf8mb4() {
    static $done = false;
    if ($done) return;
    $done = true;
    $c = xy_raw_connect(XY_DB);
    if (!$c) return;
    // 独立标记表，避免依赖 settings 的固定字段结构
    @mysqli_query($c, "CREATE TABLE IF NOT EXISTS _xy_meta (`key` VARCHAR(64) NOT NULL PRIMARY KEY, val VARCHAR(64) NOT NULL DEFAULT '') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci");
    $r = @mysqli_query($c, "SELECT 1 FROM _xy_meta WHERE `key`='charset_v2'");
    if ($r && mysqli_num_rows($r) > 0) { mysqli_close($c); return; }
    @mysqli_query($c, "ALTER DATABASE `" . XY_DB . "` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci");
    $tables = ['settings','rules','conversations','messages','card_products','card_inventory',
               'deliveries','activity_log','commands','engine_status','shop_items',
               'card_bundles','item_bundles','sw_control'];
    foreach ($tables as $t) {
        @mysqli_query($c, "ALTER TABLE `$t` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci");
    }
    @mysqli_query($c, "INSERT INTO _xy_meta (`key`,val) VALUES ('charset_v2','utf8mb4')");
    mysqli_close($c);
}

function xy_conn() {
    global $_xy_conn;
    if ($_xy_conn !== null && @mysqli_ping($_xy_conn)) return $_xy_conn;
    xy_ensure_installed();
    $_xy_conn = xy_raw_connect(XY_DB);
    if (!$_xy_conn) {
        throw new Exception('数据库未连接：MySQL 未启动或不可达（' . XY_HOST . ':' . XY_PORT . '）');
    }
    return $_xy_conn;
}

/** 执行查询，返回 mysqli result */
function xy_query($sql, $params = []) {
    $c = xy_conn();   // 连接失败会抛异常，由 api.php 的 try/catch 转成 JSON 错误
    if (!$c) throw new Exception('数据库未连接');
    $stmt = mysqli_prepare($c, $sql);
    if (!$stmt) throw new Exception('SQL 预处理失败: ' . mysqli_error($c));
    if ($params) {
        $types = str_repeat('s', count($params));
        mysqli_stmt_bind_param($stmt, $types, ...$params);
    }
    if (!mysqli_stmt_execute($stmt)) {
        $err = mysqli_stmt_error($stmt);
        mysqli_stmt_close($stmt);
        throw new Exception('数据库写入失败: ' . $err);
    }
    return $stmt;
}

/** 返回全部行（关联数组） */
function xy_all($sql, $params = []) {
    $stmt = xy_query($sql, $params);
    if (!$stmt) return [];
    $res = mysqli_stmt_get_result($stmt);
    $out = [];
    while ($row = mysqli_fetch_assoc($res)) $out[] = $row;
    mysqli_free_result($res);
    return $out;
}

/** 返回单行 */
function xy_one($sql, $params = []) {
    $rows = xy_all($sql, $params);
    return $rows ? $rows[0] : null;
}

/** 执行写操作，返回受影响行数 */
function xy_exec($sql, $params = []) {
    $stmt = xy_query($sql, $params);
    if (!$stmt) return 0;
    return mysqli_stmt_affected_rows($stmt);
}

/** 取刚插入的 id */
function xy_insert_id() {
    $c = xy_conn();
    return mysqli_insert_id($c);
}

/** JSON 输出并结束 */
function xy_json($data) {
    header('Content-Type: application/json; charset=utf-8');
    // 去掉深层 NaN/资源，保证可序列化
    echo json_encode($data, JSON_UNESCAPED_UNICODE);
    exit;
}

/** 取请求参数（GET/POST 兼容） */
function xy_param($key, $default = '') {
    if (isset($_POST[$key])) return $_POST[$key];
    if (isset($_GET[$key])) return $_GET[$key];
    return $default;
}

/** 把 JSON 字段（数据库里存的是 JSON 字符串）解析为数组 */
function xy_json_field($v) {
    if ($v === null || $v === '') return [];
    if (is_array($v)) return $v;
    $d = json_decode($v, true);
    return is_array($d) ? $d : [];
}
