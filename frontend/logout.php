<?php
/**
 * 退出登录。
 *
 * 单独成文件是为了能用 <a href> 直接点（前端顶栏的「退出」按钮），
 * 避免在 JS 里拼一个 POST 请求。
 */
require_once __DIR__ . '/auth.php';

auth_logout();
header('Location: login.php');
exit;
