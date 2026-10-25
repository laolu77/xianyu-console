<?php
/**
 * 登录认证（会话 + 密码哈希 + 失败限流）。
 *
 * 为什么单独成文件：index.php（页面）和 api.php（接口）都要校验，
 * 认证逻辑放一处才不会出现"页面要登录、接口却裸奔"的漏洞。
 *
 * 密码不存明文：落库前用 password_hash()（bcrypt）存哈希，
 * 文件权限设 600，同机其他用户读不到。
 */

// ---------------- 会话 ----------------
if (session_status() === PHP_SESSION_NONE) {
    // httponly 防 JS 偷 cookie；samesite=Lax 防跨站伪造请求
    @ini_set('session.cookie_httponly', '1');
    @ini_set('session.cookie_samesite', 'Lax');
    @ini_set('session.use_strict_mode', '1');
    session_start();
}

define('XY_AUTH_DIR', realpath(__DIR__ . '/../data') ?: (__DIR__ . '/../data'));
define('XY_AUTH_FILE', XY_AUTH_DIR . '/auth.json');
define('XY_AUTH_FAIL', XY_AUTH_DIR . '/auth_fail.json');
define('XY_AUTH_DEFAULT_USER', 'admin');
define('XY_AUTH_DEFAULT_PASS', '');  // 兜底空密码：正常流程由安装向导写 auth.json。
// 注意：绝不能把作者机器的密码写在这里 —— 那等于给所有用户一个通用口令。
define('XY_AUTH_MAX_FAIL', 5);                      // 连续失败几次锁
define('XY_AUTH_LOCK_SEC', 300);                    // 锁定 5 分钟

/**
 * 读取（必要时初始化）账号配置。
 *
 * @return array{user:string,hash:string,updated_at:string}
 */
function auth_config(): array {
    if (is_file(XY_AUTH_FILE)) {
        $d = json_decode((string)file_get_contents(XY_AUTH_FILE), true);
        if (is_array($d) && !empty($d['user']) && !empty($d['hash'])) {
            return $d;
        }
    }
    // 首次运行：用默认账号建一份
    @mkdir(XY_AUTH_DIR, 0777, true);
    // 没有任何配置时的兜底：生成**随机口令**并落到提示文件。
    // 为什么不用固定密码：写死作者密码等于给所有用户一个通用口令，
    // 开了内网穿透就是人人可进；而空密码同样危险（2026-10-23 清理）。
    $pass = XY_AUTH_DEFAULT_PASS;
    if ($pass === '') {
        $pass = bin2hex(random_bytes(4));
        @file_put_contents(XY_AUTH_DIR . '/首次密码.txt',
            "控制台账号：admin\n初始密码：{$pass}\n"
            . "（登录后请在「设置」里改掉，并删除本文件）\n");
        @chmod(XY_AUTH_DIR . '/首次密码.txt', 0600);
    }
    $cfg = [
        'user'       => XY_AUTH_DEFAULT_USER,
        'hash'       => password_hash($pass, PASSWORD_DEFAULT),
        'updated_at' => date('Y-m-d H:i:s'),
    ];
    @file_put_contents(XY_AUTH_FILE,
        json_encode($cfg, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
    @chmod(XY_AUTH_FILE, 0600);
    return $cfg;
}

/**
 * 修改登录密码。
 *
 * @param string $old      当前密码
 * @param string $new      新密码（至少 6 位）
 * @return array{ok:bool,msg:string}
 */
function auth_change_password(string $old, string $new): array {
    $cfg = auth_config();
    // 新密码已 hash 化后无法还原，只能拿旧密码 verify
    if (!password_verify($old, $cfg['hash'])) {
        return ['ok' => false, 'msg' => '当前密码不正确'];
    }
    if (strlen($new) < 6) {
        return ['ok' => false, 'msg' => '新密码至少 6 位'];
    }
    $cfg['hash'] = password_hash($new, PASSWORD_DEFAULT);
    $cfg['updated_at'] = date('Y-m-d H:i:s');
    @file_put_contents(XY_AUTH_FILE,
        json_encode($cfg, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
    @chmod(XY_AUTH_FILE, 0600);
    return ['ok' => true, 'msg' => '密码已修改'];
}

/** 当前是否已登录。 */
function auth_check(): bool {
    return !empty($_SESSION['xy_admin']);
}

/** 登录成功后的会话登记。 */
function auth_mark_login(string $user): void {
    $_SESSION['xy_admin'] = $user;
    $_SESSION['xy_login_at'] = time();
    session_regenerate_id(true);         // 防会话固定攻击
}

/** 退出登录。 */
function auth_logout(): void {
    $_SESSION = [];
    if (ini_get('session.use_cookies')) {
        $p = session_get_cookie_params();
        setcookie(session_name(), '', time() - 42000,
            $p['path'], $p['domain'], (bool)$p['secure'], (bool)$p['httponly']);
    }
    @session_destroy();
}

/** 客户端 IP（够用即可，不追求穿透代理）。 */
function auth_ip(): string {
    return (string)($_SERVER['REMOTE_ADDR'] ?? '0.0.0.0');
}

/**
 * 登录失败限流是否启用（可在网页「设置」里开关）。
 *
 * 存在 settings 表的 login_guard_enabled 列（默认 1=开启）。
 * 查不到（库连不上/老库没这列）一律按**开启**处理 —— 安全策略失败时要偏严。
 *
 * 为什么走数据库而不是本地文件：开关是用户在设置页改的，改完要立刻生效，
 * 而设置页写的就是 settings 表；再放一份文件会出现两处真相不一致。
 */
function auth_guard_enabled(): bool {
    static $cached = null;
    if ($cached !== null) return $cached;
    $cached = true;
    try {
        require_once __DIR__ . '/db.php';
        $link = xy_raw_connect();
        if ($link) {
            $r = @mysqli_query($link, 'SELECT login_guard_enabled FROM settings WHERE id=1');
            if ($r) {
                $row = mysqli_fetch_assoc($r);
                if (is_array($row) && array_key_exists('login_guard_enabled', $row)) {
                    $cached = ((int)$row['login_guard_enabled']) === 1;
                }
            }
        }
    } catch (Throwable $e) {
        // 忽略：保持默认开启
    }
    return $cached;
}

/**
 * 失败限流：同一 IP 连续失败 5 次锁 5 分钟。
 *
 * 为什么要它：暴露到公网后，密码再强也架不住脚本无限猜。
 *
 * @return int 还需等待的秒数（0 = 没被锁）
 */
function auth_locked_for(): int {
    if (!is_file(XY_AUTH_FAIL)) return 0;
    $d = json_decode((string)file_get_contents(XY_AUTH_FAIL), true) ?: [];
    $rec = $d[auth_ip()] ?? null;
    if (!$rec) return 0;
    $until = (int)($rec['until'] ?? 0);
    return $until > time() ? $until - time() : 0;
}

/** 记录一次失败；达到阈值就上锁。 */
function auth_note_fail(): void {
    $d = [];
    if (is_file(XY_AUTH_FAIL)) {
        $d = json_decode((string)file_get_contents(XY_AUTH_FAIL), true) ?: [];
    }
    $ip = auth_ip();
    $cnt = (int)($d[$ip]['count'] ?? 0) + 1;
    $d[$ip] = ['count' => $cnt, 'until' => 0];
    if ($cnt >= XY_AUTH_MAX_FAIL) {
        $d[$ip] = ['count' => 0, 'until' => time() + XY_AUTH_LOCK_SEC];
    }
    // 顺手清理过期条目，别让文件无限长
    $now = time();
    foreach ($d as $k => $v) {
        if ((int)($v['until'] ?? 0) > 0 && (int)$v['until'] < $now) unset($d[$k]);
    }
    @file_put_contents(XY_AUTH_FAIL, json_encode($d));
    @chmod(XY_AUTH_FAIL, 0600);
}

/** 登录成功：清掉该 IP 的失败计数。 */
function auth_clear_fail(): void {
    if (!is_file(XY_AUTH_FAIL)) return;
    $d = json_decode((string)file_get_contents(XY_AUTH_FAIL), true) ?: [];
    unset($d[auth_ip()]);
    @file_put_contents(XY_AUTH_FAIL, json_encode($d));
}

/**
 * 尝试登录。
 *
 * @param string $user 账号
 * @param string $pass 明文密码
 * @return array{ok:bool,msg:string}
 */
function auth_attempt(string $user, string $pass): array {
    $guard = auth_guard_enabled();          // 设置页里可关闭
    if ($guard) {
        $left = auth_locked_for();
        if ($left > 0) {
            return ['ok' => false, 'msg' => '失败次数过多，请 ' . ceil($left / 60) . ' 分钟后再试'];
        }
    }
    $cfg = auth_config();
    if ($user === $cfg['user'] && password_verify($pass, $cfg['hash'])) {
        auth_clear_fail();
        auth_mark_login($user);
        return ['ok' => true, 'msg' => '登录成功'];
    }
    if ($guard) auth_note_fail();           // 关闭时连计数都不写，避免留下脏数据
    return ['ok' => false, 'msg' => '账号或密码错误'];
}

/** 页面用：未登录直接跳登录页。 */
function auth_require_page(): void {
    if (!auth_check()) {
        header('Location: login.php');
        exit;
    }
}

/** 接口用：未登录返回 401 JSON 并结束。 */
function auth_require_api(): void {
    if (auth_check()) return;
    http_response_code(401);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(['ok' => false, 'msg' => '未登录或登录已过期，请重新登录',
                      'need_login' => true], JSON_UNESCAPED_UNICODE);
    exit;
}
