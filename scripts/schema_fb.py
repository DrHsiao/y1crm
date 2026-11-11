#!/usr/bin/env python3
"""
schema_fb.py — Facebook 粉專整合的資料表（fb_threads / fb_messages / fb_posts / fb_post_drafts）。

本專案沒有 migration 機制；這支把 DDL 固化進版控，CREATE TABLE IF NOT EXISTS 可重跑。
程式在 api/fb_page.py、api/fb_drafts.py；選單與權限在 scripts/seed_menus.py
（/fb-chat 權限鍵 customers、/fb-posts 權限鍵 web）。

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
  `status` enum('sent','dev','failed','deleted') NOT NULL COMMENT 'dev＝開發模式，沒有真的發出；deleted＝發出後又從粉專刪掉',
  `error` varchar(255) DEFAULT NULL,
  `created_by` varchar(45) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_fb_post_web` (`web_post_id`),
  KEY `idx_fb_post_fb` (`fb_post_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='官網文章分享到粉專的紀錄'
""",
    """
CREATE TABLE IF NOT EXISTS `fb_post_drafts` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `day` date NOT NULL COMMENT '哪一天的草稿',
  `category` varchar(20) NOT NULL COMMENT 'api/fb_drafts.py CATEGORIES',
  `topic_key` varchar(60) DEFAULT NULL COMMENT '題庫代號／news:網址雜湊／web:文章 id／ev:節日:年，避免重複出題',
  `topic` varchar(200) NOT NULL COMMENT '顯示用：題目或新聞標題',
  `brief` text NOT NULL COMMENT '交給本機 AI 的題材（重寫時再用一次）',
  `link` varchar(500) DEFAULT NULL COMMENT '發文時附的連結（官網或新聞原文）',
  `web_post_id` bigint unsigned DEFAULT NULL COMMENT 'y1web.web_posts.id（官網文章導讀）',
  `source_name` varchar(50) DEFAULT NULL COMMENT '新聞來源',
  `source_url` varchar(500) DEFAULT NULL,
  `message` text NOT NULL,
  `seo` json DEFAULT NULL COMMENT '同步到聽力專欄用：title／summary／category／keywords／slug',
  `web_sync` tinyint(1) NOT NULL DEFAULT 0 COMMENT '發佈時同步到官網聽力專欄',
  `web_article_id` bigint unsigned DEFAULT NULL COMMENT '同步後的 y1web.web_posts.id',
  `image` varchar(40) DEFAULT NULL COMMENT '配圖：y1web media/uploads 的檔名（本機 AI 畫的）；有圖就發相片貼文',
  `image_meta` json DEFAULT NULL COMMENT '配圖的畫面描述 {prompt（英文，給畫圖）, scene（中文，給人看）}',
  `card` varchar(40) DEFAULT NULL COMMENT '粉專圖卡（照片＋大字標題，api/post_card.py）：y1web media/uploads 的檔名；有就發這張',
  `card_meta` json DEFAULT NULL COMMENT '圖卡的 {title, sub, src（ai／text／staff）, image（用哪張照片做的）, at}',
  `picked` tinyint(1) NOT NULL DEFAULT 0 COMMENT '要發：每日草稿預設 1，到 auto_publish 時間沒取消就自動發',
  `status` enum('new','published','dev','discarded') NOT NULL DEFAULT 'new' COMMENT 'dev＝開發模式，沒有真的發出',
  `fb_post_id` varchar(120) DEFAULT NULL,
  `error` varchar(255) DEFAULT NULL COMMENT '上一次發佈失敗的原因',
  `meta` varchar(255) DEFAULT NULL COMMENT '模型、花了幾秒',
  `created_by` varchar(45) DEFAULT NULL COMMENT 'NULL＝每日排程；否則是按「多寫一則」的人',
  `published_by` varchar(45) DEFAULT NULL,
  `published_at` datetime DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_fbd_day` (`day`,`status`),
  KEY `idx_fbd_fb` (`fb_post_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='粉專貼文草稿（本機 AI 每天寫，管理者勾選才發）'
""",
]


# 後來加的欄位：已經建好的表補上（沒有才加，可重跑）
COLUMNS = [
    ("fb_post_drafts", "seo", "json DEFAULT NULL COMMENT '同步到聽力專欄用：title／summary／category／keywords／slug' AFTER `message`"),
    ("fb_post_drafts", "web_sync", "tinyint(1) NOT NULL DEFAULT 0 COMMENT '發佈時同步到官網聽力專欄' AFTER `seo`"),
    ("fb_post_drafts", "web_article_id", "bigint unsigned DEFAULT NULL COMMENT '同步後的 y1web.web_posts.id' AFTER `web_sync`"),
    # 加這欄之前寫的草稿一律 0（不會被自動發）
    ("fb_post_drafts", "picked", "tinyint(1) NOT NULL DEFAULT 0 COMMENT '要發：每日草稿預設 1，到 auto_publish 時間沒取消就自動發' AFTER `web_article_id`"),
    ("fb_post_drafts", "image", "varchar(40) DEFAULT NULL COMMENT '配圖：y1web media/uploads 的檔名（本機 AI 畫的）；有圖就發相片貼文' AFTER `web_article_id`"),
    ("fb_post_drafts", "image_meta", "json DEFAULT NULL COMMENT '配圖的畫面描述 {prompt（英文，給畫圖）, scene（中文，給人看）}' AFTER `image`"),
    # 11-09 John「粉專貼文的圖片建立新圖」→ 圖卡
    ("fb_post_drafts", "card", "varchar(40) DEFAULT NULL COMMENT '粉專圖卡（照片＋大字標題，api/post_card.py）：y1web media/uploads 的檔名；有就發這張' AFTER `image_meta`"),
    ("fb_post_drafts", "card_meta", "json DEFAULT NULL COMMENT '圖卡的 {title, sub, src（ai／text／staff）, image（用哪張照片做的）, at}' AFTER `card`"),
]

# 後來改的欄位型別：和現在的不一樣才改（可重跑）
MODIFY = [
    ("fb_posts", "status", "enum('sent','dev','failed','deleted') NOT NULL COMMENT 'dev＝開發模式，沒有真的發出；deleted＝發出後又從粉專刪掉'"),
]


def main() -> int:
    db = dbpool.connect(**config.db_params(),
                         cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    with db, db.cursor() as cur:
        for ddl in DDL:
            cur.execute(ddl)
        for table, col, spec in COLUMNS:
            cur.execute("SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                        "AND TABLE_NAME=%s AND COLUMN_NAME=%s", (table, col))
            if cur.fetchone() is None:
                cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {spec}")
                print(f"補欄位 {table}.{col}")
        for table, col, spec in MODIFY:
            cur.execute("SELECT COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                        "AND TABLE_NAME=%s AND COLUMN_NAME=%s", (table, col))
            r = cur.fetchone()
            if r and r["COLUMN_TYPE"] != spec.split(" NOT NULL")[0]:
                cur.execute(f"ALTER TABLE `{table}` MODIFY COLUMN `{col}` {spec}")
                print(f"改欄位 {table}.{col}")
        cur.execute("SHOW TABLES LIKE 'fb\\_%%'")
        print("已就緒：", ", ".join(next(iter(r.values())) for r in cur.fetchall()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
