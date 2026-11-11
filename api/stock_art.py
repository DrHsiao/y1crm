"""
stock_art.py — 好評分享以外（粉專貼文、聽力專欄、使用小教室）用真實的專業照片，不用 AI 插圖。
John 2026-11-08：先是「『助聽器知識』類別中的圖片不用吉卜力生成，尋找用相關專業的圖片修改呈現」，
後來「除了好評分享之外的都改用真實世界圖片」。

- 圖庫清單 scripts/illustrate/stock_library.json（只存來源、授權、主題標籤、裁切重心；圖檔不進 git）：
  照片來自可商用、可修改的免費圖庫（Pexels、Unsplash 授權不用標作者；Wikimedia Commons 的 CC BY／BY-SA
  要標作者——credit_required，官網文章頁封面下標出處，粉專自動配圖不挑這種）。
  scripts/illustrate/stock_import.py 下載、裁成 3:2、調成和官網一致的暖色調，存到 y1web media/uploads。
- 標記：extra.cover_src＝"stock"（圖庫照片）、cover_credit（來源、作者、授權）；粉專草稿 image_meta.src＝"stock"。
  AI 批次重畫不會動到圖庫照片。
- 挑圖（pick）：依標題和內文比對 tags，在夠相關的裡面挑用得最少的；都不相關時用標「通用」的。
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageEnhance, ImageOps

from api import web_admin

LIBRARY = Path(__file__).resolve().parent.parent / "scripts" / "illustrate" / "stock_library.json"
OUT_W, OUT_H = 1600, 1067             # 3:2，和官網卡片一樣
WARM = (255, 244, 230)                # 暖色調（官網米白底）
GENERAL_TAG = "通用"                  # 什麼主題都配得上的照片（沒有相關的照片時從這些挑）
REUSE_PENALTY = 8                     # 已經被幾篇用了，每篇扣這麼多分（約四個兩字標籤），免得相鄰文章同一張


def load(path: Path = LIBRARY) -> List[Dict[str, Any]]:
    """圖庫清單；已經匯入（有 file）的才能用。"""
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [x for x in items if isinstance(x, dict) and x.get("id") and x.get("tags")]


def available(items: Sequence[Dict[str, Any]], media: Optional[Path] = None) -> List[Dict[str, Any]]:
    out = [x for x in items if x.get("file") and web_admin.IMAGE_NAME_RE.match(x["file"])]
    return [x for x in out if media is None or (media / x["file"]).is_file()]


def score(entry: Dict[str, Any], text: str) -> int:
    t = (text or "").lower()
    return sum(len(tag) for tag in entry.get("tags", []) if tag and tag.lower() in t)


def pick(items: Sequence[Dict[str, Any]], text: str, used: Optional[Dict[str, int]] = None,
         avoid: Iterable[str] = (), allow_credit: bool = True) -> Optional[Dict[str, Any]]:
    """依標題、內文挑最相關的一張：比對到的標籤越長越多越相關；同分挑用得最少的、再依清單順序。
    avoid：不要這幾張（重新挑時排除目前那張）；allow_credit＝False：不挑要標作者的（粉專貼文）。"""
    used = used or {}
    avoid = set(avoid)
    cands = [x for x in items if x["file"] not in avoid and (allow_credit or not x.get("credit_required"))]
    if not cands:
        cands = [x for x in items if allow_credit or not x.get("credit_required")]
    if not cands:
        return None
    # 只在「夠相關」的裡面輪流（至少有最相關那張一半的分數）；完全不相關時用標了「通用」的。
    # 不這樣的話，常用的扣分會讓毫不相關、但沒人用過的照片勝出（11-08 充電、收納都配到耳鏡檢查）
    best = max(score(x, text) for x in cands)
    if best > 0:
        cands = [x for x in cands if score(x, text) * 2 >= best]
    else:
        cands = [x for x in cands if GENERAL_TAG in x.get("tags", [])] or cands
    order = {x["id"]: i for i, x in enumerate(items)}
    return max(cands, key=lambda x: (score(x, text) - REUSE_PENALTY * used.get(x["file"], 0),
                                     -used.get(x["file"], 0), -order[x["id"]]))


def credit(entry: Dict[str, Any]) -> Dict[str, Any]:
    """存在 extra.cover_credit 的出處。"""
    keys = ("id", "title", "source", "url", "author", "license", "license_url", "credit_required")
    return {k: entry.get(k) for k in keys if entry.get(k) not in (None, "")}


def credit_line(c: Dict[str, Any]) -> str:
    """官網要標的出處文字（只有 credit_required 的才標）。"""
    who = "／".join(x for x in (c.get("author"), c.get("source")) if x)
    return f"圖：{who}（{c.get('license', '')}），經裁切調色" if c.get("credit_required") else ""


def process(data: bytes, focal: Tuple[float, float] = (0.5, 0.5), pad: Optional[str] = None) -> bytes:
    """原圖 → 3:2、1600×1067、輕微暖色調的 JPEG。pad＝背景色（例如黑底的產品照）：不裁切，補邊到 3:2。"""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    if pad:
        w, h = im.size
        tw, th = (w, round(w / 1.5)) if w / h > 1.5 else (round(h * 1.5), h)      # 太寬補上下、太高補左右
        canvas = Image.new("RGB", (tw, th), pad)
        canvas.paste(im, ((tw - w) // 2, (th - h) // 2))
        im = canvas.resize((OUT_W, OUT_H), Image.LANCZOS)
    else:
        im = ImageOps.fit(im, (OUT_W, OUT_H), Image.LANCZOS, centering=focal)
    im = ImageEnhance.Color(im).enhance(0.94)
    im = ImageEnhance.Contrast(im).enhance(1.03)
    im = Image.blend(im, Image.new("RGB", im.size, WARM), 0.05)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def usage(cur) -> Dict[str, int]:
    """每張圖庫照片目前被幾篇用（挑圖時避開常用的）。"""
    cur.execute(f"SELECT cover FROM {web_admin.TABLE} WHERE cover IS NOT NULL "
                "AND JSON_UNQUOTE(JSON_EXTRACT(extra, '$.cover_src'))='stock'")
    out: Dict[str, int] = {}
    for r in cur.fetchall():
        out[r["cover"]] = out.get(r["cover"], 0) + 1
    cur.execute("SELECT image FROM fb_post_drafts WHERE image IS NOT NULL "
                "AND JSON_UNQUOTE(JSON_EXTRACT(image_meta, '$.src'))='stock'")
    for r in cur.fetchall():
        out[r["image"]] = out.get(r["image"], 0) + 1
    return out


def set_cover(cur, media: Path, post_id: int, name: str, cred: Dict[str, Any], actor: str, at: str) -> None:
    """官網內容的封面換成圖庫照片 name，標成 stock、記出處 cred；換掉的舊圖沒人用就刪檔。"""
    old = web_admin.get_post(cur, post_id)
    cur.execute(f"UPDATE {web_admin.TABLE} SET cover=%s, updated_by=%s, "
                f"extra=JSON_SET({web_admin.CLEAR_COVER_KEYS}, '$.cover_src', 'stock', '$.cover_at', %s, "
                "'$.cover_credit', CAST(%s AS JSON)) WHERE id=%s",
                (name, actor, at, json.dumps(cred, ensure_ascii=False), post_id))
    if old and old["cover"] and old["cover"] != name:
        web_admin.remove_if_unused(cur, media, old["cover"])


def draft_meta(entry: Dict[str, Any], at: str) -> Dict[str, Any]:
    return {"src": "stock", "at": at, "credit": credit(entry), "scene": entry.get("title", "")}



def text_of(*parts: Any) -> str:
    return "\n".join(str(p) for p in parts if p)
