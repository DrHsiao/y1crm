"""
post_art.py — 粉專貼文、聽力專欄、使用小教室的配圖：從圖庫挑真實照片（api/stock_art.py）。

John 2026-11-08 先要「沒有圖片的貼文用本機吉卜力生成圖片」，後來改成「除了好評分享之外的都改用真實世界圖片」，
所以這裡不再畫 AI 插圖（好評分享的示意插圖在 api/review_art.py）。
- 粉專草稿：圖存在 y1web media/uploads，檔名記在 fb_post_drafts.image（image_meta.src＝"stock"）；
  發佈時變成相片貼文（fb_drafts.publish），同步聽力專欄時同一張當文章封面。
- 已經發出去的粉專貼文：Facebook 不讓程式替已發的貼文加圖，只換它同步的專欄文章封面（草稿上也記一份）。
- 挑圖不用 GPU，一瞬間就好：呼叫端直接等結果。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import config
from api import fb_drafts, review_art, stock_art, web_admin

log = logging.getLogger("y1crm.art")

ART_KINDS = ("article", "care")
_MD_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)|[#>*_`|]")


def plain(md: str) -> str:
    """Markdown → 拿來比對主題的純文字（連結只留文字、圖片拿掉符號）。"""
    return re.sub(r"\n{3,}", "\n\n", _MD_RE.sub(lambda m: m.group(1) or "", md or "")).strip()


def _x(v: Any) -> Dict[str, Any]:
    return fb_drafts._json(v)


def cover_src(x: Dict[str, Any]) -> str:
    """官網內容圖片的來源（舊資料只有 cover_ai）。"""
    return x.get("cover_src") or ("ai" if x.get("cover_ai") else "upload")


def draft_targets(cur, ids: Sequence[int], force: bool) -> List[Dict[str, Any]]:
    """要配圖的粉專草稿（含已發佈的）。有圖的只有 force 才換一張。
    同步過的專欄文章：封面是空的、或就是這則的舊圖、或 force 換 AI／圖庫圖時，跟著換（店員自己放的封面不動）。"""
    if not ids:
        return []
    ids = [int(i) for i in ids]
    cur.execute("SELECT d.id, d.topic, d.message, d.image, d.web_article_id, "
                "w.cover AS web_cover, w.extra AS web_extra FROM fb_post_drafts d "
                "LEFT JOIN y1web.web_posts w ON w.id=d.web_article_id "
                f"WHERE d.status IN ('new','published','dev') AND d.id IN ({','.join(['%s'] * len(ids))}) ORDER BY d.id",
                ids)
    out = []
    for r in cur.fetchall():
        if r["image"] and not force:
            continue
        follow = r["web_article_id"] and (not r["web_cover"] or r["web_cover"] == r["image"]
                                          or (force and cover_src(_x(r["web_extra"])) in ("ai", "stock")))
        out.append({"kind": "draft", "id": r["id"], "label": f"粉專貼文 #{r['id']}", "title": r["topic"],
                    "text": fb_drafts.strip_extras(r["message"]), "article": r["web_article_id"] if follow else None,
                    "current": r["image"]})
    return out


def article_targets(cur, ids: Optional[Sequence[int]], force: bool,
                    kinds: Sequence[str] = ART_KINDS) -> List[Dict[str, Any]]:
    """要配封面的官網文章（聽力專欄、使用小教室）。ids＝None：這些類別裡所有沒有封面的。"""
    kinds = [k for k in kinds if k in ART_KINDS]
    if not kinds or ids == []:
        return []
    cur.execute(f"SELECT id, kind, title, summary, body_md, cover, extra FROM {web_admin.TABLE} "
                f"WHERE kind IN ({','.join(['%s'] * len(kinds))}) ORDER BY id", kinds)
    want = None if ids is None else {int(i) for i in ids}
    out = []
    for r in cur.fetchall():
        if want is not None and r["id"] not in want:
            continue
        if r["cover"] and not (force and cover_src(_x(r["extra"])) in ("ai", "stock")):   # 店員自己放的封面一律不動
            continue
        text = "\n\n".join(x for x in (r["summary"] or "", plain(r["body_md"])) if x)
        out.append({"kind": r["kind"], "id": r["id"], "label": f"#{r['id']}", "title": r["title"], "text": text,
                    "article": None, "current": r["cover"]})
    return out


def set_draft_image(cur, media: Path, did: int, name: Optional[str], meta: Optional[Dict[str, Any]] = None) -> None:
    """草稿的配圖換成 name（None＝拿掉），來源記在 meta；換掉的舊圖沒人用就刪檔。"""
    cur.execute("SELECT image, card FROM fb_post_drafts WHERE id=%s", (did,))
    r = cur.fetchone()
    stale = r and r["card"] and r["image"] != name          # 換了照片：圖卡要重做（標題留在 card_meta 沿用）
    cur.execute("UPDATE fb_post_drafts SET image=%s, image_meta=%s" + (", card=NULL" if stale else "") + " WHERE id=%s",
                (name, json.dumps(meta or {}, ensure_ascii=False) if name else None, did))
    if r and r["image"] and r["image"] != name:
        web_admin.remove_if_unused(cur, media, r["image"])
    if stale:
        web_admin.remove_if_unused(cur, media, r["card"])


def illustrate(drafts: Sequence[int], articles: Optional[Sequence[int]], actor: str, force: bool = False,
               kinds: Sequence[str] = ART_KINDS) -> Dict[str, Any]:
    """從圖庫挑照片放上去（重挑時避開目前那張）。粉專貼文不挑要標作者的照片。
    drafts：粉專草稿 id；articles：官網文章 id，None＝kinds 裡所有沒有封面的、[]＝不處理文章。回 {total, done, errors}。"""
    media = Path(config.WEB["media_dir"])
    out: Dict[str, Any] = {"total": 0, "done": 0, "errors": []}
    items = stock_art.available(stock_art.load(), media)
    with review_art._conn() as db, db.cursor() as cur:
        dts = draft_targets(cur, drafts, force)
        linked = {t["article"] for t in dts if t["article"]}
        targets = dts + [t for t in article_targets(cur, articles, force, kinds) if t["id"] not in linked]
        out["total"] = len(targets)
        used = stock_art.usage(cur)
        for t in targets:
            e = stock_art.pick(items, stock_art.text_of(t["title"], t["text"]), used,
                               avoid=[t["current"]] if force and t["current"] else [],
                               allow_credit=t["kind"] != "draft")
            if e is None:
                out["errors"].append(f"{t['label']} 圖庫裡沒有可用的照片")
                continue
            used[e["file"]] = used.get(e["file"], 0) + 1
            at, cred = review_art.now_str(), stock_art.credit(e)
            if t["kind"] == "draft":
                set_draft_image(cur, media, t["id"], e["file"], stock_art.draft_meta(e, at))
                make_card(cur, t["id"])
            if t["kind"] != "draft" or t["article"]:
                stock_art.set_cover(cur, media, t["article"] or t["id"], e["file"], cred, actor, at)
            out["done"] += 1
            log.info("圖庫照片 %s%s：%s", t["label"], f"（專欄 #{t['article']}）" if t["article"] else "", e["id"])
    return out


def make_card(cur, did: int) -> Optional[str]:
    """粉專草稿的圖卡（照片＋大字標題，api/post_card.py）。做不出來不影響配圖：發佈時用原照片。"""
    from api import post_card
    try:
        return post_card.build(cur, did)
    except Exception:  # noqa: BLE001
        log.exception("粉專草稿 #%s 圖卡做不出來（照樣發照片）", did)
        return None


def ensure_stock(cur, r: Dict[str, Any]) -> None:
    """發佈前：配圖還是以前 AI 畫的 → 換成圖庫照片（r 一起更新）。沒有圖的不補（可能是店員按了「不要圖」）。"""
    if not r.get("image") or _x(r.get("image_meta")).get("src") != "ai":
        return
    media = Path(config.WEB["media_dir"])
    e = stock_art.pick(stock_art.available(stock_art.load(), media),
                       stock_art.text_of(r.get("topic"), fb_drafts.strip_extras(r.get("message") or "")),
                       stock_art.usage(cur), allow_credit=False)
    if e is None:
        return
    meta = stock_art.draft_meta(e, review_art.now_str())
    set_draft_image(cur, media, r["id"], e["file"], meta)
    r["image"], r["image_meta"], r["card"] = e["file"], json.dumps(meta, ensure_ascii=False), None
