-- 闲鱼卖家客服 · 网页端数据库初始化（安全版，可重复执行）
-- 数据库：使用连接当前选中的库（不再硬编码 xianyu_autoreply，
--         避免用户填了别的库名时把表建到错误的库里）
-- 字符集：utf8mb4（支持 emoji 等 4 字节字符）
-- 适用：MySQL 5.7.26（phpStudy）
--
-- 【重要】本脚本不含任何 DROP 语句：重复执行不会删除已有数据。
--         如需彻底重建，请手动 DROP DATABASE。

-- ============ 全局设置（单行） ============

CREATE TABLE IF NOT EXISTS settings (
  id              INT          NOT NULL DEFAULT 1,
  cdp_port        INT          NOT NULL DEFAULT 9222,
  poll_interval   FLOAT        NOT NULL DEFAULT 3.0,
  max_replies_per_hour INT     NOT NULL DEFAULT 60,
  dry_run         TINYINT(1)   NOT NULL DEFAULT 0,
  rate_limit_enabled TINYINT(1) NOT NULL DEFAULT 1,
  rate_limit_per_user_hour INT  NOT NULL DEFAULT 3,
  rate_limit_window_sec    INT  NOT NULL DEFAULT 120,
  scan_enabled   TINYINT(1) NOT NULL DEFAULT 0,
  scan_scope     INT       NOT NULL DEFAULT 10,
  scan_interval  INT       NOT NULL DEFAULT 180,
  -- 批量上传节奏（原先漏了这两列，新装库一保存设置就报
  --  Unknown column 'batch_interval_min'，2026-10-29 修复）
  batch_interval_min INT   NOT NULL DEFAULT 0,
  batch_daily_max    INT   NOT NULL DEFAULT 0,
  -- 登录失败限流开关（1=开启，连错 5 次锁 5 分钟）
  login_guard_enabled TINYINT(1) NOT NULL DEFAULT 1,
  selectors       JSON         NULL,
  engine_running  TINYINT(1)   NOT NULL DEFAULT 0,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 自动回复规则 ============

CREATE TABLE IF NOT EXISTS rules (
  id          INT          NOT NULL AUTO_INCREMENT,
  keywords    JSON         NOT NULL COMMENT '命中关键词数组，任一命中即触发',
  reply       TEXT         NOT NULL COMMENT '要发送的内容',
  enabled     TINYINT(1)   NOT NULL DEFAULT 1,
  priority    INT          NOT NULL DEFAULT 0 COMMENT '数值越大越靠前匹配',
  hit_count   INT          NOT NULL DEFAULT 0 COMMENT '累计命中次数',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_enabled_priority (enabled, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 会话快照 ============

CREATE TABLE IF NOT EXISTS conversations (
  id          INT          NOT NULL AUTO_INCREMENT,
  buyer_name  VARCHAR(128) NOT NULL COMMENT '买家昵称',
  preview     VARCHAR(512) NULL COMMENT '列表预览文本',
  unread      TINYINT(1)   NOT NULL DEFAULT 0,
  last_msg    TEXT         NULL,
  last_active DATETIME     NULL,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_buyer (buyer_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 消息记录 ============

CREATE TABLE IF NOT EXISTS messages (
  id              BIGINT       NOT NULL AUTO_INCREMENT,
  conversation_id INT          NULL,
  buyer_name      VARCHAR(128) NULL,
  direction       ENUM('in','out') NOT NULL COMMENT 'in=买家发来, out=我们发出',
  content         TEXT         NOT NULL,
  is_order        TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '是否为订单卡片',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_buyer (buyer_name),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 卡密商品（一个商品对应一串卡密） ============

CREATE TABLE IF NOT EXISTS card_products (
  id            INT          NOT NULL AUTO_INCREMENT,
  product_name  VARCHAR(128) NOT NULL,
  keywords      JSON         NOT NULL COMMENT '订单卡片命中时的商品识别关键词',
  enabled       TINYINT(1)   NOT NULL DEFAULT 1,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 卡密库存（一次性） ============

CREATE TABLE IF NOT EXISTS card_inventory (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  product_id  INT          NOT NULL,
  code        TEXT         NOT NULL COMMENT '卡密/网盘链接',
  used        TINYINT(1)   NOT NULL DEFAULT 0,
  used_at     DATETIME     NULL,
  used_by     VARCHAR(128) NULL COMMENT '发给的买家',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_product_used (product_id, used)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 发货记录 ============

CREATE TABLE IF NOT EXISTS deliveries (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  order_id      VARCHAR(128) NULL COMMENT '订单标识（来自订单卡片）',
  buyer_name    VARCHAR(128) NOT NULL,
  product_id    INT          NULL,
  product_name  VARCHAR(128) NULL,
  code          TEXT         NOT NULL,
  status        VARCHAR(32)  NOT NULL DEFAULT 'sent' COMMENT 'sent/failed',
  platform_status VARCHAR(16) NULL DEFAULT NULL COMMENT '平台确认: pending/confirmed/failed',
  platform_confirmed_at DATETIME NULL DEFAULT NULL COMMENT '平台确认发货时间',
  note          TEXT         NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_buyer (buyer_name),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 运行日志 ============

CREATE TABLE IF NOT EXISTS activity_log (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  level       VARCHAR(16)  NOT NULL DEFAULT 'INFO',
  message     TEXT         NOT NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 命令队列（网页 -> 引擎） ============

CREATE TABLE IF NOT EXISTS commands (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  cmd           VARCHAR(32)  NOT NULL COMMENT 'start/stop/scan_now',
  payload       JSON         NULL,
  status        ENUM('pending','done','failed') NOT NULL DEFAULT 'pending',
  result        TEXT         NULL,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_at  DATETIME     NULL,
  PRIMARY KEY (id),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 引擎心跳/状态 ============

CREATE TABLE IF NOT EXISTS engine_status (
  id              INT          NOT NULL DEFAULT 1,
  pid             INT          NULL,
  state           VARCHAR(32)  NOT NULL DEFAULT 'stopped' COMMENT 'stopped/running/error',
  last_heartbeat  DATETIME     NULL,
  last_activity   DATETIME     NULL,
  started_at      DATETIME     NULL,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 店铺商品（从闲鱼个人主页采集，2026-10-17 新增） ============
-- 数据来源：tools/collect_shop_products.py -> data/collected_products.json，
-- 由网页端「获取商品」触发采集、进度条展示、「导入数据库」落库。

CREATE TABLE IF NOT EXISTS shop_items (
  id            INT          NOT NULL AUTO_INCREMENT,
  item_id       VARCHAR(32)  NOT NULL COMMENT '闲鱼商品ID（item?id=）',
  title         VARCHAR(255) NOT NULL COMMENT '商品标题',
  price         VARCHAR(32)  NULL COMMENT '现价（原始文本，如 ¥1.50）',
  origin_price  VARCHAR(32)  NULL COMMENT '原价/划线价',
  img           VARCHAR(512) NULL COMMENT '主图URL',
  descr         MEDIUMTEXT   NULL COMMENT '详情页简介全文',
  gallery       JSON         NULL COMMENT '图集URL数组',
  is_physical   TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=实物（与卡密业务无关）',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_item (item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 卡卷（2026-10-17 新增，原「卡密商品」演进为卡卷管理） ============
-- 卡卷 = 一组卡密/兑换码/网盘链接的打包；content 每行一条。
-- 商品通过 item_bundles 绑定一个或多个卡卷（多对多）。

CREATE TABLE IF NOT EXISTS card_bundles (
  id          INT          NOT NULL AUTO_INCREMENT,
  name        VARCHAR(128) NOT NULL COMMENT '卡卷名称',
  content     MEDIUMTEXT   NOT NULL COMMENT '卡卷具体内容（每行一条卡密/链接）',
  enabled     TINYINT(1)   NOT NULL DEFAULT 1,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 商品-卡卷绑定（多对多，2026-10-17 新增） ============

CREATE TABLE IF NOT EXISTS item_bundles (
  item_id   INT NOT NULL COMMENT 'shop_items.id',
  bundle_id INT NOT NULL COMMENT 'card_bundles.id',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (item_id, bundle_id),
  KEY idx_bundle (bundle_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 种子数据 ============
INSERT INTO settings (id, cdp_port, poll_interval, max_replies_per_hour, dry_run, rate_limit_enabled, rate_limit_per_user_hour, rate_limit_window_sec, scan_enabled, scan_scope, scan_interval, selectors, engine_running)
VALUES (1, 9222, 3.0, 60, 0, 1, 3, 120, 0, 10, 180,
'{"conversation_list":"[class*=\'conversation-item\']","unread_item":"[class*=\'ant-badge\']","input_box":"[class*=\'sendbox\'] textarea","send_button":"[class*=\'sendbox-bottom\'] button"}',
0)
ON DUPLICATE KEY UPDATE id=id;

-- 注意：这里**不再插入**默认回复规则。
-- rules 表没有唯一键，`INSERT IGNORE` 拦不住重复，
-- 而 ensure_schema() 每次引擎启动都会执行本脚本 ——
-- 原种子数据会让规则数每启动一次 +8 条（2026-10-29 实测：24 → 32）。
-- 新用户请在网页「回复规则」页自行添加；需要示例可参考 README。

INSERT IGNORE INTO engine_status (id, state) VALUES (1, 'stopped')
ON DUPLICATE KEY UPDATE id=id;
