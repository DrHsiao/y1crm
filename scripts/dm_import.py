#!/usr/bin/env python3
"""
dm_import.py — DM.pdf 型錄批次匯入 y1crm products 的共用工具。

分段呼叫：每段把當頁解析出的商品記錄（dict 清單）丟給 insert_products()，
以「品牌+型號」唯一鍵去重（可重跑不重複），並可選擇從 PDF 裁圖掛成商品附件。

助聽器階層展開：expand_ha() 把「一機型多等級」展成每等級一筆 SKU
（model_name 併入等級以避開 uq_product 唯一鍵）。

用法（段內）：
    from dm_import import insert_products, expand_ha, LVL
    recs = expand_ha(brand="ReSound", series="Vivia", num="61", form="ric",
                     battery="312", tiers=[(4,12,65000),(5,12,85000)],
                     img=(5,(395,200,550,360)))   # page_index 0-based
    recs += [dict(brand="ReSound", model_name="TV Streamer+", category="tv_streamer",
                  spec="電視音訊匯流器", list_price=13000)]
    insert_products(recs)
"""
import sys
from pathlib import Path

import fitz  # pymupdf
import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

PDF = "/home/john/DM.pdf"
ACTOR = "型錄匯入"
LVL = {1: "entry", 2: "standard", 3: "advanced", 4: "premium", 5: "flagship"}

_doc = None
def _pdf():
    global _doc
    if _doc is None:
        _doc = fitz.open(PDF)
    return _doc


def crop_png(page_i, rect):
    return _pdf()[page_i].get_pixmap(dpi=220, clip=fitz.Rect(*rect)).tobytes("png")


def expand_ha(brand, series, num, form, battery, tiers, img=None,
              category="hearing_aid", supplier=None):
    """一機型多等級 → 每等級一筆 SKU。tiers=[(level,channels,price),...]。
    img=(page_index0, (x0,y0,x1,y1)) 該機型的機身照裁圖框（各等級共用）。"""
    out = []
    label = f"{series} {num}".strip()
    for lvl, ch, price in tiers:
        rec = dict(brand=brand, model_name=f"{label} 等級{lvl}", category=category,
                   spec=f"等級{lvl} / {ch}頻道", ha_form=form,
                   ha_tech_level=LVL.get(lvl), ha_channels=ch, ha_battery=battery,
                   list_price=price, is_active=1)
        if supplier:
            rec["supplier"] = supplier
        if img:
            rec["_img"] = (img[0], img[1], f"{label}.png")
        out.append(rec)
    return out


def insert_products(records, dry=False):
    c = pymysql.connect(**config.db_params(), cursorclass=pymysql.cursors.DictCursor,
                        autocommit=True)
    cur = c.cursor()
    added = skipped = imgs = 0
    for rec in records:
        rec = dict(rec)
        img = rec.pop("_img", None)
        cur.execute("SELECT id FROM products WHERE brand=%s AND model_name=%s",
                    (rec["brand"], rec["model_name"]))
        ex = cur.fetchone()
        if ex:
            pid = ex["id"]; skipped += 1
        else:
            rec["updated_by"] = ACTOR
            if dry:
                added += 1
                continue
            cols = ",".join(f"`{k}`" for k in rec)
            marks = ",".join(["%s"] * len(rec))
            cur.execute(f"INSERT INTO products ({cols}) VALUES ({marks})", list(rec.values()))
            pid = cur.lastrowid; added += 1
        if img and not dry:
            cur.execute("SELECT COUNT(*) n FROM product_attachments WHERE product_id=%s", (pid,))
            if cur.fetchone()["n"] == 0:
                page_i, rect, name = img
                data = crop_png(page_i, rect)
                rel = config.save_file("product_attachments", data, "image/png", pid, orig_name=name)
                cur.execute("INSERT INTO product_attachments "
                            "(product_id,rel_path,filename,mime,bytes_size,created_by) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (pid, rel, name, "image/png", len(data), ACTOR))
                imgs += 1
    cur.execute("SELECT COUNT(*) n FROM products"); tot = cur.fetchone()["n"]
    print(f"新增 {added}、跳過(已存在) {skipped}、掛圖 {imgs}；products 總數={tot}")
    return added, skipped
