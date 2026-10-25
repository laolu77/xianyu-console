<?php
/**
 * 首次启动向导（安装后第一次打开时进入）。
 *
 * 为什么单独一个页面：此时数据库可能还没配好，不能依赖 db.php 的连接逻辑，
 * 所以本页只做"填参数 → 试连 → 写配置"，建表交给首页的 xy_ensure_installed()
 * 自动完成（它本来就是幂等的）。
 *
 * 两个数据库选项（用户明确要求）：
 *   ① 新建数据库：给一个能建库的账号，自动 CREATE DATABASE
 *   ② 连接已有数据库：直接填现成的库，验证能连上就用
 *
 * 已配置过（有 setup_done.flag）时直接跳控制台，防止用户误闯重装。
 */
require_once __DIR__ . '/auth.php';

header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');

$flag = XY_AUTH_DIR . '/setup_done.flag';
if (is_file($flag)) {
    header('Location: index.php');
    exit;
}

$cfgFile = XY_AUTH_DIR . '/db_config.json';
$cfg = is_file($cfgFile) ? (json_decode((string)file_get_contents($cfgFile), true) ?: []) : [];
$local = $cfg['local'] ?? ['host' => '127.0.0.1', 'port' => 3306, 'user' => 'root', 'pass' => '', 'db' => 'xianyu_autoreply'];

// ---------------- AJAX 处理 ----------------
$action = $_GET['action'] ?? '';
if ($action !== '') {
    header('Content-Type: application/json; charset=utf-8');
    $post = function ($k, $d = '') { return trim((string)($_POST[$k] ?? $d)); };

    if ($action === 'test') {
        $host = $post('host'); $port = (int)$post('port', '3306');
        $user = $post('user'); $pass = (string)($_POST['pass'] ?? '');
        $dbn  = $post('db');
        $fp = @fsockopen($host, $port, $e1, $e2, 3.0);
        if (!$fp) {
            echo json_encode(['ok' => false, 'msg' =>
                "连不上 {$host}:{$port}。请确认：① MySQL 服务已启动　② 端口正确　③ 防火墙/安全组放行"],
                JSON_UNESCAPED_UNICODE);
            exit;
        }
        fclose($fp);
        $link = @mysqli_connect($host, $user, $pass, '', $port);
        if (!$link) {
            echo json_encode(['ok' => false, 'msg' =>
                '端口通的，但登录失败：账号或密码不对（或该账号没有远程连接权限）'],
                JSON_UNESCAPED_UNICODE);
            exit;
        }
        $ver = mysqli_get_server_info($link);
        $hasDb = $dbn !== '' && @mysqli_select_db($link, $dbn);
        $tables = 0;
        if ($hasDb) {
            if ($q = @mysqli_query($link, 'SHOW TABLES')) $tables = mysqli_num_rows($q);
        }
        mysqli_close($link);
        echo json_encode(['ok' => true, 'version' => $ver,
                          'db_exists' => (bool)$hasDb, 'tables' => $tables,
                          'msg' => "连接成功（MySQL {$ver}）"
                                  . ($hasDb ? "，库 {$dbn} 已存在（{$tables} 张表）"
                                            : ($dbn !== '' ? "，库 {$dbn} 还不存在，可以让程序自动创建" : ''))],
                         JSON_UNESCAPED_UNICODE);
        exit;
    }

    if ($action === 'finish') {
        $mode = $post('mode', 'new');
        $host = $post('host', '127.0.0.1'); $port = (int)$post('port', '3306');
        $user = $post('user'); $pass = (string)($_POST['pass'] ?? '');
        $dbn  = $post('db', 'xianyu_autoreply');
        $pw   = (string)($_POST['console_pass'] ?? '');
        $client = $post('client_path');

        if ($user === '' || $dbn === '') {
            echo json_encode(['ok' => false, 'msg' => '数据库账号和库名不能为空'], JSON_UNESCAPED_UNICODE); exit;
        }
        if (strlen($pw) < 6) {
            echo json_encode(['ok' => false, 'msg' => '控制台密码至少 6 位'], JSON_UNESCAPED_UNICODE); exit;
        }

        // 1) 连上（新建模式要建库）
        $link = @mysqli_connect($host, $user, $pass, '', $port);
        if (!$link) {
            echo json_encode(['ok' => false, 'msg' => '数据库登录失败，请回到上一步检查'], JSON_UNESCAPED_UNICODE); exit;
        }
        if ($mode === 'new') {
            @mysqli_query($link, "CREATE DATABASE IF NOT EXISTS `{$dbn}` "
                . "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci");
        }
        if (!@mysqli_select_db($link, $dbn)) {
            echo json_encode(['ok' => false, 'msg' => "库 `{$dbn}` 不存在或当前账号无权访问"], JSON_UNESCAPED_UNICODE); exit;
        }
        mysqli_close($link);

        // 2) 写数据库配置（本地/远程都指向这一份连接）
        $entry = ['host' => $host, 'port' => $port, 'user' => $user,
                  'pass' => $pass, 'db' => $dbn];
        $out = ['mode' => 'local', 'local' => $entry, 'remote' => $entry];
        @mkdir(XY_AUTH_DIR, 0777, true);
        @file_put_contents($cfgFile, json_encode($out, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
        @chmod($cfgFile, 0600);

        // 3) 写控制台密码
        $authFile = XY_AUTH_DIR . '/auth.json';
        @file_put_contents($authFile, json_encode([
            'user' => 'admin',
            'hash' => password_hash($pw, PASSWORD_DEFAULT),
            'updated_at' => date('Y-m-d H:i:s'),
        ], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
        @chmod($authFile, 0600);

        // 4) 记下闲鱼客户端路径（引擎启动客户端时用）
        if ($client !== '') {
            @file_put_contents(XY_AUTH_DIR . '/client_path.txt', $client, LOCK_EX);
        }

        // 5) 完成标记
        @file_put_contents($flag, date('Y-m-d H:i:s'));

        echo json_encode(['ok' => true, 'msg' => '初始化完成'], JSON_UNESCAPED_UNICODE);
        exit;
    }

    echo json_encode(['ok' => false, 'msg' => '未知操作'], JSON_UNESCAPED_UNICODE);
    exit;
}

// 探测闲鱼客户端常见位置
$clientGuess = '';
$cands = [
    getenv('LOCALAPPDATA') . '/Programs/xianyu-seller-im',
    'C:/Program Files/xianyu-seller-im',
    'D:/Programs/xianyu-seller-im',
];
foreach ($cands as $c) {
    if ($c && is_dir($c)) { $clientGuess = str_replace('\\', '/', $c); break; }
}
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>初始化 · 闲鱼客服控制台</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{--brand:#2f6df6;--brand-d:#1f4fc4;--err:#e0473e;--ok:#1faa6b;
        --ink:#1f2733;--muted:#6b7686;--line:#e6eaf2;--warn:#e08a00}
  *{box-sizing:border-box}
  body{margin:0;padding:32px 16px;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
       background:linear-gradient(180deg,#0f1830,#16213d);color:var(--ink);min-height:100vh}
  .wrap{max-width:720px;margin:0 auto}
  .hd{display:flex;align-items:center;gap:12px;margin-bottom:20px}
  .logo{width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,#3b82f6,#22d3ee);
        color:#fff;font-weight:700;font-size:20px;display:flex;align-items:center;justify-content:center}
  .hd h1{color:#fff;font-size:18px;margin:0}
  .hd p{color:#8893ad;font-size:12px;margin:2px 0 0}
  .card{background:#fff;border-radius:14px;padding:24px;margin-bottom:16px}
  .card h2{font-size:15px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
  .card .desc{font-size:12px;color:var(--muted);margin:0 0 16px;line-height:1.7}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .field{flex:1;min-width:140px;margin-bottom:12px}
  label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
  input[type=text],input[type=password],input[type=number]{
    width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;font-size:13px;
    outline:none;background:#fbfcfe;transition:.15s}
  input:focus{border-color:var(--brand);background:#fff;box-shadow:0 0 0 3px rgba(47,109,246,.12)}
  .modes{display:flex;gap:10px;margin-bottom:14px}
  .mode{flex:1;border:1.5px solid var(--line);border-radius:10px;padding:12px;cursor:pointer;transition:.15s}
  .mode.on{border-color:var(--brand);background:#f2f7ff}
  .mode b{display:block;font-size:13px;margin-bottom:3px}
  .mode span{font-size:11px;color:var(--muted);line-height:1.5}
  button{padding:10px 18px;border:0;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;transition:.15s}
  .primary{background:var(--brand);color:#fff}
  .primary:hover{background:var(--brand-d)}
  .ghost{background:#eef2f9;color:#3a4560}
  .ghost:hover{background:#e3e9f4}
  .msg{margin-top:12px;padding:10px 12px;border-radius:8px;font-size:12.5px;line-height:1.6;display:none}
  .msg.show{display:block}
  .msg.ok{background:#e9f7f0;color:#137a4c;border:1px solid #c6ecd9}
  .msg.err{background:#fdecea;color:var(--err);border:1px solid #f8cfcb}
  .msg.warn{background:#fff6e6;color:#a86900;border:1px solid #ffe2b0}
  .foot{color:#8893ad;font-size:11px;text-align:center;margin-top:18px;line-height:1.8}
  code{background:#eef2f9;padding:1px 5px;border-radius:4px;font-size:11px}
</style>
</head>
<body>
<div class="wrap">
  <div class="hd">
    <div class="logo">闲</div>
    <div><h1>首次使用，先做几步配置</h1><p>全部填完点最下面「开始使用」即可</p></div>
  </div>

  <!-- ① 数据库 -->
  <div class="card">
    <h2>① 数据库</h2>
    <p class="desc">
      本软件的数据（卡卷、商品、规则、订单）存在 MySQL 里。你需要有可用的 MySQL ——
      本机装的、或服务器上的都行。<br>
      <b>没有 MySQL？</b>装一个 <a href="https://www.mysql.com/downloads/" target="_blank">MySQL 官方版</a>
      或 phpStudy 都可以，装完回到这页填地址即可。
    </p>
    <div class="modes">
      <div class="mode on" data-mode="new">
        <b>新建数据库</b>
        <span>给一个能建库的账号，程序自动创建库和所有表</span>
      </div>
      <div class="mode" data-mode="use">
        <b>使用已有数据库</b>
        <span>已经有库了，直接填进去连上就用</span>
      </div>
    </div>
    <div class="row">
      <div class="field"><label>主机</label>
        <input id="f_host" type="text" value="<?php echo htmlspecialchars($local['host'], ENT_QUOTES); ?>"></div>
      <div class="field" style="max-width:110px"><label>端口</label>
        <input id="f_port" type="number" value="<?php echo (int)$local['port']; ?>"></div>
    </div>
    <div class="row">
      <div class="field"><label>账号</label>
        <input id="f_user" type="text" value="<?php echo htmlspecialchars($local['user'], ENT_QUOTES); ?>"></div>
      <div class="field"><label>密码</label>
        <input id="f_pass" type="password" placeholder="MySQL 密码"></div>
    </div>
    <div class="field"><label>库名</label>
      <input id="f_db" type="text" value="xianyu_autoreply"></div>
    <div class="row" style="align-items:center">
      <button class="ghost" id="btnTest">测试连接</button>
    </div>
    <div class="msg" id="dbMsg"></div>
  </div>

  <!-- ② 闲鱼客户端 -->
  <div class="card">
    <h2>② 闲鱼卖家客户端</h2>
    <p class="desc">
      本软件是<b>操作闲鱼官方客户端</b>来工作的（不是破解接口），所以你必须先装好
      <b>闲鱼卖家客户端</b>并登录自己的店铺。没装的话先装，装完回到这页。
    </p>
    <div class="field">
      <label>客户端安装目录</label>
      <input id="f_client" type="text"
             value="<?php echo htmlspecialchars($clientGuess, ENT_QUOTES); ?>"
             placeholder="例如 C:/Users/你/AppData/Local/Programs/xianyu-seller-im">
    </div>
    <div class="msg <?php echo $clientGuess ? 'ok show' : 'warn show'; ?>" id="clientMsg">
      <?php echo $clientGuess
        ? '✓ 已检测到客户端：' . htmlspecialchars($clientGuess, ENT_QUOTES)
        : '没自动找到客户端。如果还没装，请先安装；装好了就把路径填到上面（可以在客户端快捷方式上右键 → 打开文件所在位置 查到）。'; ?>
    </div>
  </div>

  <!-- ③ 控制台密码 -->
  <div class="card">
    <h2>③ 设置控制台登录密码</h2>
    <p class="desc">以后打开控制台要输这个密码。账号固定是 <code>admin</code>。</p>
    <div class="row">
      <div class="field"><label>密码（至少 6 位）</label>
        <input id="f_pw1" type="password" autocomplete="new-password"></div>
      <div class="field"><label>再输一次</label>
        <input id="f_pw2" type="password" autocomplete="new-password"></div>
    </div>
    <div class="msg" id="pwMsg"></div>
  </div>

  <div style="text-align:center;margin:22px 0 6px">
    <button class="primary" id="btnFinish" style="padding:13px 46px;font-size:15px">开始使用</button>
  </div>
  <div class="msg" id="finMsg" style="max-width:520px;margin:0 auto"></div>
  <div class="foot">配置只会保存在你本机的 data 目录里，不会上传到任何地方。</div>
</div>

<script>
let mode = 'new';
const $ = s => document.querySelector(s);
document.querySelectorAll('.mode').forEach(el => {
  el.onclick = () => {
    document.querySelectorAll('.mode').forEach(x => x.classList.remove('on'));
    el.classList.add('on');
    mode = el.dataset.mode;
    $('#f_db').value = mode === 'new' ? 'xianyu_autoreply' : $('#f_db').value;
  };
});
const show = (sel, type, text) => {
  const el = $(sel);
  el.className = 'msg show ' + type;
  el.textContent = text;
};
const payload = () => ({
  mode: mode,
  host: $('#f_host').value.trim(),
  port: $('#f_port').value.trim(),
  user: $('#f_user').value.trim(),
  pass: $('#f_pass').value,
  db: $('#f_db').value.trim(),
  client_path: $('#f_client').value.trim(),
});

$('#btnTest').onclick = () => {
  const p = payload();
  if (!p.host || !p.user) return show('#dbMsg', 'err', '主机和账号必填');
  show('#dbMsg', 'warn', '正在连接…');
  const fd = new FormData();
  Object.entries(p).forEach(([k, v]) => fd.append(k, v));
  fetch('setup.php?action=test', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(d => show('#dbMsg', d.ok ? 'ok' : 'err', (d.ok ? '✓ ' : '✗ ') + d.msg))
    .catch(e => show('#dbMsg', 'err', '请求失败：' + e.message));
};

$('#btnFinish').onclick = () => {
  const p = payload();
  if (!p.host || !p.user || !p.db) return show('#finMsg', 'err', '数据库信息没填完');
  const pw1 = $('#f_pw1').value, pw2 = $('#f_pw2').value;
  if (pw1.length < 6) return show('#finMsg', 'err', '密码至少 6 位');
  if (pw1 !== pw2) return show('#finMsg', 'err', '两次输入的密码不一致');

  $('#btnFinish').disabled = true;
  show('#finMsg', 'warn', '正在初始化，请稍候…');
  const fd = new FormData();
  Object.entries(p).forEach(([k, v]) => fd.append(k, v));
  fd.append('console_pass', pw1);
  fetch('setup.php?action=finish', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) { $('#btnFinish').disabled = false; return show('#finMsg', 'err', '✗ ' + d.msg); }
      show('#finMsg', 'ok', '✓ 初始化完成，正在进入控制台…');
      setTimeout(() => location.href = 'index.php', 900);
    })
    .catch(e => { $('#btnFinish').disabled = false; show('#finMsg', 'err', '请求失败：' + e.message); });
};
</script>
</body>
</html>
