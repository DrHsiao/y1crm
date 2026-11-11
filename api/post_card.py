"""
post_card.py — 粉專貼文的圖卡：圖庫的專業照片修改過（裁切、調色、漸層），加上大字標題和店名。
John 2026-11-09「粉專貼文的圖片建立新圖」→「找任何專業圖片經過修改後使用＋大字標題」，今天 3 則＋以後每天。

- 版面 1080×1350（4:5，手機動態消息佔最大）：照片在上（不放大、不壓字在臉上），下方深色字卡放
  大字標題（第一行吸睛問句、第二行重點），左上角是類別標籤，最下面店名和網址。
- 照片一樣只用可商用的圖庫（api/stock_art.py）：網路上別人的照片就算修改過著作權還是對方的，不能用。
- 標題：本機 AI（573，純文字）從貼文擬，過用語檢查；AI 不能用時取貼文第一句。
- 只用在粉專（相片貼文）；同步聽力專欄時封面還是原照片（官網標題另外顯示，圖上再壓字會重複）。
- 檔案存 y1web media/uploads（web_admin.save_image，WebP），檔名記在 fb_post_drafts.card，
  標題和用的哪張照片記在 card_meta；換照片時圖卡重做、標題沿用。
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

import config
from api import fb_drafts, web_admin

log = logging.getLogger("y1crm.card")

W, H = 1080, 1350
PHOTO_H = 930                                  # 照片區高度；下面是字卡
FADE = 170                                     # 照片底部漸層到字卡的高度
NAVY = (20, 28, 40)
GOLD, WHITE, GREY = (255, 214, 102), (255, 255, 255), (178, 188, 202)
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_INDEX = 3                                 # Noto Sans CJK TC（繁中字形）
BRAND = "韻譯助聽器・睿聲中正門市"
TITLE_MAX, SUB_MAX = 12, 16                    # 大字標題、第二行的字數上限
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")

TITLE_PROMPT = """下面是助聽器門市粉專的一則貼文。請擬貼文圖片上的大字標題，只回 JSON：
{"title": "第一行：讓長輩和家人想停下來看的生活情境問句，結尾用？（12 個字以內）",
 "sub": "第二行：貼文裡的一個具體做法（16 個字以內）"}
格式參考（只是示範長短和語氣，內容要照貼文寫，不要照抄）：{"title": "出門常忘了帶電池？", "sub": "門口放一盒備用的"}
規則：
- 只用貼文裡有的內容；口語、溫和、台灣用語；
- 第二行寫「做什麼」，不寫效果：不要「更清楚」「更順暢」「更穩定」「更安心」這類說法；
- 不提價格、優惠、療效、「最」、品牌；助聽器不要叫耳機；不要表情符號和引號。

貼文：
@TEXT@"""


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, size, index=FONT_INDEX)


def _clean(s: Any, n: int) -> str:
    s = _EMOJI_RE.sub("", re.sub(r"\s+", "", str(s or ""))).strip("「」『』\"'“”")
    return s[:n]


def fallback_title(message: str, topic: str = "") -> Tuple[str, str]:
    """AI 不能用時：貼文第一句當標題（太長就在逗號斷）、第二句當第二行。"""
    body = fb_drafts.strip_extras(message or "")
    sents = [x for x in re.split(r"(?<=[。！？?!])|\n+", _EMOJI_RE.sub("", body)) if x and x.strip()]
    first = re.sub(r"\s+", "", sents[0] if sents else topic or "")
    if len(first) > TITLE_MAX + 4:                       # 稍長的整句留著（圖上會斷成兩行），太長才在逗號斷
        first = next((p for p in re.split(r"[，、：]", first) if 0 < len(p) <= TITLE_MAX + 4), first[:TITLE_MAX])
    second = re.sub(r"\s+", "", sents[1]) if len(sents) > 1 else ""
    if len(second) > SUB_MAX:
        second = next((p for p in re.split(r"[，、：]", second) if 0 < len(p) <= SUB_MAX), "")
    return first.lstrip("・").rstrip("。"), second.lstrip("・").rstrip("。")


def make_title(message: str, topic: str = "") -> Dict[str, Any]:
    """本機 AI 擬標題 → {title, sub, src（ai／text）}。寫太長、有不能用的字眼就改用 fallback_title。"""
    try:
        raw = fb_drafts._chat([{"role": "user", "content": TITLE_PROMPT.replace(
            "@TEXT@", fb_drafts.strip_extras(message)[:1200])}], 120, json_mode=True, temperature=0.5, num_predict=300)
        d = json.loads(raw)
        title, sub = _clean(d.get("title"), 40), _clean(d.get("sub"), 40)
        if not title or len(title) > TITLE_MAX + 3 or len(sub) > SUB_MAX + 2:        # 稍長的圖上會斷成兩行
            raise ValueError(f"太長：{title}／{sub}")
        errors, warns = fb_drafts.check(f"{title}\n{sub}")
        if errors or warns or "耳機" in title + sub:
            raise ValueError("用語：" + "；".join(errors + warns))
        return {"title": title, "sub": sub, "src": "ai"}
    except Exception as e:  # noqa: BLE001 — 改用貼文第一句
        log.warning("圖卡標題：本機 AI 的不能用（%s），改用貼文第一句", str(e)[:120])
    title, sub = fallback_title(message, topic)
    return {"title": title, "sub": sub, "src": "text"}


def _fit(d: ImageDraw.ImageDraw, text: str, width: int, size: int, low: int) -> ImageFont.FreeTypeFont:
    """一行放得下的最大字級（size 往下找到 low）。"""
    for s in range(size, low - 1, -2):
        f = _font(s)
        if d.textlength(text, font=f) <= width:
            return f
    return _font(low)


def _wrap(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, width: int) -> List[str]:
    """一行放不下就斷成兩行：從中間附近的逗號斷（沒有逗號就從中間），不會只剩一個字掉到下一行。"""
    if d.textlength(text, font=f) <= width:
        return [text]
    mid = len(text) / 2
    cuts = [i + 1 for i, ch in enumerate(text[:-1]) if ch in "，、：；！？"]
    i = min(cuts, key=lambda c: abs(c - mid)) if cuts else round(mid)
    a, b = text[:i].rstrip("，、"), text[i:]
    if max(d.textlength(a, font=f), d.textlength(b, font=f)) <= width:
        return [a, b]
    rows, cur = [], ""
    for ch in text:
        if cur and d.textlength(cur + ch, font=f) > width:
            if ch in "，。、！？；：」）":
                cur, ch = cur[:-1], cur[-1] + ch
            rows.append(cur)
            cur = ch
        else:
            cur += ch
    return rows + ([cur] if cur else [])


def render(photo: Image.Image, title: str, sub: str = "", tag: str = "", focal: Sequence[float] = (0.5, 0.45),
           site: str = "") -> Image.Image:
    """照片＋文字 → 1080×1350 的圖卡（RGB）。"""
    im = Image.new("RGB", (W, H), NAVY)
    pic = ImageOps.fit(photo.convert("RGB"), (W, PHOTO_H), Image.LANCZOS, centering=tuple(focal))
    pic = ImageEnhance.Contrast(ImageEnhance.Color(pic).enhance(1.05)).enhance(1.04)   # 修改：調色
    im.paste(pic, (0, 0))
    fade = Image.new("L", (1, FADE))                                      # 照片底部漸層進字卡
    for y in range(FADE):
        fade.putpixel((0, y), int(255 * (y / (FADE - 1)) ** 1.6))
    band = Image.new("RGB", (W, FADE), NAVY)
    im.paste(band, (0, PHOTO_H - FADE), fade.resize((W, FADE)))
    top = Image.new("L", (1, 160))                                        # 上緣淡淡壓暗，標籤看得清楚
    for y in range(160):
        top.putpixel((0, y), int(110 * (1 - y / 159) ** 2))
    im.paste(Image.new("RGB", (W, 160), NAVY), (0, 0), top.resize((W, 160)))

    d = ImageDraw.Draw(im)
    M = 72                                                                # 左右留白
    if tag:
        f = _font(34)
        tw = d.textlength(tag, font=f)
        d.rounded_rectangle((M - 8, 48, M + tw + 40, 48 + 62), radius=31, fill=GOLD)
        d.text((M + 16, 48 + 31), tag, font=f, fill=NAVY, anchor="lm")

    tf = _fit(d, title, W - 2 * M, 100, 72)
    rows = _wrap(d, title, tf, W - 2 * M)[:2]
    sf = _fit(d, sub, W - 2 * M, 60, 44) if sub else None
    subs = _wrap(d, sub, sf, W - 2 * M)[:2] if sub else []
    th = sum(d.textbbox((0, 0), r, font=tf, stroke_width=3)[3] + 14 for r in rows)          # 先量高度再置中
    sh = sum(d.textbbox((0, 0), r, font=sf)[3] + 12 for r in subs) if subs else 0
    block = th + 46 + sh
    y = max(PHOTO_H - 70, (PHOTO_H - 70 + H - 130 - block) // 2)    # 字卡區（標題壓一點在漸層上）垂直置中
    for r in rows:
        bb = d.textbbox((W // 2, y), r, font=tf, anchor="mt", stroke_width=3)
        d.text((W // 2, y), r, font=tf, fill=WHITE, anchor="mt", stroke_width=3, stroke_fill=NAVY)
        y = bb[3] + 14
    lw = min(d.textlength(rows[-1], font=tf), W - 2 * M) * 0.72           # 金色底線
    d.rounded_rectangle((W // 2 - lw / 2, y + 4, W // 2 + lw / 2, y + 14), radius=5, fill=GOLD)
    y += 46
    for r in subs:
        bb = d.textbbox((W // 2, y), r, font=sf, anchor="mt")
        d.text((W // 2, y), r, font=sf, fill=GOLD, anchor="mt")
        y = bb[3] + 12
    host = re.sub(r"^https?://", "", site or "").rstrip("/")
    d.line((M, H - 112, W - M, H - 112), fill=(52, 62, 78), width=2)
    d.text((W // 2, H - 64), f"{BRAND}{'　' + host if host else ''}", font=_font(32), fill=GREY, anchor="mm")
    return im


def check_title(title: str, sub: str) -> Tuple[str, str]:
    """店員改的標題：整理、檢查（圖上的字發佈時不會再檢查，所以提醒也擋）。錯誤丟 ValueError。"""
    title, sub = _clean(title, 40), _clean(sub, 40)
    if not title:
        raise ValueError("請填大字標題。")
    if len(title) > TITLE_MAX + 6 or len(sub) > SUB_MAX + 6:
        raise ValueError(f"字太多了：大字標題 {TITLE_MAX + 6} 字、第二行 {SUB_MAX + 6} 字以內。")
    errors, warns = fb_drafts.check(f"{title}\n{sub}")
    if errors or warns:
        raise ValueError("圖上的字要改：" + "；".join(errors + warns))
    return title, sub


def _focal(image_meta: Dict[str, Any]) -> Sequence[float]:
    """照片在圖庫清單裡的裁切重心（臉、重點的位置）；找不到用偏上一點的中間。"""
    from api import stock_art
    pid = ((image_meta or {}).get("credit") or {}).get("id")
    e = next((x for x in stock_art.load() if x["id"] == pid), None)
    return (e or {}).get("focal") or (0.5, 0.45)


WEB_TAGS = {"article": "聽力專欄", "care": "使用小教室"}


def _tag(cur, r: Dict[str, Any]) -> str:
    """左上角的標籤：草稿類別；官網文章導讀改標那篇的分類（使用小教室、聽力專欄）。"""
    if r["category"] == "web" and r.get("web_post_id"):
        cur.execute(f"SELECT kind FROM {web_admin.TABLE} WHERE id=%s", (r["web_post_id"],))
        w = cur.fetchone()
        if w and w["kind"] in WEB_TAGS:
            return WEB_TAGS[w["kind"]]
    return fb_drafts.CATEGORIES.get(r["category"], "")


def build(cur, did: int, title: Optional[str] = None, sub: Optional[str] = None, retitle: bool = False,
          src: str = "staff") -> Optional[str]:
    """草稿 did 的圖卡做好、記到資料庫，回檔名（沒有配圖＝不做、清掉，回 None）。
    標題：有給就用（店員改的）；沒給沿用上次的；retitle 或第一次做才請本機 AI 擬。"""
    cur.execute("SELECT id, category, topic, message, image, image_meta, card, card_meta, status, web_post_id "
                "FROM fb_post_drafts WHERE id=%s", (did,))
    r = cur.fetchone()
    if not r:
        raise LookupError(did)
    if r["status"] != "new":                     # 已經發出去的不用再做（Facebook 上的圖改不了）
        return r["card"]
    media = Path(config.WEB["media_dir"])
    meta = fb_drafts._json(r["card_meta"])
    if not r["image"]:
        _set(cur, media, r, None, meta)
        return None
    if title is not None:
        meta.update(title=title, sub=sub or "", src=src)
    elif retitle or not meta.get("title"):
        meta.update(make_title(r["message"], r["topic"]))
    path = web_admin.image_path(media, r["image"])
    if not path or not path.is_file():
        raise FileNotFoundError(r["image"])
    with Image.open(path) as photo:
        im = render(photo, meta["title"], meta.get("sub", ""), _tag(cur, r),
                    _focal(fb_drafts._json(r["image_meta"])), fb_drafts.site_url(cur))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    name = web_admin.save_image(media, buf.getvalue())
    meta.update(image=r["image"], at=time.strftime("%Y-%m-%d %H:%M"))
    _set(cur, media, r, name, meta)
    log.info("圖卡 #%s：%s（%s／%s）", did, name, meta["title"], meta.get("sub", ""))
    return name


def _set(cur, media: Path, r: Dict[str, Any], name: Optional[str], meta: Dict[str, Any]) -> None:
    cur.execute("UPDATE fb_post_drafts SET card=%s, card_meta=%s WHERE id=%s",
                (name, json.dumps(meta, ensure_ascii=False) if meta else None, r["id"]))
    if r["card"] and r["card"] != name:
        web_admin.remove_if_unused(cur, media, r["card"])
