-- 闲鱼卖家客服 · 网页端数据库初始化
-- 数据库：xianyu_autoreply （独立库，不碰现有 game_db）
-- 字符集：utf8mb4（支持 emoji 等 4 字节字符）
-- 适用：MySQL 5.7.26（phpStudy）

CREATE DATABASE IF NOT EXISTS xianyu_autoreply DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE xianyu_autoreply;

-- ============ 全局设置（单行） ============
DROP TABLE IF EXISTS settings;
CREATE TABLE settings (
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
  selectors       JSON         NULL,
  engine_running  TINYINT(1)   NOT NULL DEFAULT 0,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 自动回复规则 ============
DROP TABLE IF EXISTS rules;
CREATE TABLE rules (
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
DROP TABLE IF EXISTS conversations;
CREATE TABLE conversations (
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
DROP TABLE IF EXISTS messages;
CREATE TABLE messages (
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
DROP TABLE IF EXISTS card_products;
CREATE TABLE card_products (
  id            INT          NOT NULL AUTO_INCREMENT,
  product_name  VARCHAR(128) NOT NULL,
  keywords      JSON         NOT NULL COMMENT '订单卡片命中时的商品识别关键词',
  enabled       TINYINT(1)   NOT NULL DEFAULT 1,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 卡密库存（一次性） ============
DROP TABLE IF EXISTS card_inventory;
CREATE TABLE card_inventory (
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
DROP TABLE IF EXISTS deliveries;
CREATE TABLE deliveries (
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
DROP TABLE IF EXISTS activity_log;
CREATE TABLE activity_log (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  level       VARCHAR(16)  NOT NULL DEFAULT 'INFO',
  message     TEXT         NOT NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 命令队列（网页 -> 引擎） ============
DROP TABLE IF EXISTS commands;
CREATE TABLE commands (
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
DROP TABLE IF EXISTS engine_status;
CREATE TABLE engine_status (
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
DROP TABLE IF EXISTS shop_items;
CREATE TABLE shop_items (
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
DROP TABLE IF EXISTS card_bundles;
CREATE TABLE card_bundles (
  id          INT          NOT NULL AUTO_INCREMENT,
  name        VARCHAR(128) NOT NULL COMMENT '卡卷名称',
  content     MEDIUMTEXT   NOT NULL COMMENT '卡卷具体内容（每行一条卡密/链接）',
  enabled     TINYINT(1)   NOT NULL DEFAULT 1,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ============ 商品-卡卷绑定（多对多，2026-10-17 新增） ============
DROP TABLE IF EXISTS item_bundles;
CREATE TABLE item_bundles (
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

INSERT INTO rules (keywords, reply, priority) VALUES
('["在吗","在么","有人吗","在不在","你好","您好"]', '在的～看中哪款虚拟资源？付款后自动发货，秒到！', 10),
('["怎么用","使用教程","不会用","教程","怎么兑换","如何使用"]', '卡密/链接在订单里，复制后打开对应页面兑换即可，不会操作随时喊我', 9),
('["提取码","网盘","链接","卡密","兑换码","在哪看"]', '发货内容含链接+提取码（或兑换码），按说明操作即可，查收后麻烦确认收货哦', 8),
('["多久到账","多久发","什么时候发","发货时间"]', '虚拟商品付款后秒发，系统自动发货，稍等几秒即可', 7),
('["拍了没发货","没收到","未发货","没到账","没发"]', '请稍等或刷新订单页，自动发货偶尔延迟几秒；仍未收到联系我手动补发', 6),
('["能便宜","少点","抹零","优惠","便宜点"]', '虚拟商品价格已是实价，多拍有优惠，拍前先跟我说', 5),
('["人工","客服","转人工"]', '我在的，直接说问题就行', 4),
('["成色","几成新","用了多久","瑕疵"]', '这是虚拟商品（卡密/网盘资源），全新未使用，一物一码', 3);

INSERT INTO engine_status (id, state) VALUES (1, 'stopped')
ON DUPLICATE KEY UPDATE id=id;
