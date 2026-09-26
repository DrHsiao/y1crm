#!/usr/bin/env python3
"""
seed_menus.py — 導覽選單與選單權限的種子資料（menus / menu_permissions）。

本專案沒有 migration 機制，選單一向是直接對 DB 操作的，換機器部署就會漏掉。
這支把「目前正式環境的選單結構」固化進版控，可重跑不重複。

用法（要用專案的 venv，才有 pymysql 與 config）：
    ./venv/bin/python scripts/seed_menus.py            # 補齊缺的，不動既有列
    ./venv/bin/python scripts/seed_menus.py --dry-run  # 只列出會做什麼
    ./venv/bin/python scripts/seed_menus.py --sync     # 另把既有列的圖示/路徑/排序校正回本檔

預設**只新增、不修改、不刪除**：因為 README 寫明「改選單＝直接改 menus 表」，
店裡若手動調過標題或順序，重跑種子不該把它蓋掉。要以本檔為準時才加 --sync。

⚠ 未涵蓋 `codes` 表（category=menu 的權限鍵中文名、category=level 的層級清單，
  以及其餘業務代碼）。那是整張表的事，另外處理。
"""
import argparse
import sys
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from api import dbpool  # noqa: E402  連線池（2026-09-26）

# ── 選單結構（與正式環境一致）──────────────────────────────
#   群組沒有 path 與 perm_key（可見性由子項決定，無可見子項會自動隱藏）；
#   葉節點的 perm_key 對到 menu_permissions.menu_key。
#   行事曆刻意不在選單裡：品牌 logo 就是它的入口。
MENUS = [
    {"title": "客戶", "icon": "👥", "sort": 10, "children": [
        {"title": "客戶資料",  "icon": "📇", "path": "/customers", "perm": "customers", "sort": 11},
        {"title": "線上預約",  "icon": "💬", "path": "/bookings",  "perm": "customers", "sort": 12},
        {"title": "LINE 客服", "icon": "🤖", "path": "/line-chat", "perm": "customers", "sort": 13},
    ]},
    {"title": "交易", "icon": "💰", "sort": 20, "children": [
        {"title": "交易記錄", "icon": "🧾", "path": "/purchases", "perm": "purchases", "sort": 21},
        {"title": "商品管理", "icon": "📦", "path": "/products",  "perm": "products",  "sort": 22},
    ]},
    {"title": "系統", "icon": "⚙️", "sort": 30, "children": [
        {"title": "人員管理", "icon": "👤", "path": "/staff", "perm": "staff", "sort": 31},
    ]},
]

# ── 選單權限（menu_key × level）────────────────────────────
#   calendar 不在選單樹裡，但 /api/calendar 等端點仍以它做權限檢查，故一併種下。
PERMISSIONS = {
    "calendar":  ["admin", "manager", "sales", "user"],
    "customers": ["admin", "manager", "sales", "user"],
    "purchases": ["admin", "manager", "sales"],
    "products":  ["admin", "manager"],
    "staff":     ["admin"],
}

_DDL_MENUS = """
CREATE TABLE IF NOT EXISTS `menus` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `parent_id` int unsigned DEFAULT NULL COMMENT '上層選單（NULL=主選單）',
  `title` varchar(30) NOT NULL COMMENT '顯示名稱',
  `icon` varchar(8) DEFAULT NULL COMMENT 'emoji 圖示',
  `path` varchar(100) DEFAULT NULL COMMENT '前端路徑（群組可空）',
  `perm_key` varchar(30) DEFAULT NULL COMMENT '權限鍵→menu_permissions.menu_key（群組空＝依子項）',
  `sort_order` int NOT NULL DEFAULT 0,
  `is_active` tinyint(1) NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  KEY `fk_menus_parent` (`parent_id`),
  CONSTRAINT `fk_menus_parent` FOREIGN KEY (`parent_id`) REFERENCES `menus` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='導覽選單（多層，資料表管理）'
"""

_DDL_PERMS = """
CREATE TABLE IF NOT EXISTS `menu_permissions` (
  `id` int unsigned NOT NULL AUTO_INCREMENT,
  `menu_key` varchar(30) NOT NULL COMMENT '選單代碼(codes.menu)',
  `level` varchar(20) NOT NULL COMMENT '使用者層級(users.level)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_menu_level` (`menu_key`,`level`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='選單權限：哪個層級可用哪個選單'
"""


class Seeder:
    def __init__(self, cur, dry_run: bool, sync: bool):
        self.cur, self.dry_run, self.sync = cur, dry_run, sync
        self.added = self.updated = self.kept = 0

    def _say(self, mark: str, what: str) -> None:
        print(f"  {'[乾跑] ' if self.dry_run else ''}{mark} {what}")

    def _fields_differ(self, row: dict, want: dict) -> list:
        return [k for k, v in want.items() if row.get(k) != v]

    def _update(self, mid: int, want: dict, label: str) -> None:
        """--sync 時把既有列校正回本檔（預設不做，以免蓋掉店裡的手動調整）。"""
        diff = self._fields_differ(self.cur_row, want)
        if not diff:
            self.kept += 1
            return
        if not self.sync:
            self.kept += 1
            self._say("=", f"{label}（已存在，{'/'.join(diff)} 與本檔不同，加 --sync 才校正）")
            return
        sets = ", ".join(f"`{k}`=%s" for k in diff)
        if not self.dry_run:
            self.cur.execute(f"UPDATE menus SET {sets} WHERE id=%s",
                             [want[k] for k in diff] + [mid])
        self.updated += 1
        self._say("~", f"{label}（校正 {'/'.join(diff)}）")

    def group(self, g: dict) -> int:
        want = {"icon": g["icon"], "sort_order": g["sort"], "is_active": 1}
        self.cur.execute("SELECT * FROM menus WHERE parent_id IS NULL AND title=%s", (g["title"],))
        row = self.cur.fetchone()
        if row:
            self.cur_row = row
            self._update(row["id"], want, f"群組「{g['title']}」")
            return row["id"]
        if self.dry_run:
            self.added += 1
            self._say("+", f"群組「{g['title']}」")
            return -1                      # 乾跑：子項用假 id，不會真的寫入
        self.cur.execute(
            "INSERT INTO menus (parent_id,title,icon,path,perm_key,sort_order,is_active) "
            "VALUES (NULL,%s,%s,NULL,NULL,%s,1)", (g["title"], g["icon"], g["sort"]))
        self.added += 1
        self._say("+", f"群組「{g['title']}」")
        return self.cur.lastrowid          # ⚠ 緊接 INSERT 讀，別被後續語句蓋掉

    def leaf(self, parent_id: int, m: dict) -> None:
        # 以 path 當識別鍵：店裡改過標題也不會種出重複列
        want = {"parent_id": parent_id, "title": m["title"], "icon": m["icon"],
                "perm_key": m["perm"], "sort_order": m["sort"], "is_active": 1}
        self.cur.execute("SELECT * FROM menus WHERE path=%s", (m["path"],))
        row = self.cur.fetchone()
        if row:
            self.cur_row = row
            self._update(row["id"], want, f"選單「{m['title']}」({m['path']})")
            return
        if not self.dry_run:
            self.cur.execute(
                "INSERT INTO menus (parent_id,title,icon,path,perm_key,sort_order,is_active) "
                "VALUES (%s,%s,%s,%s,%s,%s,1)",
                (parent_id, m["title"], m["icon"], m["path"], m["perm"], m["sort"]))
        self.added += 1
        self._say("+", f"選單「{m['title']}」({m['path']})")

    def permission(self, key: str, level: str) -> None:
        self.cur.execute("SELECT id FROM menu_permissions WHERE menu_key=%s AND level=%s",
                         (key, level))
        if self.cur.fetchone():
            self.kept += 1
            return
        if not self.dry_run:
            self.cur.execute("INSERT INTO menu_permissions (menu_key,level) VALUES (%s,%s)",
                             (key, level))
        self.added += 1
        self._say("+", f"權限 {key} → {level}")


def main() -> int:
    ap = argparse.ArgumentParser(description="種下 menus / menu_permissions（可重跑）")
    ap.add_argument("--dry-run", action="store_true", help="只列出會做什麼，不寫入")
    ap.add_argument("--sync", action="store_true",
                    help="既有列若與本檔不同也一併校正（預設只新增，不覆蓋手動調整）")
    args = ap.parse_args()

    db = dbpool.connect(**config.db_params(),
                         cursorclass=pymysql.cursors.DictCursor, autocommit=True)
    with db, db.cursor() as cur:
        cur.execute(_DDL_MENUS)
        cur.execute(_DDL_PERMS)
        s = Seeder(cur, args.dry_run, args.sync)

        print("選單結構：")
        for g in MENUS:
            gid = s.group(g)
            for m in g["children"]:
                s.leaf(gid, m)

        print("選單權限：")
        for key, levels in PERMISSIONS.items():
            for lv in levels:
                s.permission(key, lv)

        print(f"\n新增 {s.added}、校正 {s.updated}、維持原樣 {s.kept}"
              + ("（乾跑，未實際寫入）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
