#!/usr/bin/env python3
"""
schema_fb.py — Facebook 粉專整合的資料表（fb_threads / fb_messages / fb_posts）。

本專案沒有 migration 機制；這支把 DDL 固化進版控，CREATE TABLE IF NOT EXISTS 可重跑。
程式在 api/fb_page.py；選單與權限在 scripts/seed_menus.py（/fb-chat、權限鍵 customers）。

用法（要用專案的 venv）：
    ./venv/bin/python scripts/schema_fb.py
"""
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from api import dbpool  # noqa: E402

DDL = [
    """
CREATE TABLE IF NOT EXISTS `fb_threads` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `kind` enum('messenger','comment') NOT NULL COMMENT 'Messenger 私訊／粉專貼文留言',
  `ext_id` varchar(120) NOT NULL COMMENT 'messenger＝客人的 PSID；comment＝第一層留言 id（底下的回覆都併進來）',
  `post_id` varchar(120) DEFAULT NULL COMMENT '留言所在的粉專貼文 id',
  `person_id` varchar(64) DEFAULT NULL,
  `person_name` varchar(100) DEFAULT NULL,
  `mode` enum('bot','human') NOT NULL DEFAULT 'bot' COMMENT '只對 messenger 有意義：human＝AI 靜音',
  `human_until` datetime DEFAULT NULL,
  `assigned_to` varchar(45) DEFAULT NULL,
  `handoff_reason` varchar(30) DEFAULT NULL,
  `last_in_at` datetime DEFAULT NULL COMMENT '客人最後一則（Messenger 24 小時回覆視窗從這裡算）',
  `last_out_at` datetime DEFAULT NULL COMMENT '最後一則實際送出的回覆',
  `handled_at` datetime DEFAULT NULL COMMENT '店員按「不用回」的時間',
  `last_msg_at` datetime DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_fb_thread` (`kind`,`ext_id`),
  KEY `idx_fb_thread_last` (`last_msg_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='粉專對話（Messenger 一人一串、留言一則一串）'
""",
    """
CREATE TABLE IF NOT EXISTS `fb_messages` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `thread_id` bigint unsigned NOT NULL,
  `direction` enum('in','out') NOT NULL,
  `source` enum('customer','ai','ai_draft','staff','system') NOT NULL,
  `ext_mid` varchar(120) DEFAULT NULL COMMENT 'Facebook 的訊息／留言 id（webhook 重送與自己送出的回音靠它去重）',
  `text` text NOT NULL,
  `status` enum('new','sent','discarded','failed') NOT NULL DEFAULT 'new',
  `meta` varchar(255) DEFAULT NULL,
  `created_by` varchar(45) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_fb_mid` (`ext_mid`),
  KEY `idx_fb_msg_thread` (`thread_id`,`id`),
  CONSTRAINT `fk_fb_msg_thread` FOREIGN KEY (`thread_id`) REFERENCES `fb_threads` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='粉專對話的每一則訊息'
""",
    """
CREATE TABLE IF NOT EXISTS `fb_posts` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `web_post_id` bigint unsigned DEFAULT NULL COMMENT 'y1web.web_posts.id（官網文章）',
  `fb_post_id` varchar(120) DEFAULT NULL,
  `message` text NOT NULL,
  `link` varchar(500) NOT NULL,
  `status` enum('sent','dev','failed') NOT NULL COMMENT 'dev＝開發模式，沒有真的發出',
  `error` varchar(255) DEFAULT NULL,
  `created_by` varchar(45) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_fb_post_web` (`web_post_id`),
  KEY `idx_fb_post_fb` (`fb_post_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='官網文章分享到粉專的紀錄'
""",
]


def main() -> int:
    db = dbpool.connect(**config.db_params(),
                         cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    with db, db.cursor() as cur:
        for ddl in DDL:
            cur.execute(ddl)
        cur.execute("SHOW TABLES LIKE 'fb\\_%%'")
        print("已就緒：", ", ".join(next(iter(r.values())) for r in cur.fetchall()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
