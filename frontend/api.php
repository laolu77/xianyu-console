<?php
/**
 * JSON API：网页端所有数据读写都走这里。
 * 所有 action 返回 JSON：{"ok":true, ...} 或 {"ok":false, "msg":"..."}
 */
require_once __DIR__ . '/db.php';
require_once __DIR__ . '/auth.php';

// 所有接口都要求登录：认证必须放在 try 之前，未登录直接 401 退出，
// 不能等走进 switch 才判断（那样部分 action 会先执行副作用）
auth_require_api();

header('Content-Type: application/json; charset=utf-8');

// v2 新表幂等补建（shop_items / card_bundles / item_bundles）
xy_ensure_v2_tables();
// 字符集升级到 utf8mb4（支持 emoji），已迁移则跳过
xy_ensure_utf8mb4();

function ok($data = []) { xy_json(array_merge(['ok' => true], $data)); }
function fail($msg)     { xy_json(['ok' => false, 'msg' => $msg]); }

try {
    $action = xy_param('action', '');

    switch ($action) {

        // ---------- 安装 / 健康检查 ----------
        case 'install':
            xy_ensure_installed();
            ok(['msg' => '数据库已就绪']);

        case 'test_db':
            $c = xy_raw_connect(XY_DB);
            if ($c) { mysqli_close($c); ok(['msg' => 'MySQL 连接正常']); }
            fail('MySQL 连接失败（' . XY_HOST . ':' . XY_PORT . ' 未响应，请确认已在 phpStudy 启动 MySQL）');

        // ---------- 仪表盘 ----------
        case 'dashboard':
            $st = xy_one("SELECT * FROM engine_status WHERE id=1") ?: ['state' => 'stopped', 'pid' => null, 'last_heartbeat' => null, 'last_activity' => null];
            $alive = ($st['state'] === 'running' && $st['last_heartbeat']);
            if ($alive) {
                $ts = strtotime($st['last_heartbeat']);
                $alive = ($ts !== false) && (time() - $ts < 15);
            }
            $st['alive'] = (bool)$alive;
            ok([
                'engine' => $st,
                'counts' => [
                    'rules'      => (int)xy_one("SELECT COUNT(*) c FROM rules WHERE enabled=1")['c'],
                    'products'   => (int)xy_one("SELECT COUNT(*) c FROM card_products")['c'],
                    'cards_left' => (int)xy_one("SELECT COUNT(*) c FROM card_inventory WHERE used=0")['c'],
                    'conv'       => (int)xy_one("SELECT COUNT(*) c FROM conversations")['c'],
                    'deliv_today'=> (int)xy_one("SELECT COUNT(*) c FROM deliveries WHERE DATE(created_at)=CURDATE()")['c'],
                    'msg_today'  => (int)xy_one("SELECT COUNT(*) c FROM messages WHERE DATE(created_at)=CURDATE()")['c'],
                    'shop_items' => (int)xy_one("SELECT COUNT(*) c FROM shop_items")['c'],
                    'bundles'    => (int)xy_one("SELECT COUNT(*) c FROM card_bundles")['c'],
                ],
                'activity'  => xy_all("SELECT * FROM activity_log ORDER BY id DESC LIMIT 15"),
                'deliveries'=> xy_all("SELECT * FROM deliveries ORDER BY id DESC LIMIT 8"),
            ]);

        // ---------- 回复规则 ----------
        case 'rules':
            $rows = xy_all("SELECT * FROM rules ORDER BY priority DESC, id ASC");
            foreach ($rows as &$r) $r['keywords'] = xy_json_field($r['keywords']);
            ok(['rows' => $rows]);

        case 'rule_add':
            $kws = xy_param('keywords', '');
            $reply = trim(xy_param('reply', ''));
            $priority = (int)xy_param('priority', 0);
            $enabled = xy_param('enabled', '1') === '1' ? 1 : 0;
            if ($reply === '') fail('回复内容不能为空');
            $kws = xy_parse_keywords($kws);
            if (empty($kws)) fail('至少填写一个关键词');
            xy_exec("INSERT INTO rules (keywords, reply, priority, enabled) VALUES (?,?,?,?)",
                    [json_encode($kws, JSON_UNESCAPED_UNICODE), $reply, $priority, $enabled]);
            ok(['id' => xy_insert_id()]);

        case 'rule_update':
            $id = (int)xy_param('id', 0);
            $sets = []; $args = [];
            if (($v = xy_param('reply', null)) !== null) { $sets[] = 'reply=?'; $args[] = $v; }
            if (($v = xy_param('priority', null)) !== null) { $sets[] = 'priority=?'; $args[] = (int)$v; }
            if (($v = xy_param('enabled', null)) !== null) { $sets[] = 'enabled=?'; $args[] = $v==='1'?1:0; }
            if (($v = xy_param('keywords', null)) !== null) { $sets[] = 'keywords=?'; $args[] = json_encode(xy_parse_keywords($v), JSON_UNESCAPED_UNICODE); }
            if ($sets) { $args[] = $id; xy_exec("UPDATE rules SET ".implode(',', $sets)." WHERE id=?", $args); }
            ok([]);

        case 'rule_delete':
            xy_exec("DELETE FROM rules WHERE id=?", [(int)xy_param('id', 0)]);
            ok([]);

        case 'rule_toggle':
            $id = (int)xy_param('id', 0);
            $on = xy_param('enabled', '1') === '1' ? 1 : 0;
            xy_exec("UPDATE rules SET enabled=? WHERE id=?", [$on, $id]);
            ok([]);

        // ---------- 卡密商品 ----------
        case 'products':
            $rows = xy_all("SELECT p.*, (SELECT COUNT(*) FROM card_inventory ci WHERE ci.product_id=p.id) AS total,
                                   (SELECT SUM(used) FROM card_inventory ci WHERE ci.product_id=p.id) AS used_cnt
                            FROM card_products p ORDER BY p.id ASC");
            foreach ($rows as &$r) {
                $r['keywords'] = xy_json_field($r['keywords']);
                $r['used_cnt'] = (int)$r['used_cnt'];
                $r['total'] = (int)$r['total'];
                $r['remain'] = $r['total'] - $r['used_cnt'];
            }
            ok(['rows' => $rows]);

        case 'product_add':
            $name = trim(xy_param('name', ''));
            $kws = xy_parse_keywords(xy_param('keywords', ''));
            if ($name === '') fail('商品名称不能为空');
            if (empty($kws)) fail('至少填写一个匹配关键词');
            xy_exec("INSERT INTO card_products (product_name, keywords) VALUES (?,?)",
                    [$name, json_encode($kws, JSON_UNESCAPED_UNICODE)]);
            ok(['id' => xy_insert_id()]);

        case 'product_delete':
            $id = (int)xy_param('id', 0);
            xy_exec("DELETE FROM card_inventory WHERE product_id=?", [$id]);
            xy_exec("DELETE FROM card_products WHERE id=?", [$id]);
            ok([]);

        case 'codes_add':
            $pid = (int)xy_param('product_id', 0);
            $text = xy_param('codes', '');
            $lines = preg_split('/\r|\n/', $text);
            $codes = array_filter(array_map('trim', $lines), function($x){ return $x !== ''; });
            if ($pid <= 0) fail('商品无效');
            if (empty($codes)) fail('没有可导入的卡密');
            $n = 0;
            foreach ($codes as $c) {
                xy_exec("INSERT INTO card_inventory (product_id, code) VALUES (?,?)", [$pid, $c]);
                $n++;
            }
            ok(['imported' => $n]);

        case 'inventory':
            $pid = (int)xy_param('product_id', 0);
            $rows = xy_all("SELECT * FROM card_inventory WHERE product_id=? ORDER BY id DESC", [$pid]);
            ok(['rows' => $rows]);

        // ---------- 店铺商品（采集/导入/绑定卡卷，2026-10-17 新增） ----------
        case 'product_image':
            // 商品缩略图：优先返回**本地缓存**（data/product_images/<item_id>/cover.*）。
            // 为什么要有它：商品列表页原来直接用 img.alicdn.com 的 CDN 链接，
            // 部署机器网络访问不了 / 加载慢时就是一片空白（2026-11-03 用户截图）。
            // data/ 在 web 根目录之外，浏览器访问不到，所以用这个端点代理输出。
            $iid = preg_replace('/[^0-9A-Za-z]/', '', (string)($_GET['item_id'] ?? ''));
            if ($iid === '') {
                http_response_code(400);
                exit;
            }
            $base = dirname(__DIR__) . '/data/product_images/' . $iid;
            foreach (array('cover.webp', 'cover.jpg', 'cover.png') as $fn) {
                $f = $base . '/' . $fn;
                if (!is_file($f)) continue;
                $ext = strtolower(pathinfo($f, PATHINFO_EXTENSION));
                $mimes = array('webp' => 'image/webp', 'jpg' => 'image/jpeg',
                               'jpeg' => 'image/jpeg', 'png' => 'image/png');
                header('Content-Type: ' . ($mimes[$ext] ?? 'application/octet-stream'));
                header('Cache-Control: public, max-age=86400');
                header('Content-Length: ' . filesize($f));
                readfile($f);
                exit;
            }
            http_response_code(404);
            exit;

        case 'shop_items':
            $rows = xy_all("SELECT * FROM shop_items ORDER BY id DESC");
            foreach ($rows as &$r) {
                $r['gallery'] = xy_json_field($r['gallery']);
                $bs = xy_all("SELECT b.id, b.name FROM item_bundles ib
                              JOIN card_bundles b ON b.id=ib.bundle_id
                              WHERE ib.item_id=? ORDER BY b.id", [$r['id']]);
                $r['bundles'] = $bs;
                $r['bundle_ids'] = array_map(function($b){ return (int)$b['id']; }, $bs);
            }
            unset($r);
            ok(['rows' => $rows]);

        case 'product_collect':
            // 写入采集触发器（时间戳）；桌面软件监听后拉起采集脚本，
            // 进度由采集器写 data/collect_progress.json，前端轮询渲染进度条。
            if (xy_collect_running()) fail('采集正在进行中，请稍候');
            xy_set_control('collect', (string)time());
            ok(['msg' => '采集已启动']);

        case 'product_collect_status':
            $f = xy_data_dir() . '/collect_progress.json';
            if (!is_file($f)) {
                ok(['status' => 'idle', 'current' => 0, 'total' => 0, 'message' => '尚未采集过']);
            }
            $d = json_decode((string)file_get_contents($f), true);
            if (!is_array($d)) {
                ok(['status' => 'idle', 'current' => 0, 'total' => 0, 'message' => '进度文件损坏']);
            }
            // 采集一完成就在这里顺手入库，前端无需再额外发一次导入请求，
            // 用户只看到"采集完成"，中间的导入对它不可见（2026-10-19 用户要求）。
            // 幂等由 xy_import_collected_products 的文件戳保证，重复轮询不会重复导。
            if (($d['status'] ?? '') === 'done') {
                $n = xy_import_collected_products();
                if ($n > 0) $d['imported'] = $n;
            }
            ok($d);

        case 'product_import':
            // 兼容入口：正常流程已由 product_collect_status 自动导入，
            // 这里保留给旧页面/手工补导（$force=true 忽略已导入标记）。
            $f = xy_data_dir() . '/collected_products.json';
            if (!is_file($f)) fail('未找到采集文件（请先点「获取商品」）');
            $items = json_decode((string)file_get_contents($f), true);
            if (!is_array($items) || empty($items)) fail('采集文件为空或不是合法 JSON');
            $n = xy_import_collected_products(true);
            ok(['imported' => $n]);

        case 'shop_item_delete':
            $id = (int)xy_param('id', 0);
            xy_exec("DELETE FROM item_bundles WHERE item_id=?", [$id]);
            xy_exec("DELETE FROM shop_items WHERE id=?", [$id]);
            ok([]);

        case 'item_bind_bundles':
            // 商品绑定一个或多个卡卷（整体替换式保存）
            $id = (int)xy_param('id', 0);
            $item = xy_one("SELECT id FROM shop_items WHERE id=?", [$id]);
            if (!$item) fail('商品不存在');
            $ids = array_values(array_filter(
                array_map('intval', preg_split('/[,，\s]+/', (string)xy_param('bundle_ids', ''))),
                function($x){ return $x > 0; }
            ));
            xy_exec("DELETE FROM item_bundles WHERE item_id=?", [$id]);
            foreach ($ids as $b) {
                xy_exec("INSERT IGNORE INTO item_bundles (item_id, bundle_id) VALUES (?,?)", [$id, $b]);
            }
            ok(['bound' => count($ids)]);

        // ---------- 卡卷管理（2026-10-17 新增） ----------
        case 'bundles':
            $rows = xy_all("SELECT b.*, (SELECT COUNT(*) FROM item_bundles ib WHERE ib.bundle_id=b.id) AS item_cnt
                            FROM card_bundles b ORDER BY b.id DESC");
            foreach ($rows as &$r) $r['item_cnt'] = (int)$r['item_cnt'];
            unset($r);
            ok(['rows' => $rows]);

        case 'bundle_add':
            $name = trim(xy_param('name', ''));
            $content = xy_param('content', '');
            if ($name === '') fail('卡卷名称不能为空');
            if (trim($content) === '') fail('卡卷内容不能为空');
            xy_exec("INSERT INTO card_bundles (name, content) VALUES (?,?)", [$name, $content]);
            ok(['id' => xy_insert_id()]);

        case 'bundle_update':
            $id = (int)xy_param('id', 0);
            $sets = []; $args = [];
            if (($v = xy_param('name', null)) !== null && trim($v) !== '') { $sets[] = 'name=?'; $args[] = trim($v); }
            if (($v = xy_param('content', null)) !== null && trim($v) !== '') { $sets[] = 'content=?'; $args[] = $v; }
            if (($v = xy_param('enabled', null)) !== null) { $sets[] = 'enabled=?'; $args[] = $v === '1' ? 1 : 0; }
            if ($sets) { $args[] = $id; xy_exec("UPDATE card_bundles SET " . implode(',', $sets) . " WHERE id=?", $args); }
            ok([]);

        case 'bundle_toggle':
            // 状态列点击切换启用/停用
            $id = (int)xy_param('id', 0);
            $row = xy_one("SELECT enabled FROM card_bundles WHERE id=?", [$id]);
            if (!$row) fail('卡卷不存在');
            $new = $row['enabled'] ? 0 : 1;
            xy_exec("UPDATE card_bundles SET enabled=? WHERE id=?", [$new, $id]);
            ok(['enabled' => $new]);

        case 'bundle_import':
            // 表格形式批量导入：items = JSON [{"name":"..","content":".."}, ...]
            // （前端把粘贴的 TSV 解析成两列后整批提交）
            $raw = xy_param('items', '');
            $items = json_decode($raw, true);
            if (!is_array($items) || empty($items)) fail('没有可导入的数据（请先粘贴解析）');
            $n = 0;
            foreach ($items as $it) {
                if (!is_array($it)) continue;
                $name = trim((string)($it['name'] ?? ''));
                $content = (string)($it['content'] ?? '');
                if ($name === '' || trim($content) === '') continue;
                xy_exec("INSERT INTO card_bundles (name, content) VALUES (?,?)", [$name, $content]);
                $n++;
            }
            if ($n === 0) fail('所有行都缺少名称或内容，未导入任何数据');
            xy_log_activity('IMPORT', "导入卡卷 {$n} 条");
            ok(['imported' => $n]);

        case 'bundle_export':
            // 导出卡卷：ids 逗号分隔（空 = 全部）。前端拼 CSV 下载。
            $ids = array_values(array_filter(
                array_map('intval', preg_split('/[,，\s]+/', (string)xy_param('ids', ''))),
                function($x){ return $x > 0; }
            ));
            if ($ids) {
                $in = implode(',', array_fill(0, count($ids), '?'));
                $rows = xy_all("SELECT id, name, content FROM card_bundles WHERE id IN ($in) ORDER BY id DESC", $ids);
            } else {
                $rows = xy_all("SELECT id, name, content FROM card_bundles ORDER BY id DESC");
            }
            ok(['rows' => $rows]);

        case 'bundle_delete':
            $id = (int)xy_param('id', 0);
            xy_exec("DELETE FROM item_bundles WHERE bundle_id=?", [$id]);
            xy_exec("DELETE FROM card_bundles WHERE id=?", [$id]);
            ok([]);

        // ---------- 智能匹配卡券（2026-10-20 数据清空后重建绑定用） ----------
        // 只给**建议**，不写库。用户在弹窗里勾选后才调 bulk_bind。
        // 为什么不敢自动绑：绑错卡卷 = 买家付钱收到无关内容，比不发货更糟。
        case 'match_bundles':
            $items = xy_all("SELECT id,title,descr FROM shop_items ORDER BY id");
            $bundles = xy_all("SELECT id,name FROM card_bundles ORDER BY id");
            $bound = [];
            foreach (xy_all("SELECT item_id,bundle_id FROM item_bundles") as $r) {
                $bound[(int)$r['item_id']][] = (int)$r['bundle_id'];
            }
            // 归一化：去标点/空格/大小写，中文保留
            $norm = function ($s) {
                $s = mb_strtolower((string)$s, 'UTF-8');
                return preg_replace('/[^\x{4e00}-\x{9fa5}a-z0-9]+/u', '', $s);
            };
            // 通用插件名不算区分度（几乎每个商品都提 ItemsAdder/Oraxen）
            $generic = ['itemsadder', 'oraxen', 'nexo', 'dropb', 'modelengine', 'mythicmobs'];
            $sugg = [];
            foreach ($items as $it) {
                $nt = $norm($it['title']);
                $nd = $norm($it['descr']);
                $best = null;
                foreach ($bundles as $b) {
                    $nb = $norm($b['name']);
                    if ($nb === '' || mb_strlen($nb) < 2) continue;
                    $score = 0; $reason = '';
                    if (in_array($nb, $generic, true)) {
                        // 通用插件名：只有商品标题里也单独出现该词才弱命中
                        if ($nt !== '' && strpos($nt, $nb) !== false) {
                            $score = 40; $reason = '标题含插件名';
                        }
                    } elseif ($nt !== '' && strpos($nt, $nb) !== false) {
                        $score = 100; $reason = '标题完全包含卡券名';
                    } elseif ($nd !== '' && strpos($nd, $nb) !== false) {
                        $score = 60; $reason = '描述中提到';
                    } elseif ($nb !== '' && $nt !== '' && mb_strlen($nt) >= 4
                              && strpos($nb, $nt) !== false) {
                        $score = 70; $reason = '卡券名包含商品名';
                    }
                    if ($score <= 0) continue;
                    // 同名多条时优先短名（更精确）
                    $rank = [$score, -mb_strlen($nb)];
                    if ($best === null || $rank > $best['rank']) {
                        $best = ['rank' => $rank, 'bundle_id' => (int)$b['id'],
                                 'bundle_name' => $b['name'], 'score' => $score,
                                 'reason' => $reason];
                    }
                }
                if ($best === null) continue;
                $sugg[] = [
                    'item_id'     => (int)$it['id'],
                    'item_title'  => $it['title'],
                    'bundle_id'   => $best['bundle_id'],
                    'bundle_name' => $best['bundle_name'],
                    'score'       => $best['score'],
                    'reason'      => $best['reason'],
                    'already'     => in_array($best['bundle_id'], $bound[(int)$it['id']] ?? [], true),
                ];
            }
            // 高分在前
            usort($sugg, function ($a, $b) { return $b['score'] <=> $a['score']; });
            ok(['rows' => $sugg, 'items' => count($items), 'bundles' => count($bundles)]);

        // ---------- 批量上传商品（文件夹模式，2026-10-20 用户要求） ----------
        // 约定：用户在工作目录下，一个商品一个子文件夹，里面放表格（xlsx/csv）+ 图片。
        // 网页选目录 → 扫描预览 → 一键逐个上传。
        // 上传是**异步**的（每个商品 40-60s，24 个要十几分钟），前端轮询 batch_status。
        case 'batch_scan':
            $path = trim((string)xy_param('path', ''));
            if ($path === '') fail('请先填写或选择工作目录');
            $py = dirname(__DIR__) . '/runtime/python/Scripts/python.exe';
            $script = dirname(__DIR__) . '/tools/batch_publish.py';
            if (!is_file($script)) fail('缺少 tools/batch_publish.py');
            putenv('PYTHONIOENCODING=utf-8');
            putenv('PYTHONUTF8=1');
            $cmd = escapeshellarg($py) . ' ' . escapeshellarg($script)
                 . ' --scan ' . escapeshellarg($path);
            $out = [];
            $rc = 0;
            @exec($cmd . ' 2>&1', $out, $rc);
            $raw = implode("\n", $out);
            $pos = strpos($raw, '{');
            $data = $pos === false ? null : json_decode(substr($raw, $pos), true);
            if (!is_array($data)) {
                fail('扫描失败：' . xy_safe_text(implode(' | ', array_slice($out, -3)), 160));
            }
            $data['rows'] = array_map(function ($r) {
                foreach (['title', 'price', 'error', 'table'] as $k) {
                    if (isset($r[$k])) $r[$k] = xy_safe_text($r[$k], 200);
                }
                return $r;
            }, $data['rows'] ?? []);
            ok($data);

        case 'batch_open_folder':
            // 在资源管理器里打开工作目录，方便用户往里面丢文件夹
            $path = trim((string)xy_param('path', ''));
            if ($path === '') fail('路径为空');
            if (!is_dir($path)) {
                if (!@mkdir($path, 0777, true)) fail('目录不存在且无法创建：' . $path);
            }
            @pclose(@popen('explorer.exe ' . escapeshellarg($path), 'r'));
            ok(['path' => $path]);

        case 'batch_upload':
            $path = trim((string)xy_param('path', ''));
            if ($path === '') fail('请先填写工作目录');
            if (!is_dir($path)) fail('目录不存在：' . $path);
            $limit = max(0, (int)xy_param('limit', 0));
            $py = dirname(__DIR__) . '/runtime/python/Scripts/python.exe';
            $script = dirname(__DIR__) . '/tools/batch_publish.py';
            if (!is_file($script)) fail('缺少 tools/batch_publish.py');

            $prog = xy_data_dir() . '/batch_progress.json';
            // 上一次还在跑就别重复启动
            if (is_file($prog)) {
                $d = json_decode((string)file_get_contents($prog), true);
                if (is_array($d) && ($d['status'] ?? '') === 'running') {
                    $ts = strtotime((string)($d['updated_at'] ?? ''));
                    if ($ts !== false && time() - $ts < 60) {
                        fail('已有批量上传任务在跑，请等它结束');
                    }
                }
            }
            @unlink($prog);
            @file_put_contents($prog, json_encode(
                ['status' => 'starting', 'current' => 0, 'total' => 0,
                 'message' => '正在启动…', 'updated_at' => date('Y-m-d H:i:s')],
                JSON_UNESCAPED_UNICODE));

            // 异步启动：同步跑会被 PHP 超时掐断（24 个商品要十几分钟）。
            //
            // 走 detach_run.py 而不是直接 proc_open（2026-10-22 用户反馈黑框）：
            //   ① python.exe 是控制台程序，proc_open 会弹出黑色 cmd 窗口；
            //   ② 长驻子进程继承 php 的 socket，可能让本请求卡住不返回。
            // detach_run 用 CREATE_NO_WINDOW 启动，既无窗口也不卡调用方。
            $logFile = xy_data_dir() . '/logs/batch.log';
            @mkdir(dirname($logFile), 0777, true);
            putenv('PYTHONIOENCODING=utf-8');
            putenv('PYTHONUTF8=1');
            putenv('NO_PROXY=127.0.0.1,localhost');
            $launcher = dirname(__DIR__) . '/tools/detach_run.py';
            if (!is_file($launcher)) fail('缺少 tools/detach_run.py');
            $cmd = escapeshellarg($py) . ' ' . escapeshellarg($launcher)
                 . ' --log ' . escapeshellarg($logFile) . ' -- '
                 . escapeshellarg($script)
                 . ' --run ' . escapeshellarg($path)
                 . ($limit > 0 ? ' --limit ' . $limit : '')
                 . (xy_param('fresh', '0') === '1' ? ' --fresh' : '');
            $out = [];
            $rc = 0;
            @exec($cmd . ' 2>&1', $out, $rc);
            $pid = (int)trim((string)($out[0] ?? '0'));
            if ($pid <= 0) {
                fail('启动批量上传失败：' . xy_safe_text(implode(' | ', array_slice($out, 0, 3)), 160));
            }
            xy_log_activity('PUBLISH', "批量上传已启动：{$path}"
                                      . ($limit > 0 ? "（仅前 {$limit} 个）" : ''));
            ok(['state' => 'started', 'log' => $logFile, 'pid' => $pid]);

        case 'batch_status':
            $f = xy_data_dir() . '/batch_progress.json';
            if (!is_file($f)) ok(['status' => 'idle', 'message' => '尚未开始']);
            $d = json_decode((string)file_get_contents($f), true);
            if (!is_array($d)) ok(['status' => 'idle', 'message' => '进度文件损坏']);
            // 明细里的字符串做一次编码清洗，避免中文异常把整个响应打死
            if (!empty($d['details']) && is_array($d['details'])) {
                foreach ($d['details'] as &$it) {
                    foreach ($it as $k => $v) {
                        if (is_string($v)) $it[$k] = xy_safe_text($v, 200);
                    }
                }
                unset($it);
            }
            ok($d);

        // ---------- 数据库连接设置（本地 / 远程） ----------
        // 配置存 data/db_config.json，PHP 与 Python 引擎共用这一份。
        // 两套参数常驻、只用 mode 切换：远程挂了把 mode 改回 local 就能恢复。
        case 'db_get':
            $c = xy_db_config();
            $mask = function ($x) {
                return ['host' => $x['host'], 'port' => (int)$x['port'],
                        'user' => $x['user'], 'db' => $x['db'],
                        'has_pass' => !empty($x['pass'])];
            };
            $a = xy_db_active();
            ok([
                'mode'     => $c['mode'],
                'local'    => $mask($c['local']),
                'remote'   => $mask($c['remote']),
                'active'   => ['host' => $a['host'], 'db' => $a['db'],
                               'user' => $a['user'], 'port' => (int)$a['port']],
                'fallback' => xy_db_fallback(),
            ]);

        case 'db_test':
            $host = trim((string)xy_param('host', ''));
            $port = max(1, (int)xy_param('port', 3306));
            $user = trim((string)xy_param('user', ''));
            $dbn  = trim((string)xy_param('db', ''));
            $pass = (string)xy_param('pass', '');
            if ($host === '' || $user === '') fail('主机和账号必填');
            // 密码留空 = 沿用已保存的那份（前端不回显明文密码）
            if ($pass === '') {
                $c = xy_db_config();
                foreach (['local', 'remote'] as $k) {
                    if ($host === $c[$k]['host'] && $user === $c[$k]['user']) {
                        $pass = $c[$k]['pass'];
                        break;
                    }
                }
            }
            if (!xy_db_reachable($host, $port)) {
                fail("连不上 {$host}:{$port}（端口不通）。检查：MySQL 是否启动、"
                     . '防火墙/安全组是否放行、地址端口是否正确');
            }
            $test = xy_connect_with(['host' => $host, 'port' => $port, 'user' => $user,
                                     'pass' => $pass, 'db' => ''],
                                    $dbn !== '' ? $dbn : null);
            if (!$test) {
                fail('能连到端口但登录失败：账号/密码错，或该库不存在、账号没有权限');
            }
            $n = 0;
            $q = @mysqli_query($test, 'SHOW TABLES');
            if ($q) { $n = mysqli_num_rows($q); }
            $ver = '';
            $q2 = @mysqli_query($test, 'SELECT VERSION()');
            if ($q2) { $row = mysqli_fetch_row($q2); $ver = (string)($row[0] ?? ''); }
            @mysqli_close($test);
            ok(['msg' => '连接成功', 'tables' => $n, 'version' => $ver]);

        case 'db_save':
            $mode = (xy_param('mode', 'local') === 'remote') ? 'remote' : 'local';
            $f = xy_data_dir() . '/db_config.json';
            $old = xy_db_config();

            $build = function ($prefix, $fallback) {
                $host = trim((string)xy_param($prefix . '_host', ''));
                $user = trim((string)xy_param($prefix . '_user', ''));
                $dbn  = trim((string)xy_param($prefix . '_db', ''));
                $pass = (string)xy_param($prefix . '_pass', '');
                return [
                    'host' => $host !== '' ? $host : $fallback['host'],
                    'port' => max(1, (int)xy_param($prefix . '_port',
                                    $fallback['port'] ?? 3306)),
                    'user' => $user !== '' ? $user : $fallback['user'],
                    // 密码留空 = 保持原值（界面不回显明文，用户不填就说明没改）
                    'pass' => $pass !== '' ? $pass : $fallback['pass'],
                    'db'   => $dbn !== '' ? $dbn : $fallback['db'],
                ];
            };
            $cfg = [
                'mode'   => $mode,
                'local'  => $build('local', $old['local']),
                'remote' => $build('remote', $old['remote']),
            ];
            if ($mode === 'remote' && $cfg['remote']['host'] === '') {
                fail('选择了远程库但没填主机地址');
            }
            @mkdir(dirname($f), 0777, true);
            if (@file_put_contents($f, json_encode($cfg, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT)) === false) {
                fail('配置写入失败（data 目录不可写？）');
            }
            @chmod($f, 0600);
            $where = $mode === 'remote'
                ? $cfg['remote']['host'] . '/' . $cfg['remote']['db']
                : '本地 ' . $cfg['local']['db'];
            xy_log_activity('INFO', "数据库连接已切换为 {$where}");
            ok(['mode' => $mode, 'msg' => "已切换到 {$where}"]);

        // ---------- 登录与账号 ----------
        case 'auth_change_pass':
            $r = auth_change_password((string)xy_param('old', ''),
                                      (string)xy_param('new', ''));
            if (!$r['ok']) fail($r['msg']);
            xy_log_activity('INFO', '登录密码已修改');
            ok(['msg' => $r['msg']]);

        // ---------- 内网穿透（2026-10-22 用户要求） ----------
        // 本机跑 tunnel_client.py 主动连公网 tunnel_server.py，
        // 外网访问服务器端口 = 访问本机控制台。配置存 data/tunnel.json。
        case 'tunnel_get':
            $f = xy_data_dir() . '/tunnel.json';
            $cfg = is_file($f) ? (json_decode((string)file_get_contents($f), true) ?: []) : [];
            $defaults = ['server' => '', 'port' => 8080, 'token' => '',
                         'local_port' => 8082, 'tls' => 0,
                         'access_user' => '', 'access_password' => ''];
            ok(['config' => array_merge($defaults, $cfg)]);

        case 'tunnel_save':
            $f = xy_data_dir() . '/tunnel.json';
            $cfg = [
                'server'          => trim((string)xy_param('server', '')),
                'port'            => max(1, (int)xy_param('port', 8080)),
                'token'           => trim((string)xy_param('token', '')),
                'local_port'      => max(1, (int)xy_param('local_port', 8082)),
                'tls'             => (int)xy_param('tls', 0) ? 1 : 0,
                'access_user'     => trim((string)xy_param('access_user', '')),
                'access_password' => (string)xy_param('access_password', ''),
            ];
            @mkdir(dirname($f), 0777, true);
            if (@file_put_contents($f, json_encode($cfg, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT)) === false) {
                fail('配置写入失败（data 目录不可写？）');
            }
            @chmod($f, 0600);   // 里面有 token，别让同机其他用户读到
            ok(['config' => $cfg]);

        case 'tunnel_status':
            $pidFile = xy_data_dir() . '/tunnel_client.pid';
            $logFile = xy_data_dir() . '/logs/tunnel_client.log';
            $pid = is_file($pidFile) ? (int)trim((string)file_get_contents($pidFile)) : 0;
            $alive = false;
            if ($pid > 0) {
                // php 内置服务器环境下用 tasklist 判存活（wmic 在受限环境会被拦）
                $out = [];
                @exec('tasklist /FI "PID eq ' . $pid . '" /NH 2>NUL', $out);
                foreach ($out as $ln) {
                    if (stripos($ln, (string)$pid) !== false) { $alive = true; break; }
                }
            }
            $tail = [];
            if (is_file($logFile)) {
                $lines = @file($logFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [];
                $tail = array_map(function ($s) { return xy_safe_text($s, 200); },
                                  array_slice($lines, -12));
            }
            ok(['pid' => $pid, 'alive' => $alive,
                'connected' => (bool)preg_grep('/已连接/', $tail),
                'log' => $tail]);

        case 'tunnel_start':
            $f = xy_data_dir() . '/tunnel.json';
            if (!is_file($f)) fail('请先保存配置');
            $c = json_decode((string)file_get_contents($f), true) ?: [];
            if (empty($c['server']) || empty($c['token'])) fail('请先填写服务器地址和令牌');
            $pidFile = xy_data_dir() . '/tunnel_client.pid';
            if (is_file($pidFile)) {
                $old = (int)trim((string)file_get_contents($pidFile));
                if ($old > 0) {
                    $out = [];
                    @exec('tasklist /FI "PID eq ' . $old . '" /NH 2>NUL', $out);
                    foreach ($out as $ln) {
                        if (stripos($ln, (string)$old) !== false) fail('客户端已在运行（PID ' . $old . '），请先停止');
                    }
                }
            }
            $py = dirname(__DIR__) . '/runtime/python/Scripts/python.exe';
            $script = dirname(__DIR__) . '/tools/tunnel_client.py';
            $launcher = dirname(__DIR__) . '/tools/detach_run.py';
            if (!is_file($script)) fail('缺少 tools/tunnel_client.py');
            if (!is_file($launcher)) fail('缺少 tools/detach_run.py');
            // 走通用启动器，别直接 proc_open 长驻进程：
            //   ① 长驻子进程继承 php 的 socket → 本请求永不返回（实测卡 25 秒）；
            //   ② python.exe 是控制台程序 → 弹出黑色 cmd 窗口（用户截图反馈）。
            // detach_run 用 CREATE_NO_WINDOW，两个问题一起解决。
            $logFile = xy_data_dir() . '/logs/tunnel_client.log';
            @mkdir(dirname($logFile), 0777, true);
            $args = ' --server ' . escapeshellarg((string)$c['server'])
                  . ' --port ' . (int)($c['port'] ?? 8080)
                  . ' --token ' . escapeshellarg((string)$c['token'])
                  . ' --local-port ' . (int)($c['local_port'] ?? 8082)
                  . (!empty($c['tls']) ? ' --tls' : '')
                  . ' --verbose';
            putenv('PYTHONIOENCODING=utf-8');
            putenv('PYTHONUTF8=1');
            putenv('NO_PROXY=127.0.0.1,localhost');
            $cmd = escapeshellarg($py) . ' ' . escapeshellarg($launcher)
                 . ' --log ' . escapeshellarg($logFile)
                 . ' --pidfile ' . escapeshellarg($pidFile)
                 . ' -- ' . escapeshellarg($script) . $args;
            $out = [];
            $rc = 0;
            @exec($cmd . ' 2>&1', $out, $rc);
            $pid = (int)trim((string)($out[0] ?? '0'));
            if ($pid <= 0) {
                fail('启动失败：' . xy_safe_text(implode(' | ', array_slice($out, 0, 3)), 160));
            }
            xy_log_activity('INFO', "内网穿透客户端已启动（PID {$pid}），指向 {$c['server']}:{$c['port']}");
            ok(['pid' => $pid]);

        case 'tunnel_stop':
            $pidFile = xy_data_dir() . '/tunnel_client.pid';
            if (!is_file($pidFile)) fail('没有正在运行的客户端');
            $pid = (int)trim((string)file_get_contents($pidFile));
            if ($pid > 0) @exec('taskkill /F /PID ' . $pid . ' 2>NUL');
            @unlink($pidFile);
            ok([]);

        case 'bulk_bind':
            // 批量绑定：pairs = JSON [{"item_id":1,"bundle_id":2}, ...]
            $raw = xy_param('pairs', '');
            $pairs = json_decode($raw, true);
            if (!is_array($pairs) || empty($pairs)) fail('没有要绑定的数据');
            $n = 0;
            foreach ($pairs as $p) {
                $iid = (int)($p['item_id'] ?? 0);
                $bid = (int)($p['bundle_id'] ?? 0);
                if ($iid <= 0 || $bid <= 0) continue;
                // 幂等：已存在就跳过（联合主键）
                xy_exec("INSERT IGNORE INTO item_bundles (item_id, bundle_id) VALUES (?,?)",
                        [$iid, $bid]);
                $n++;
            }
            if ($n === 0) fail('没有有效的绑定对');
            xy_log_activity('BIND', "批量绑定卡券 {$n} 组（商品↔卡卷）");
            ok(['bound' => $n]);

        case 'bundle_items':
            // 某卡卷被哪些商品绑定（卡卷行内查看）
            $bid = (int)xy_param('id', 0);
            $rows = xy_all("SELECT s.id, s.title FROM item_bundles ib
                            JOIN shop_items s ON s.id=ib.item_id
                            WHERE ib.bundle_id=? ORDER BY s.id", [$bid]);
            ok(['rows' => $rows]);

        // ---------- 会话 / 消息 ----------
        case 'conversations':
            ok(['rows' => xy_all("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 200")]);

        case 'messages':
            $buyer = xy_param('buyer', '');
            if ($buyer !== '') {
                $rows = xy_all("SELECT * FROM messages WHERE buyer_name=? ORDER BY id DESC LIMIT 200", [$buyer]);
            } else {
                $rows = xy_all("SELECT * FROM messages ORDER BY id DESC LIMIT 200");
            }
            ok(['rows' => $rows]);

        // ---------- 发货记录 / 日志 ----------
        case 'deliveries':
            ok(['rows' => xy_all("SELECT * FROM deliveries ORDER BY id DESC LIMIT 200")]);

        case 'delivery_delete':
            // 订单管理：单条/批量删除（ids 逗号分隔）
            $ids = array_values(array_filter(
                array_map('intval', preg_split('/[,，\s]+/', (string)xy_param('ids', ''))),
                function($x){ return $x > 0; }
            ));
            if (!$ids) fail('未指定要删除的订单');
            $in = implode(',', array_fill(0, count($ids), '?'));
            xy_exec("DELETE FROM deliveries WHERE id IN ($in)", $ids);
            ok(['deleted' => count($ids)]);

        case 'activity':
            ok(['rows' => xy_all("SELECT * FROM activity_log ORDER BY id DESC LIMIT 200")]);

        // ---------- 设置 ----------
        case 'settings_get':
            $s = xy_one("SELECT * FROM settings WHERE id=1");
            if ($s) $s['selectors'] = xy_json_field($s['selectors']);
            ok(['settings' => $s]);

        case 'settings_save':
            $sets = []; $args = [];
            $map = ['cdp_port'=>'i','poll_interval'=>'f','max_replies_per_hour'=>'i','dry_run'=>'i','rate_limit_enabled'=>'i','rate_limit_per_user_hour'=>'i','rate_limit_window_sec'=>'i','scan_enabled'=>'i','scan_scope'=>'i','scan_interval'=>'i','batch_interval_min'=>'i','batch_daily_max'=>'i','login_guard_enabled'=>'i'];
            foreach ($map as $k => $t) {
                if (($v = xy_param($k, null)) !== null) {
                    $sets[] = "$k=?"; $args[] = ($t==='i')?(int)$v:(float)$v;
                }
            }
            if (($v = xy_param('selectors', null)) !== null) {
                $sets[] = "selectors=?"; $args[] = $v; // 前端传 JSON 字符串
            }
            if ($sets) { $args[] = 1; xy_exec("UPDATE settings SET ".implode(',', $sets)." WHERE id=?", $args); }
            ok([]);

        // ---------- 引擎控制（意图写入 sw_control，由桌面软件执行进程） ----------
        case 'engine_start':
            // 写入「希望运行」意图；桌面软件监听后会拉起 Python 引擎。
            // 引擎连上 CDP / 上报心跳通常需要几秒，这里轮询等待结果。
            xy_set_control('engine', 'run');
            $state = 'starting';
            for ($i = 0; $i < 10; $i++) {
                $st = xy_one("SELECT * FROM engine_status WHERE id=1");
                if ($st && $st['state'] === 'running' && $st['last_heartbeat']) {
                    $ts = strtotime($st['last_heartbeat']);
                    if ($ts !== false && (time() - $ts) < 15) { $state = 'running'; break; }
                }
                usleep(500000);
            }
            ok(['state' => $state]);

        case 'engine_stop':
            xy_set_control('engine', 'stop');
            xy_exec("INSERT INTO commands (cmd) VALUES ('stop')");
            ok([]);

        case 'engine_scan':
            xy_exec("INSERT INTO commands (cmd) VALUES ('scan_now')");
            ok([]);

        // ---------- 发布商品：发布期间暂停引擎（2026-10-20 用户要求） ----------
        // 为什么要停：引擎的定时扫描每轮都会 goto 切路由（回 im-desktop），
        // 正在填写的发布表单会被冲掉。用户在网页点「发布商品」时自动暂停，
        // 发布结束再恢复，全程静默、只在运行日志里留痕。
        case 'publish_engine_pause':
            $was = xy_engine_alive();
            if ($was) {
                xy_set_control('engine', 'stop');
                xy_exec("INSERT INTO commands (cmd) VALUES ('stop')");
                // 等它真正退出（最多 6s）：否则发布过程中它还在切路由
                for ($i = 0; $i < 12; $i++) {
                    if (!xy_engine_alive()) break;
                    usleep(500000);
                }
                xy_log_activity('PUBLISH', '发布商品：已自动暂停自动发货引擎'
                                          . '（定时扫描会切页面，避免冲掉发布表单）');
            } else {
                xy_log_activity('PUBLISH', '发布商品：引擎原本未运行，无需暂停');
            }
            ok(['was_running' => $was]);

        case 'publish_engine_resume':
            // force=1 表示发布前引擎在运行 → 需要恢复；否则保持停止原样
            if (xy_param('force', '0') !== '1') {
                xy_log_activity('PUBLISH', '发布商品：引擎原本未运行，结束后保持停止状态');
                ok(['state' => 'skipped']);
            }
            xy_set_control('engine', 'run');
            $up = false;
            for ($i = 0; $i < 20; $i++) {          // 最多等 10s
                if (xy_engine_alive()) { $up = true; break; }
                usleep(500000);
            }
            xy_log_activity('PUBLISH', $up
                ? '发布商品：引擎已自动恢复运行'
                : '发布商品：引擎恢复超时，请到控制台检查');
            ok(['state' => $up ? 'running' : 'timeout']);

        case 'publish_note':
            // 发布流程的进度留痕。**只允许 PUBLISH 级别**，避免成为任意写日志的入口。
            $msg = trim((string)xy_param('msg', ''));
            if ($msg !== '') xy_log_activity('PUBLISH', mb_substr($msg, 0, 200));
            ok([]);

        // ---------- 发布商品：先落盘到本地商品文件夹，再后台上传（2026-10-20） ----------
        // 用户要求：「点发布时先把所有信息保存到文件夹，然后后台静默上传，
        // 点上传后显示上传中请稍后，看日志是否上传成功」。
        // 落盘的意义：断网/上传失败时商品资料不会丢，可以直接重试上传；
        // 也便于排查"到底提交了什么内容"。
        case 'publish_begin':
            $root = xy_data_dir() . '/publish';
            if (!is_dir($root) && !@mkdir($root, 0777, true)) {
                fail('无法创建商品目录（检查 data 目录写权限）');
            }
            $name = date('Ymd_His') . '_' . substr(md5(uniqid('', true)), 0, 6);
            if (!@mkdir($root . '/' . $name, 0777, true)) fail('无法创建本次商品文件夹');
            xy_log_activity('PUBLISH', "发布商品：已创建商品文件夹 publish/{$name}");
            ok(['dir' => $name]);

        case 'publish_image':
            $dir = xy_publish_dirname(xy_param('dir', ''));
            if ($dir === '') fail('商品目录无效');
            $idx = max(1, (int)xy_param('idx', 1));
            $raw = (string)xy_param('data', '');
            if ($raw === '') fail('图片内容为空');
            // 兼容 dataURL（data:image/jpeg;base64,xxxx）
            $comma = strpos($raw, ',');
            if ($comma !== false) $raw = substr($raw, $comma + 1);
            $bin = base64_decode($raw, true);
            if ($bin === false) fail('图片数据不是合法 base64');
            if (strlen($bin) > 6 * 1024 * 1024) fail('单张图片超过 6MB');
            $file = sprintf('%02d.jpg', $idx);
            $path = xy_data_dir() . '/publish/' . $dir . '/' . $file;
            if (@file_put_contents($path, $bin) === false) fail('图片写入失败');
            ok(['file' => $file, 'bytes' => strlen($bin)]);

        case 'publish_finish':
            $dir = xy_publish_dirname(xy_param('dir', ''));
            if ($dir === '') fail('商品目录无效');
            $base = xy_data_dir() . '/publish/' . $dir;
            $imgs = [];
            foreach ((array)glob($base . '/*.jpg') as $p) $imgs[] = basename($p);
            sort($imgs);
            $manifest = [
                'created_at'   => date('Y-m-d H:i:s'),
                'status'       => 'pending_upload',
                'title'        => trim((string)xy_param('title', '')),
                'price'        => trim((string)xy_param('price', '')),
                'origin_price' => trim((string)xy_param('origin', '')),
                'stock'        => (int)xy_param('stock', 1),
                'desc'         => (string)xy_param('desc', ''),
                'is_virtual'   => xy_param('is_virtual', '1') === '1',
                'bundle_id'    => (int)xy_param('bundle_id', 0),
                'images'       => $imgs,
                'history'      => [['at' => date('Y-m-d H:i:s'), 'event' => 'saved']],
            ];
            $json = json_encode($manifest, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
            if (@file_put_contents($base . '/manifest.json', $json) === false) {
                fail('商品信息写入失败');
            }
            $n = count($imgs);
            xy_log_activity('PUBLISH', "发布商品：资料已保存到 publish/{$dir}"
                                      . "（「{$manifest['title']}」¥{$manifest['price']}，{$n} 张图）");
            ok(['dir' => $dir, 'images' => $n, 'status' => 'pending_upload']);

        case 'publish_upload':
            // 把 publish/<dir> 提交到闲鱼发布页（tools/publish_item.py 走 CDP 自动化）。
            // real=1 才真正点「发布」；否则只填表留证（dry），便于先验证表单。
            $dir = xy_publish_dirname(xy_param('dir', ''));
            if ($dir === '') fail('商品目录无效');
            $base = xy_data_dir() . '/publish/' . $dir;
            $mfPath = $base . '/manifest.json';
            if (!is_file($mfPath)) fail('未找到商品信息（manifest.json）');

            $py = dirname(__DIR__) . '/runtime/python/Scripts/python.exe';
            $script = dirname(__DIR__) . '/tools/publish_item.py';
            if (!is_file($py)) fail('未找到 Python 运行时');
            if (!is_file($script)) fail('未找到发布脚本 tools/publish_item.py');

            // 发布可能耗时几十秒（传图 + 智能识别属性 + 提交），别被默认超时掐断
            @set_time_limit(300);
            // 本机 CDP 不能被系统代理劫持；强制 Python 用 UTF-8 输出，
            // 否则管道里是 GBK 字节，写日志会炸（见 xy_safe_text 注释）
            putenv('NO_PROXY=127.0.0.1,localhost');
            putenv('HTTP_PROXY=');
            putenv('HTTPS_PROXY=');
            putenv('PYTHONIOENCODING=utf-8');
            putenv('PYTHONUTF8=1');

            $real = xy_param('real', '0') === '1';
            $progress = $base . '/_progress.json';
            @unlink($progress);
            $cmd = escapeshellarg($py) . ' ' . escapeshellarg($script)
                 . ' --dir ' . escapeshellarg($dir)
                 . ' --progress ' . escapeshellarg($progress)
                 . ($real ? ' --yes' : '');
            xy_log_activity('PUBLISH', "上传商品：publish/{$dir} "
                . ($real ? '开始提交到闲鱼…' : '开始填表（试运行，不点发布）…'));

            $out = [];
            $rc = 0;
            @exec($cmd . ' 2>&1', $out, $rc);
            $prog = is_file($progress)
                ? json_decode((string)file_get_contents($progress), true) : null;
            $state = is_array($prog) ? ($prog['state'] ?? 'unknown') : 'unknown';
            // 进度文件是 UTF-8 JSON，优先用它；exec 输出只作兜底且必须清洗编码
            $msg = is_array($prog) ? xy_safe_text($prog['message'] ?? '', 160) : '';
            if ($msg === '') {
                $msg = xy_safe_text(implode(' | ', array_slice($out, -3)), 160);
            }
            if ($msg === '') $msg = "脚本退出码 {$rc}（未生成进度文件）";

            $okState = ($rc === 0 && in_array($state, ['done', 'dry_done'], true));
            // 回写 manifest，便于日后查看这条商品到底发没发出去
            $mf = json_decode((string)file_get_contents($mfPath), true);
            if (is_array($mf)) {
                $mf['status'] = $state === 'done' ? 'published'
                    : ($state === 'dry_done' ? 'dry_run' : 'failed');
                // 发布成功时把**新商品 ID** 记下来（删旧发新后必须更新，
                // 否则 manifest 还留着上一个已删除商品的 id）
                if ($state === 'done' && !empty($prog['item_id'])) {
                    $mf['item_id'] = (string)$prog['item_id'];
                }
                $mf['history'][] = ['at' => date('Y-m-d H:i:s'),
                                    'event' => $mf['status'], 'message' => $msg];
                // json_encode 遇到非法 UTF-8 会返回 false，直接 file_put_contents
                // 会把 manifest **写成 0 字节**，之后所有重试都读不出商品信息
                // （2026-10-20 实测踩坑）。必须先校验再写。
                $encoded = json_encode($mf, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
                if ($encoded === false) {
                    xy_log_activity('PUBLISH', "上传商品：publish/{$dir} 结果回写失败"
                                              . "（manifest 含非法字符，已保持原样）");
                } else {
                    @file_put_contents($mfPath, $encoded);
                }
            }

            if ($okState) {
                xy_log_activity('PUBLISH', "上传商品：publish/{$dir} "
                    . ($state === 'done' ? '发布成功' : '填表完成（未提交）')
                    . ($msg !== '' ? "（{$msg}）" : ''));
            } else {
                xy_log_activity('PUBLISH', "上传商品：publish/{$dir} 失败"
                    . ($msg !== '' ? "（{$msg}）" : '') . "，资料仍保存在该目录，可重试");
            }
            ok(['dir' => $dir, 'state' => $state, 'rc' => $rc, 'message' => $msg,
                // exec 输出是 GBK，必须逐条清洗，否则 json_encode 会整体失败
                'log' => array_map(function ($l) { return xy_safe_text($l, 200); },
                                    array_slice($out, -8))]);

        case 'engine_start_app':
            // 启动卖家客户端（带调试端口）——写入意图，由桌面软件执行
            xy_set_control('client', 'run');
            ok(['msg' => '已发送启动指令，软件将在后台拉起闲鱼卖家客服并监听 9222 端口']);

        // ---------- 卖家客户端（闲鱼软件）后台启动 / 状态 ----------
        case 'app_status':
            ok(['running' => xy_app_running(), 'port' => 9222]);

        case 'app_start':
            xy_set_control('client', 'run');
            ok(['msg' => '已发送启动指令，软件将在后台拉起闲鱼卖家客服']);

        case 'app_stop':
            xy_set_control('client', 'stop');
            $n = xy_stop_seller_app();   // 兜底：若软件未运行也立即结束现有进程
            ok(['stopped' => $n, 'msg' => $n > 0 ? "已关闭软件（结束 {$n} 个进程）" : '未找到运行中的软件']);

        // ---------- 导出消息（2026-10-18 从桌面按钮移植；执行链同商品采集：
        //   网页写 sw_control 触发器 -> 桌面控制台拉起 export_runner.py
        //   -> 进度写 data/export_progress.json 供本页轮询） ----------
        case 'export_msgs':
            if (xy_export_running()) fail('导出正在进行中，请稍候');
            xy_set_control('export', (string)time());
            ok(['msg' => '导出已启动（重启客户端后逐个打开会话，约需几分钟，进度见下方）']);

        case 'export_msgs_status':
            $f = xy_data_dir() . '/export_progress.json';
            $ctrl = xy_get_control('export', '0');
            $pending = ($ctrl !== '' && $ctrl !== '0');   // 触发器已写入、桌面还没接手
            if (!is_file($f)) {
                if ($pending) {
                    ok(['status' => 'pending', 'current' => 0, 'total' => 0,
                         'message' => '已发送导出指令，等待桌面控制台执行…（请确认「闲鱼卖家客服·控制台启动器」正在运行）']);
                }
                ok(['status' => 'idle', 'current' => 0, 'total' => 0, 'message' => '尚未导出过']);
            }
            $d = json_decode((string)file_get_contents($f), true);
            if (!is_array($d)) $d = ['status' => 'idle', 'current' => 0, 'total' => 0, 'message' => '进度文件损坏'];
            // 触发器比进度新 -> 这次点击还没被桌面接手（或刚接手没来得及写进度）
            if ($pending && ($d['trigger'] ?? '') !== $ctrl) {
                $d['status'] = 'pending';
                $d['message'] = '已发送导出指令，等待桌面控制台执行…（请确认「闲鱼卖家客服·控制台启动器」正在运行）';
            }
            ok($d);

        case 'export_files':
            // 列出 backend/messages/ 下的导出产物（_索引.md 固定置顶）
            $dir = realpath(__DIR__ . '/../backend/messages');
            $files = [];
            if ($dir && is_dir($dir)) {
                foreach (glob($dir . '/*.md') as $p) {
                    $files[] = ['name' => basename($p), 'size' => filesize($p),
                                'mtime' => date('Y-m-d H:i', filemtime($p))];
                }
            }
            usort($files, function ($a, $b) {
                if ($a['name'] === '_索引.md') return -1;
                if ($b['name'] === '_索引.md') return 1;
                return strcmp($a['name'], $b['name']);
            });
            ok(['dir' => $dir ?: '', 'files' => $files]);

        case 'export_download':
            // 单文件下载（GET 友好，供 <a href> 直接用）。
            // 安全：basename 剥掉目录 + realpath  containment 双重防护，只放行 .md。
            $name = basename((string)xy_param('name', ''));
            if ($name === '' || !preg_match('/\.md$/i', $name)) fail('非法的文件名');
            $dir = realpath(__DIR__ . '/../backend/messages');
            $p = ($dir && $name !== '') ? realpath($dir . '/' . $name) : false;
            if (!$dir || !$p || strncmp($p, $dir, strlen($dir)) !== 0 || !is_file($p)) {
                fail('文件不存在（请先执行导出）');
            }
            header('Content-Type: text/markdown; charset=utf-8');
            header('Content-Disposition: attachment; filename="session.md";'
                 . " filename*=UTF-8''" . rawurlencode($name));
            header('Content-Length: ' . filesize($p));
            readfile($p);
            exit;

        default:
            fail('未知 action: ' . $action);
    }
} catch (Exception $e) {
    fail('异常：' . $e->getMessage());
}

// ---------- 工具函数 ----------

/** 采集器数据目录（临时 JSON 的落点；data/collected_products.json 是临时文件，
 *  由 product_import 导入数据库后即完成使命）。相对软件根目录（frontend 的上级）。 */
function xy_data_dir() {
    return realpath(__DIR__ . '/../data') ?: (__DIR__ . '/../data');
}

/**
 * 把采集器产物 data/collected_products.json 导入 shop_items。
 *
 * 为什么抽成函数（2026-10-19 用户要求「删掉导入数据库按钮，让用户感觉不到」）：
 * 原来导入只能由前端在采集完成后额外发一次 product_import 请求触发——用户
 * 中途关掉页面、或刷新时正好错过那一刻，商品就永远进不了库，得手动点按钮。
 * 现在改成：采集状态变 done 时**后端自己调这个函数**，前端只管显示进度条；
 * product_import 接口保留（兼容旧页面/手动补导），内部也走同一份逻辑。
 *
 * 幂等：shop_items 以 item_id 做 UNIQUE，用 ON DUPLICATE KEY UPDATE 覆盖，
 * 重复导入不会产生脏数据。
 *
 * @param bool $force 为 true 时忽略"本次已导入过"的标记强制重导
 * @return int 实际写入/更新的商品数
 */
function xy_import_collected_products($force = false) {
    $f = xy_data_dir() . '/collected_products.json';
    if (!is_file($f)) return 0;

    // 同一份采集文件只自动导一次：用文件 mtime 做戳，避免每次轮询都重导
    // （虽然 ON DUPLICATE KEY 幂等，但会重复刷活动日志）。
    $stamp_file = xy_data_dir() . '/.imported_stamp';
    $mtime = (string)@filemtime($f);
    if (!$force && is_file($stamp_file)
        && trim((string)@file_get_contents($stamp_file)) === $mtime) {
        return 0;
    }

    $items = json_decode((string)file_get_contents($f), true);
    if (!is_array($items) || empty($items)) return 0;

    $n = 0;
    foreach ($items as $it) {
        $iid = trim((string)($it['item_id'] ?? ''));
        if ($iid === '') continue;
        $title = trim((string)($it['detail_title'] ?? $it['title'] ?? ''));
        $title = preg_replace('/_闲鱼\s*$/', '', $title);   // 去详情页标题后缀
        if ($title === '') $title = (string)($it['title'] ?? '');
        $phys = xy_is_physical_text($title) ? 1 : 0;
        $gal = json_encode($it['gallery'] ?? [], JSON_UNESCAPED_UNICODE);
        xy_exec("INSERT INTO shop_items (item_id, title, price, origin_price, img, descr, gallery, is_physical)
                VALUES (?,?,?,?,?,?,?,?)
                ON DUPLICATE KEY UPDATE title=VALUES(title), price=VALUES(price),
                  origin_price=VALUES(origin_price), img=VALUES(img),
                  descr=VALUES(descr), gallery=VALUES(gallery), is_physical=VALUES(is_physical)",
                [$iid, $title, (string)($it['price'] ?? ''),
                 (string)($it['origin_price'] ?? ''), (string)($it['img'] ?? ''),
                 (string)($it['desc'] ?? ''), $gal, $phys]);
        $n++;
    }
    if ($n > 0) {
        @file_put_contents($stamp_file, $mtime);
        xy_log_activity('IMPORT', "自动导入店铺商品 {$n} 个");
    }
    return $n;
}

/** 软件控制意图表（sw_control）：网站/前端按钮只写意图，桌面软件读取并执行进程。
 *  key: engine(运行引擎) / client(闲鱼客户端) / collect(采集触发器, 存时间戳)
 *      / export(导出消息触发器, 存时间戳)
 *  val: 'run' | 'stop' | 时间戳
 *  表不存在则自动建（兼容旧库）。 */
function xy_ensure_control_table() {
    static $done = false;
    if ($done) return;
    $done = true;
    $c = xy_raw_connect(XY_DB);
    if (!$c) return;
    mysqli_query($c, "CREATE TABLE IF NOT EXISTS sw_control (
        `key` VARCHAR(32) NOT NULL PRIMARY KEY,
        `val` VARCHAR(64) NOT NULL DEFAULT '',
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci");
    mysqli_close($c);
}

function xy_set_control($key, $val) {
    xy_ensure_control_table();
    $c = xy_raw_connect(XY_DB);
    if (!$c) return false;
    mysqli_query($c, "INSERT INTO sw_control (`key`,`val`) VALUES ('".mysqli_real_escape_string($c,$key)."','".mysqli_real_escape_string($c,$val)."')
                     ON DUPLICATE KEY UPDATE val=VALUES(val)");
    mysqli_close($c);
    return true;
}

function xy_get_control($key, $default = '') {
    xy_ensure_control_table();
    $c = xy_raw_connect(XY_DB);
    if (!$c) return $default;
    $r = mysqli_query($c, "SELECT val FROM sw_control WHERE `key`='".mysqli_real_escape_string($c,$key)."'");
    $v = $default;
    if ($r && $row = mysqli_fetch_assoc($r)) $v = $row['val'];
    mysqli_close($c);
    return $v;
}

/** 采集是否正在进行（progress=running 且 10 分钟内刷新过）。 */
function xy_collect_running() {
    $f = xy_data_dir() . '/collect_progress.json';
    if (!is_file($f)) return false;
    $d = json_decode((string)file_get_contents($f), true);
    if (!is_array($d) || ($d['status'] ?? '') !== 'running') return false;
    $ts = strtotime((string)($d['updated_at'] ?? ''));
    return $ts !== false && (time() - $ts) < 600;
}

/** 导出消息是否正在进行（progress=running 且 10 分钟内刷新过）。 */
function xy_export_running() {
    $f = xy_data_dir() . '/export_progress.json';
    if (!is_file($f)) return false;
    $d = json_decode((string)file_get_contents($f), true);
    if (!is_array($d) || ($d['status'] ?? '') !== 'running') return false;
    $ts = strtotime((string)($d['updated_at'] ?? ''));
    return $ts !== false && (time() - $ts) < 600;
}

/** 实物商品关键词（用户作为卖家卖的电脑硬件等，与虚拟卡密业务无关）。 */
function xy_is_physical_text($s) {
    foreach (['5060', '1060', '显卡', '七彩虹', '影驰', '7500f'] as $h) {
        if (mb_stripos((string)$s, $h) !== false) return true;
    }
    return false;
}

/** 写一条运行日志（activity_log）。 */
function xy_log_activity($level, $msg) {
    xy_exec("INSERT INTO activity_log (level, message) VALUES (?,?)", [$level, $msg]);
}

/**
 * 引擎是否真的在跑：以**心跳时间**为准（<15s），不看 state 字段。
 *
 * 为什么不能只看 state：引擎崩溃/被外部杀掉时 state 会停在 'running'，
 * 靠心跳才能反映真实存活（控制台仪表盘也是同一口径）。
 *
 * @return bool 存活返回 true
 */
function xy_engine_alive(): bool {
    $st = xy_one("SELECT * FROM engine_status WHERE id=1");
    if (!$st || ($st['state'] ?? '') !== 'running' || empty($st['last_heartbeat'])) {
        return false;
    }
    $ts = strtotime((string)$st['last_heartbeat']);
    return $ts !== false && (time() - $ts) < 15;
}

/**
 * 校验发布商品目录名，防止路径穿越。
 *
 * 目录名只由 publish_begin 生成，形如 `20261020_003401_a1b2c3`。
 * 前端会把它回传，**绝不能直接拼进路径**——否则 `../` 就能写到 data 外面。
 *
 * @param string $raw 前端回传的目录名
 * @return string 合法则返回目录名，非法返回空串
 */
function xy_publish_dirname($raw): string {
    $n = trim((string)$raw);
    return preg_match('/^[0-9]{8}_[0-9]{6}_[a-f0-9]{6}$/', $n) === 1 ? $n : '';
}

/**
 * 把子进程输出清洗成**合法 UTF-8** 再入库。
 *
 * 为什么需要：Windows 下 Python 通过管道输出中文时，stdout 用系统代码页（GBK），
 * 于是 `exec()` 拿到的是 GBK 字节。直接写进 utf8mb4 的 activity_log 会抛
 * 「Incorrect string value」把整个接口打成 500（2026-10-20 实测）。
 *
 * @param mixed $s   原始文本
 * @param int   $max 截断长度
 * @return string 合法 UTF-8
 */
function xy_safe_text($s, int $max = 180): string {
    $s = (string)$s;
    if ($s === '') return '';
    if (!mb_check_encoding($s, 'UTF-8')) {
        $conv = @mb_convert_encoding($s, 'UTF-8', 'GB18030,GBK,CP936');
        if (is_string($conv) && mb_check_encoding($conv, 'UTF-8')) $s = $conv;
    }
    if (!mb_check_encoding($s, 'UTF-8')) {
        // 仍不合法：剔除所有非法字节，宁可少字也不能让写库失败
        $s = preg_replace('/[\x00-\x08\x0B\x0C\x0E-\x1F]/u', '', $s) ?? '';
        if (!mb_check_encoding($s, 'UTF-8')) $s = '';
    }
    return mb_substr($s, 0, $max);
}

function xy_parse_keywords($raw) {
    if (is_array($raw)) return array_values(array_filter(array_map('trim', $raw), function($x){return $x!=='';}));
    $parts = preg_split('/[,，\n\r]+/u', (string)$raw);
    return array_values(array_filter(array_map('trim', $parts), function($x){ return $x !== ''; }));
}

// ---------- 卖家客户端（闲鱼软件）后台启动 / 状态 ----------
function xy_app_exe_dirs() {
    // 顺序：① 安装时填的路径（data/client_path.txt）② LOCALAPPDATA 标准位置 ③ 各盘常见目录
    // 绝不能写死某个用户名（原实现写着 <用户名>\...，别人机器上必然找不到）
    $dirs = [];
    $cfg = dirname(__DIR__) . DIRECTORY_SEPARATOR . 'data' . DIRECTORY_SEPARATOR . 'client_path.txt';
    if (is_file($cfg)) {
        $saved = trim((string)@file_get_contents($cfg));
        if ($saved !== '') $dirs[] = rtrim($saved, '\\/');
    }
    if (getenv('LOCALAPPDATA')) {
        $dirs[] = rtrim(getenv('LOCALAPPDATA'), '\\') . '\\Programs\\xianyu-seller-im';
    }
    foreach (array('C:', 'D:', 'E:', 'F:') as $d) {
        $dirs[] = $d . '\\Programs\\xianyu-seller-im';
        $dirs[] = $d . '\\xianyu-seller-im';
    }
    return array_values(array_filter($dirs));
}

function xy_find_seller_exe() {
    foreach (xy_app_exe_dirs() as $dir) {
        if (!$dir || !is_dir($dir)) continue;
        // 先按固定名，再放宽到「含闲鱼+客服」的 exe
        $exact = $dir . DIRECTORY_SEPARATOR . '闲鱼卖家客服.exe';
        if (is_file($exact)) return $exact;
        foreach (glob($dir . DIRECTORY_SEPARATOR . '*.exe') as $f) {
            $bn = basename($f);
            if ($bn === '闲鱼卖家客服.exe' || (mb_strpos($bn, '闲鱼') !== false
                && mb_strpos($bn, '客服') !== false)) {
                return $f;
            }
        }
    }
    return null;
}

function xy_app_codepage() {
    $out = @shell_exec('chcp 2>nul');
    if (preg_match('/(\d{3,5})/', (string)$out, $m)) return $m[1];
    return '936';
}

// 中文路径需按系统代码页传给 cmd：936(GBK) 转 GBK，65001(UTF-8) 原样。
function xy_app_encode_cmd($cmd) {
    if (xy_app_codepage() === '65001') return $cmd;
    $g = @mb_convert_encoding($cmd, 'GBK', 'UTF-8');
    return $g ?: $cmd;
}

// 软件是否运行：调试端口 9222 是否就绪（引擎与自动发货都依赖此端口）
function xy_app_running() {
    $fp = @fsockopen('127.0.0.1', 9222, $errno, $errstr, 1.0);
    if ($fp) { fclose($fp); return true; }
    return false;
}

function xy_stop_seller_app() {
    // 按端口 9222 找 PID 再 taskkill，避免中文进程名在 cmd 下编码错乱
    $out = @shell_exec('netstat -ano 2>nul | findstr /R "[:]9222 "');
    $pids = [];
    foreach (explode("\n", (string)$out) as $line) {
        if (preg_match('/(\d+)\s*$/', $line, $m)) $pids[] = $m[1];
    }
    $pids = array_unique($pids);
    foreach ($pids as $pid) @shell_exec('taskkill /F /PID ' . $pid . ' 2>nul');
    return count($pids);
}
