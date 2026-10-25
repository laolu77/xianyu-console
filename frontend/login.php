<?php
/**
 * 登录页。
 *
 * 为什么单独成文件而不是塞进 index.php：未登录时页面不该输出任何控制台
 * 结构（导航、按钮、内嵌的引擎状态），否则等于把功能清单白送给访客。
 */
require_once __DIR__ . '/auth.php';

header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');

if (auth_check()) {                       // 已登录就别停在登录页
    header('Location: index.php');
    exit;
}

$err = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $u = trim((string)($_POST['user'] ?? ''));
    $p = (string)($_POST['pass'] ?? '');
    $r = auth_attempt($u, $p);
    if ($r['ok']) {
        header('Location: index.php');
        exit;
    }
    $err = $r['msg'];
}
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>登录 · 闲鱼客服控制台</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root{
      --brand:#2f6df6; --brand-d:#1f4fc4; --err:#e0473e;
      --ink:#1f2733; --muted:#6b7686; --line:#e6eaf2;
    }
    *{box-sizing:border-box}
    body{
      margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      font-family:"Microsoft YaHei","PingFang SC",system-ui,Segoe UI,Arial,sans-serif;
      background:radial-gradient(1200px 600px at 20% 0%, #1b2b52 0%, #0f1830 55%, #0a1020 100%);
      color:var(--ink);
    }
    .card{
      width:min(380px,92vw);background:#fff;border-radius:16px;padding:34px 30px 28px;
      box-shadow:0 24px 60px rgba(6,12,28,.45);
    }
    .brand{display:flex;align-items:center;gap:12px;margin-bottom:22px}
    .logo{
      width:44px;height:44px;border-radius:12px;flex:0 0 auto;
      background:linear-gradient(135deg,#3b82f6,#22d3ee);
      color:#fff;font-weight:700;font-size:20px;
      display:flex;align-items:center;justify-content:center;
    }
    .brand h1{font-size:16px;margin:0 0 3px}
    .brand p{font-size:12px;color:var(--muted);margin:0}
    label{display:block;font-size:12px;color:var(--muted);margin:14px 0 6px}
    input[type=text],input[type=password]{
      width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:9px;
      font-size:14px;outline:none;transition:.15s;background:#fbfcfe;
    }
    input[type=text]:focus,input[type=password]:focus{
      border-color:var(--brand);background:#fff;box-shadow:0 0 0 3px rgba(47,109,246,.12);
    }
    button{
      width:100%;margin-top:20px;padding:12px;border:0;border-radius:9px;cursor:pointer;
      background:var(--brand);color:#fff;font-size:15px;font-weight:600;transition:.15s;
    }
    button:hover{background:var(--brand-d)}
    button:active{transform:translateY(1px)}
    .err{
      margin-top:16px;padding:10px 12px;border-radius:8px;font-size:13px;
      background:#fdecea;color:var(--err);border:1px solid #f8cfcb;
    }
    .foot{margin-top:18px;font-size:11px;color:#9aa4b4;text-align:center;line-height:1.6}
  </style>
</head>
<body>
  <form class="card" method="post" autocomplete="off">
    <div class="brand">
      <div class="logo">闲</div>
      <div>
        <h1>闲鱼客服控制台</h1>
        <p>自动回复 · 卡密自动发货</p>
      </div>
    </div>

    <label for="user">账号</label>
    <input id="user" name="user" type="text" required autofocus
           value="<?php echo htmlspecialchars($_POST['user'] ?? '', ENT_QUOTES); ?>">

    <label for="pass">密码</label>
    <input id="pass" name="pass" type="password" required>

    <?php if ($err !== ''): ?>
      <div class="err"><?php echo htmlspecialchars($err, ENT_QUOTES); ?></div>
    <?php endif; ?>

    <button type="submit">登 录</button>
    <div class="foot">连续输错 5 次会锁定 5 分钟</div>
  </form>
</body>
</html>
