#!/usr/bin/env python3
"""
schema_hearing.py — 聽檢輔助的資料表（hearing_tests / hearing_test_files）。

本專案沒有 migration 機制；這支把 DDL 固化進版控，CREATE TABLE IF NOT EXISTS 可重跑。
選單與權限在 scripts/seed_menus.py（/hearing、權限鍵 hearing）。

用法（要用專案的 venv）：
    ./venv/bin/python scripts/schema_hearing.py
"""
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from api import dbpool  # noqa: E402

DDL = [
    """
CREATE TABLE IF NOT EXISTS `hearing_tests` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `customer_id` bigint unsigned NOT NULL,
  `test_date` date NOT NULL,
  `mode` enum('adult','child') NOT NULL DEFAULT 'adult' COMMENT '成人/兒童（決定測試順序）',
  `transducer` varchar(10) NOT NULL DEFAULT 'DD45' COMMENT '氣導耳機（決定耳間衰減 IA）',
  `first_ear` enum('R','L') NOT NULL DEFAULT 'R',
  `talk` enum('ok','poor') NOT NULL DEFAULT 'ok' COMMENT '對談狀況（決定起始音量）',
  `data` json NOT NULL COMMENT '聽閾/重測/鼓室圖/語音，格式見 api/hearing.py',
  `summary` varchar(200) DEFAULT NULL COMMENT '存檔當下的判讀摘要（列表顯示用）',
  `rules_version` varchar(30) DEFAULT NULL COMMENT '判讀規則版本（api/hearing.RULES_VERSION）',
  `notes` varchar(1000) DEFAULT NULL,
  `created_by` varchar(45) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_by` varchar(45) DEFAULT NULL,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_hearing_customer` (`customer_id`,`test_date`),
  CONSTRAINT `fk_hearing_customer` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='聽力檢查紀錄（聽檢輔助）'
""",
    """
CREATE TABLE IF NOT EXISTS `hearing_test_files` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `test_id` bigint unsigned NOT NULL,
  `filename` varchar(255) DEFAULT NULL,
  `rel_path` varchar(255) NOT NULL COMMENT '相對 storage.base_dir（hearing/{test_id}/…）',
  `mime` varchar(50) NOT NULL,
  `bytes_size` int unsigned NOT NULL,
  `created_by` varchar(45) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_hfile_test` (`test_id`),
  CONSTRAINT `fk_hfile_test` FOREIGN KEY (`test_id`) REFERENCES `hearing_tests` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='聽檢原始檔（MA42 螢幕照片／PDF 報告，檔案存磁碟）'
""",
]


def main() -> int:
    db = dbpool.connect(**config.db_params(),
                         cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    with db, db.cursor() as cur:
        for ddl in DDL:
            cur.execute(ddl)
        cur.execute("SHOW TABLES LIKE 'hearing%%'")
        print("已就緒：", ", ".join(next(iter(r.values())) for r in cur.fetchall()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
