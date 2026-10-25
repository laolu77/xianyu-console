<?php
require_once __DIR__ . '/auth.php';
// 未登录直接跳登录页：控制台结构（导航、按钮、线上状态）不该给访客看到
auth_require_page();

// 防止浏览器缓存旧版前端（改了 app.js 后强制让客户端重新拉取）
header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
header('Pragma: no-cache');
header('Expires: 0');
?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>闲鱼卖家客服 · 自动回复 / 自动发货控制台</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="assets/css/app.css">
</head>
<body>
  <div id="app">
    <aside class="sidebar">
      <div class="brand">
        <div class="logo">闲</div>
        <div class="brand-txt">
          <strong>闲鱼客服控制台</strong>
          <small>自动回复 · 卡密自动发货</small>
        </div>
      </div>
      <nav id="nav">
        <a data-page="dashboard" class="active">📊 仪表盘</a>
        <a data-page="rules">💬 回复规则</a>
        <a data-page="shopitems">🛍 商品列表</a>
        <a data-page="publish">📤 发布商品</a>
        <a data-page="batchpublish">📚 批量上传</a>
        <a data-page="products">🎴 卡卷管理</a>
        <a data-page="conversations">👥 会话 / 消息</a>
        <a data-page="deliveries">📦 订单管理</a>
        <a data-page="activity">📜 运行日志</a>
        <a data-page="settings">⚙️ 设置</a>
        <a data-page="tunnel">🌐 内网穿透</a>
      </nav>
      <div class="engine-box" id="engineBox">
        <div class="engine-state"><span class="dot" id="engDot"></span><span id="engText">检测中…</span></div>
        <div class="engine-btns">
          <button id="btnStart" class="btn btn-sm btn-primary">启动引擎</button>
          <button id="btnStop" class="btn btn-sm btn-ghost">停止</button>
          <button id="btnScan" class="btn btn-sm btn-ghost">立即扫描</button>
        </div>
      </div>
      <div class="engine-box" id="clientBox">
        <div class="engine-state"><span class="dot" id="appDot"></span><span id="appText">检测客户端…</span></div>
        <div class="engine-btns">
          <button id="btnAppStart" class="btn btn-sm btn-primary">启动客户端</button>
          <button id="btnAppStop" class="btn btn-sm btn-ghost">停止</button>
        </div>
      </div>
    </aside>

    <main class="main">
      <header class="topbar">
        <button class="nav-toggle" id="navToggle" aria-label="打开菜单">☰</button>
        <h1 id="pageTitle">仪表盘</h1>
        <div class="topbar-right">
          <span id="dryBadge" class="badge hidden">试运行模式</span>
          <button id="btnRefresh" class="btn btn-sm btn-ghost">刷新</button>
          <a href="logout.php" class="btn btn-sm btn-ghost" title="退出登录">退出</a>
        </div>
      </header>
      <section id="content" class="content"><div class="loading">加载中…</div></section>
    </main>
    <!-- 窄屏下侧栏变抽屉时的遮罩，点一下关闭 -->
    <div class="nav-mask" id="navMask"></div>
  </div>
  <div id="toast" class="toast"></div>
  <script src="assets/js/app.js?v=<?php echo filemtime(__DIR__ . '/assets/js/app.js'); ?>"></script>
</body>
</html>
