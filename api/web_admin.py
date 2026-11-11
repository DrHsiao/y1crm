"""
web_admin.py — 「官網管理」：編輯官網（y1web，port 8006）的關於我們、聽力專欄、好評分享、使用小教室、補助小幫手。

資料在 y1web 資料庫的 web_posts。y1crm 用自己的資料庫連線寫入；官網那邊的資料庫帳號只能讀。
官網有 60 秒快取，存檔後最多一分鐘就會出現在官網上。
圖片一律轉成 WebP、縮到 1600px，存到官網的 media/uploads（官網唯讀），檔名＝內容雜湊；
封面圖另做一張卡片圖放在 media/uploads/c/（同檔名）。

本檔是純規則（欄位檢查、網址代稱、圖片處理）＋資料存取，有單元測試；app.py 只管 I/O 與權限。
"""
from __future__ import annotations

import calendar
import hashlib
import html
import io
import json
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from PIL import Image, ImageOps

from api import wording

KINDS = {"about": "關於我們", "article": "聽力專欄", "share": "好評分享", "care": "使用小教室", "subsidy": "補助小幫手"}
# 下面三份和官網 y1web app/content.py 的 COLUMN_CATEGORIES／CARE_TYPES／SUBSIDY_CONDITIONS 相同，要一起改
COLUMN_CATEGORIES = ["助聽器知識", "聽力保健", "聽能復健", "補助申請", "生活樂活"]
# 使用小教室「適用的助聽器」：都不勾＝每一種都適用（官網產品頁依外型／電源帶出相關教學）
CARE_TYPES = {"battery": "電池式", "rechargeable": "充電式", "ric": "耳掛型 RIC", "bte": "耳掛型 BTE",
              "custom": "耳內型（訂製）"}
# 補助小幫手「適用條件」：全部符合才列為「可能適用」；都不勾＝一般資訊（每個人都會看到）
SUBSIDY_CONDITIONS = {"taipei": "設籍臺北市", "disability": "領有身心障礙證明", "age65": "65 歲以上",
                      "low_income": "低收入戶或中低收入戶", "work": "聽力損失和工作有關（職業傷病）",
                      "veteran": "榮民或榮眷"}
STATUSES = {"draft": "草稿", "published": "發佈"}
# 好評分享的兩種格式（John 2026-11-03，設計在 y1web docs/好評轉貼.md）：
#   心得文章＝客人的故事，有自己的網頁；評論轉貼＝Google 地圖等平台上的評論，原文照登或節錄，只在列表顯示
SHARE_FORMATS = {"story": "心得文章", "quote": "評論轉貼"}
# 評論轉貼的來源平台；官網 y1web app/content.py 的 SHARE_PLATFORMS 有同樣的代碼和名稱，要一起改。
#   代碼: (名稱, 原文連結允許的網域（None＝任何網址；()＝沒有連結）, 有沒有星等)
SHARE_PLATFORMS = {
    "google": ("Google 地圖", ("google.com", "google.com.tw", "goo.gl", "g.co"), True),
    "facebook": ("Facebook", ("facebook.com", "fb.com", "fb.me"), False),
    "store": ("門市心得卡", (), False),
    "other": ("其他", None, False),
}
LIMITS = {"title": 120, "summary": 300, "body_md": 50000, "slug": 80, "en": 60,
          "person": 40, "model": 60, "hearing": 40, "video": 300, "link": 300, "agency": 60,
          "url": 300, "author": 60, "original": 5000, "source_url": 300, "source_title": 200, "source_site": 60}
PASTE_MAX = 20000
URL_RE = re.compile(r"^https?://[^\s<>\"']+$")
# 官網對外網址：只要「協定＋網域＋（port）」，不帶路徑（官網 y1web app/content.py SITE_URL_RE 同規則）
SITE_URL_RE = re.compile(r"^https?://[a-z0-9.-]+(:\d{1,5})?$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{16}\.webp$")
IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "MPO", "BMP"}
IMAGE_MAX_EDGE = 1600
IMAGE_MAX_PIXELS = 50_000_000          # 超過就拒收（防解壓縮炸彈）
# 卡片圖：封面圖另存一張中等大小的在 media/uploads/c/（檔名和原圖相同，資料庫不用多欄位）。
# 好評分享的評論卡片上方約 360×240 的圖（裁成 3:2），做成至少蓋滿 720×480，高解析螢幕也清楚
THUMB_DIR = "c"
THUMB_W, THUMB_H = 720, 480
TABLE = "y1web.web_posts"
# 示意插圖（api/review_art.py）的標記：跟著圖片走；店員自己上傳或移除圖片就清掉
# 圖片的來源（John 2026-11-08「要標註圖片是原圖還是 AI 生成，以後才能批次替換」）：
#   cover_src＝"ai"（本機 AI 畫的）或 "upload"（原圖：店員上傳的照片）；AI 的另外記 cover_model（模型／畫風版本）、
#   cover_at（什麼時候畫的）、cover_prompt、cover_people。cover_ai 是舊的旗標，官網和程式還在用，和 cover_src="ai" 同義。
#   批次重畫：scripts/illustrate/image_sources.py
#   "stock"＝圖庫照片（「助聽器知識」類別，api/stock_art.py），出處記在 cover_credit。
#   "source"＝翻譯文章帶回來的原文圖片（api/article_import.py），cover_credit＝{source: 出處名稱, url: 原文網址}，官網標「圖片來源」。
COVER_KEYS = ("cover_ai", "cover_src", "cover_model", "cover_at", "cover_prompt", "cover_people", "cover_credit")
COVER_SRC = {"ai": "AI 生成", "upload": "原圖", "stock": "圖庫照片", "source": "原文圖片"}
# 表單上沒有、存檔時要從舊資料帶過來的 extra：AI 插圖標記、Google 評論代號、評論附了幾張照片、文章影片
PRESERVE_KEYS = COVER_KEYS + ("review_id", "review_photos", "video", "video_draft")   # video：做好的影片；video_draft：CRM「做影片」的分鏡


def carried_extra(old_x: Dict[str, Any]) -> Dict[str, Any]:
    """存檔時要從舊資料帶過來的 extra。video 只帶程式做的短影音（dict，含 YouTube 連結）：
    使用小教室的「影片連結」也叫 video（字串），那是表單上的欄位，要用表單送來的值。"""
    return {k: old_x[k] for k in PRESERVE_KEYS if k in old_x and (k != "video" or isinstance(old_x[k], dict))}
CLEAR_COVER_KEYS = ("JSON_REMOVE(COALESCE(extra, JSON_OBJECT()), '$.cover_ai', '$.cover_src', '$.cover_model', "
                    "'$.cover_at', '$.cover_prompt', '$.cover_people', '$.cover_credit')")
UPLOAD_COVER_KEYS = f"JSON_SET({CLEAR_COVER_KEYS}, '$.cover_src', 'upload', '$.cover_at', %s)"   # 店員上傳＝原圖


class Invalid(ValueError):
    """欄位不合格：訊息直接顯示給店員。"""


# ── 欄位檢查 ─────────────────────────────────────────────────

def _text(body: Dict[str, Any], key: str, required: bool = False, label: str = "") -> Optional[str]:
    v = str(body.get(key) or "").strip()
    if required and not v:
        raise Invalid(f"請填寫{label}。")
    if len(v) > LIMITS[key]:
        raise Invalid(f"{label}太長了（最多 {LIMITS[key]} 個字）。")
    return v or None


def _url(body: Dict[str, Any], key: str, label: str) -> str:
    v = _text(body, key, False, label) or ""
    if v and not URL_RE.match(v):
        raise Invalid(f"{label}請貼完整網址（https:// 開頭）。")
    return v


def _picks(body: Dict[str, Any], key: str, allowed: Dict[str, str]) -> List[str]:
    """勾選清單：只留認得的代碼，順序照 allowed。"""
    got = body.get(key) or []
    if not isinstance(got, list):
        raise Invalid("勾選欄位格式不正確。")
    return [k for k in allowed if k in got]


# 搜尋關鍵字（SEO）：聽力專欄、使用小教室可以填；官網放進網頁標頭（meta keywords、結構化資料），
# 專欄文章最下面也列出來（點了用專欄搜尋）。粉專貼文同步到專欄時由本機 AI 先擬好（api/fb_drafts.py）。
KEYWORDS_MAX = 10
KEYWORD_LEN = 20
_KW_SPLIT_RE = re.compile(r"[,，、;；\n]+")


def clean_keywords(v: Any) -> List[str]:
    """逗號、頓號或換行分開 → 去掉 #、去重。價格和療效、最高級用語不收（官網的用語規定）。"""
    items = v if isinstance(v, list) else _KW_SPLIT_RE.split(str(v or ""))
    out: List[str] = []
    for k in items:
        k = re.sub(r"\s+", " ", str(k)).strip().lstrip("#＃").strip()
        if not k or k in out:
            continue
        if len(k) > KEYWORD_LEN:
            raise Invalid(f"關鍵字「{k[:KEYWORD_LEN]}…」太長，每個請在 {KEYWORD_LEN} 字以內。")
        if wording.price_word(k):
            raise Invalid(f"關鍵字不能有價格字眼（{k}）。")
        if wording.claim_words(k):
            raise Invalid(f"關鍵字不要用療效或最高級用語（{k}）。")
        out.append(k)
    if len(out) > KEYWORDS_MAX:
        raise Invalid(f"關鍵字最多 {KEYWORDS_MAX} 個。")
    return out


# 翻譯／轉載的專欄文章：出處（John 2026-11-11「翻譯轉貼，給出處就好」）。官網文章上方顯示「本文翻譯整理自 出處〈原文標題〉」
SOURCE_MODES = {"translate": "翻譯整理", "repost": "轉載"}


def clean_source(body: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """原文網址、原文標題、出處名稱、翻譯或轉載 → extra.source；都沒填回 None。"""
    url = _url(body, "source_url", "原文網址")
    title = _text(body, "source_title", False, "原文標題") or ""
    site = _text(body, "source_site", False, "出處名稱") or ""
    if not (url or title or site):
        return None
    if not url:
        raise Invalid("有填出處的話，請貼原文網址。")
    if not site:
        site = re.sub(r"^(www|m)\.", "", (urlparse(url).hostname or "").lower())
    mode = body.get("source_mode") or "translate"
    if mode not in SOURCE_MODES:
        raise Invalid("出處的方式不正確。")
    return {"url": url, "title": title, "site": site, "mode": mode}


def auto_slug(kind: str, now: Optional[datetime] = None) -> str:
    """沒填網址代稱時自動產生，例如 article → a261021153045。"""
    now = now or datetime.now()
    return f"{kind[0]}{now:%y%m%d%H%M%S}"


def clean_post(kind: str, body: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """店員送來的欄位 → 可以寫進 web_posts 的值。不合格丟 Invalid。"""
    if kind not in KINDS:
        raise Invalid("不支援的內容類型。")
    now = now or datetime.now()
    quote = kind == "share" and body.get("format") == "quote"
    out: Dict[str, Any] = {
        "kind": kind,
        "title": _text(body, "title", not quote, "標題"),       # 評論轉貼的標題自動產生（只給後台看）
        "summary": _text(body, "summary", False, "摘要"),
        "body_md": _text(body, "body_md", False, "內文") or "",
    }
    slug = str(body.get("slug") or "").strip().lower()
    if slug and not SLUG_RE.match(slug):
        raise Invalid("網址代稱只能用英文小寫、數字和「-」，例如 hearing-aid-care。")
    out["slug"] = slug or auto_slug(kind, now)

    status = body.get("status") or "draft"
    if status not in STATUSES:
        raise Invalid("狀態不正確。")
    out["status"] = status
    try:
        out["sort"] = int(body.get("sort") or 0)
    except (TypeError, ValueError):
        raise Invalid("排序請填數字。")

    pub = str(body.get("publish_date") or "").strip()
    if pub:
        try:
            out["publish_at"] = datetime.combine(date.fromisoformat(pub), datetime.min.time()).replace(hour=9)
        except ValueError:
            raise Invalid("發佈日期格式不正確。")
    elif kind in ("article", "share") and status == "published":
        out["publish_at"] = now.replace(microsecond=0)     # 發佈時沒填日期＝今天
    else:
        out["publish_at"] = None

    out["category"] = None
    if kind == "article":
        cat = body.get("category") or ""
        if cat not in COLUMN_CATEGORIES:
            raise Invalid("請選擇專欄分類。")
        out["category"] = cat
        extra: Dict[str, Any] = {"keywords": clean_keywords(body.get("keywords"))}
        src = clean_source(body)
        if src:
            extra["source"] = src
    elif kind == "about":
        extra = {"en": _text(body, "en", False, "英文小標") or ""}
    elif kind == "care":
        extra = {"types": _picks(body, "types", CARE_TYPES), "video": _url(body, "video", "影片連結"),
                 "keywords": clean_keywords(body.get("keywords"))}
    elif kind == "subsidy":
        extra = {"when": _picks(body, "when", SUBSIDY_CONDITIONS),
                 "agency": _text(body, "agency", False, "受理單位") or "",
                 "link": _url(body, "link", "官方網址")}
    else:
        fmt = body.get("format") or "story"
        if fmt not in SHARE_FORMATS:
            raise Invalid("好評分享的格式不正確。")
        if quote:
            extra = _clean_quote(body, out, now)
        else:
            extra = {"format": "story", "person": _text(body, "person", True, "分享人稱呼"),
                     "model": _text(body, "model", False, "機種") or "",
                     "hearing": _text(body, "hearing", False, "聽力狀況") or ""}
        extra["consent"] = bool(body.get("consent"))
        # 好評分享用的是真實客人的經驗、照片和評論：沒有本人同意就不能公開
        if status == "published" and not extra["consent"]:
            raise Invalid("好評分享要先取得當事人同意（勾選「已取得當事人同意」）才能發佈。")
    out["extra"] = extra
    return out


# ── 好評分享：評論轉貼 ───────────────────────────────────────
#   評論的文字是別人寫的、掛著別人的名字：只能照登或刪節（刪掉的地方標「……」），不能改寫。
#   提到價格的不能發佈（用節錄刪掉那一句）；療效／誇大用語回 warnings，店員確認後才發佈。

ELLIPSIS_RE = re.compile(r"…+|⋯+|\.{3,}")
_ELLIPSIS_END_RE = re.compile(r"(?:…+|⋯+|\.{3,})$")


def _squash(s: str) -> str:
    """比對原文時不管空白和換行（店員常會順手刪掉換行）。"""
    return re.sub(r"\s+", "", s or "")


def mask_name(name: str) -> str:
    """官網顯示的評論帳號：末 3 個字換成 *（John 2026-11-04）。
    不到 4 個字的留第一個字，例如 王小明 → 王**，不然整個名字都是 *。"""
    n = (name or "").strip()
    if len(n) <= 1:
        return n
    k = min(3, len(n) - 1)
    return n[:-k] + "*" * k


def excerpt_problem(original: str, shown: str) -> Optional[str]:
    """官網顯示的文字是不是原文或原文的節錄。沒問題回 None，有問題回說明（給店員看）。"""
    src = _squash(original)
    s = (shown or "").strip()
    parts = [p for p in (_squash(x) for x in ELLIPSIS_RE.split(s)) if p]
    if not parts:
        return "官網顯示的文字是空的。"
    lead, trail = bool(ELLIPSIS_RE.match(s)), bool(_ELLIPSIS_END_RE.search(s))
    pos = 0
    for i, p in enumerate(parts):
        first, last = i == 0, i == len(parts) - 1
        j = src.find(p, pos)
        if j < 0:
            return f"「{p[:15]}」在原文裡找不到（或順序不對）。官網顯示的文字只能從原文刪減，不能改寫。"
        if first and not lead and j != 0:
            return "開頭刪掉的部分要用「……」標出來。"
        if last and not trail:
            k = len(src) - len(p)
            if not src.endswith(p) or k < pos or (first and not lead and k != 0):
                return "結尾刪掉的部分要用「……」標出來。"
            j = k
        pos = j + len(p)
    return None


_SENT_RE = re.compile(r"[^。！？!?；;\n]*[。！？!?；;]+|[^。！？!?；;\n]+")


def flag_sentences(text: str) -> List[Dict[str, Any]]:
    """把原文切成句子，標出提到價格（不能發佈）或療效／誇大用語（要確認）的句子。"""
    out = []
    for m in _SENT_RE.finditer(text or ""):
        if not m.group(0).strip():
            continue
        price, claims = wording.price_word(m.group(0)), wording.claim_words(m.group(0))
        out.append({"start": m.start(), "end": m.end(), "text": m.group(0).strip(),
                    "price": price, "claims": claims, "bad": bool(price or claims)})
    return out


def suggest_excerpt(original: str) -> str:
    """建議的節錄：刪掉有問題的句子，刪掉的地方換成「……」（一定通過 excerpt_problem）。"""
    out, last, dropping = "", 0, False
    for f in flag_sentences(original):
        gap = original[last:f["start"]]
        if f["bad"]:
            if not dropping:
                out = (out + gap).rstrip() + "……"
                dropping = True
        else:
            out += ("\n" if "\n" in gap else "") if dropping else gap
            out += original[f["start"]:f["end"]]
            dropping = False
        last = f["end"]
    return out.strip()


# 從 Google 地圖（或 Google 商家檔案）整段複製的評論長這樣（一行一段）：
#   王小明 / 在地嚮導・52 則評論・120 張相片 / 3 週前 / 新 / 評論內容… / 讚 / 分享 / 業主回應 2 週前 / 回覆內容…
#   星星是圖片，複製不到。介面上的字照 Google 2026-11 的版面寫，Google 改版時調這幾個規則。
_ZH_NUM = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_REL_RE = re.compile(r"^(?:(?:上次)?編輯(?:時間)?[:：]?\s*|已編輯[:：]?\s*|edited\s+)?(?:"
                     r"(\d+|[一二兩三四五六七八九十]{1,3})\s*(分鐘|小時|天|日|週|周|星期|個月|月|年)前"
                     r"|(a|an|one|\d+)\s+(minute|hour|day|week|month|year)s?\s+ago"
                     r"|(剛剛|昨天|上週|上星期|上個月|去年|just now|yesterday|last week|last month|last year))"
                     r"(?:\s*[·・|]?\s*(?:新|new|在\s*google\s*上|on\s+google))?$", re.I)
_YEAR_RE = re.compile(r"年前|去年|years?\s+ago|last\s+year", re.I)     # 「1 年前」只知道年，官網只顯示年
REVIEW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
_ABS_RE = re.compile(r"^(\d{4})\s*[年/.-]\s*(\d{1,2})\s*(?:[月/.-]\s*(\d{1,2})\s*日?)?$")
_META_RE = re.compile(r"\d\s*則(評論|留言)|\d\s*張(相片|照片)|在地嚮導|local guide|\d\s*reviews?\b|\d\s*photos?\b", re.I)
_STAR_RE = re.compile(r"^(?:([1-5])\s*(?:/\s*5|顆星|星|stars?)|(★{1,5})☆*)$", re.I)
_BADGE = {"新", "new"}
_TRUNC = {"更多", "顯示更多", "more", "…更多"}
_TRANSLATED = re.compile(r"由\s*google\s*提供翻譯|translated by google|查看原文|顯示原文|see original|^\(?原文\)?$|^（原文）$", re.I)
_STOP_RE = re.compile(r"^(讚|分享|實用|回覆|檢舉評論|like|share|helpful|reply)(\s*\d+)?$", re.I)
_OWNER_RE = re.compile(r"^(業主回應|業者回應|商家回應|店家回應|擁有者回應|response from the owner)", re.I)


def _is_meta(line: str) -> bool:
    """「在地嚮導・52 則評論・120 張相片」這種帳號說明（評論內容裡也可能有「52則評論」，所以限短行）。"""
    return len(line) <= 40 and bool(_META_RE.search(line))


def _zh_num(s: str) -> Optional[int]:
    if s.isdigit():
        return int(s)
    if s.lower() in ("a", "an", "one"):
        return 1
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _ZH_NUM.get(s[1:], 0)
    if s.endswith("十"):
        return _ZH_NUM.get(s[0], 0) * 10
    return _ZH_NUM.get(s)


def _months_ago(today: date, n: int) -> date:
    y, m = divmod(today.year * 12 + today.month - 1 - n, 12)
    return date(y, m + 1, min(today.day, calendar.monthrange(y, m + 1)[1]))


def review_date(line: str, today: date) -> Optional[date]:
    """「3 週前」「2026年10月3日」這類評論時間 → 大約的日期；不是時間回 None。"""
    t = line.strip()
    m = _ABS_RE.match(t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
        except ValueError:
            return None
    m = _REL_RE.match(t)
    if not m:
        return None
    if m.group(5):
        word = m.group(5).lower()
        n, unit = 1, {"剛剛": "天", "just now": "天", "昨天": "天", "yesterday": "天", "上週": "週", "上星期": "週",
                      "last week": "週", "上個月": "月", "last month": "月", "去年": "年", "last year": "年"}[word]
        if word in ("剛剛", "just now"):
            n = 0
    else:
        n = _zh_num(m.group(1) or m.group(3))
        unit = m.group(2) or {"minute": "分鐘", "hour": "小時", "day": "天", "week": "週",
                              "month": "月", "year": "年"}[m.group(4).lower()]
        if n is None:
            return None
    if unit in ("分鐘", "小時"):
        return today
    if unit in ("天", "日"):
        return today - timedelta(days=n)
    if unit in ("週", "周", "星期"):
        return today - timedelta(weeks=n)
    if unit in ("個月", "月"):
        return _months_ago(today, n)
    return _months_ago(today, 12 * n)


def _next_review(lines: List[str], i: int, today: date) -> bool:
    """第 i 行是不是下一則評論的帳號（後面一兩行是「52 則評論」或評論時間）。"""
    nxt = lines[i + 1:i + 3]
    x = lines[i]
    if not nxt or _OWNER_RE.match(x) or _STOP_RE.match(x) or review_date(x, today) or _OWNER_RE.match(nxt[0]):
        return False
    return _is_meta(nxt[0]) or any(review_date(y, today) for y in nxt)


def parse_review(text: str, today: Optional[date] = None) -> Dict[str, Any]:
    """整段複製的評論 → 評論帳號、日期、星等、原文、業主回應。拆不準的由店員在表單上改。"""
    today = today or date.today()
    lines = [x.strip() for x in (text or "").replace("\r", "").split("\n")]
    lines = [x for x in lines if x]
    out: Dict[str, Any] = {"author": "", "posted": "", "posted_prec": "", "rating": None, "original": "",
                           "owner_reply": "", "warnings": []}
    if not lines:
        out["warnings"].append("沒有貼上內容。")
        return out
    # 評論時間在前幾行：時間之前是帳號、「52 則評論」、星等；之後是評論內容
    t = next((i for i, x in enumerate(lines[:6]) if review_date(x, today)), None)
    if t is not None:
        out["posted"] = review_date(lines[t], today).isoformat()
        out["posted_prec"] = "year" if _YEAR_RE.search(lines[t]) else ""
        start = t + 1
    else:
        meta = next((i for i, x in enumerate(lines[:4]) if _is_meta(x)), None)
        start = meta + 1 if meta is not None else 0
        out["warnings"].append("沒找到評論時間，請自己填日期（官網只顯示年月）。")
    for x in lines[:start]:
        star = _STAR_RE.match(x)
        if star:
            out["rating"] = int(star.group(1) or len(star.group(2)))
        elif not out["author"] and not _is_meta(x) and not review_date(x, today):
            out["author"] = x
    body: List[str] = []
    reply: List[str] = []
    state, i = "body", start          # body → after（讚、分享之後）→ reply（業主回應）
    while i < len(lines):
        x = lines[i]
        low = x.lower()
        if _next_review(lines, i, today):
            out["warnings"].append("好像貼了不只一則評論，只取第一則。")
            break
        if _OWNER_RE.match(x) and state != "reply":
            state = "reply"
            rest = _OWNER_RE.sub("", x).strip(" ：:")
            if rest and not review_date(rest, today):
                reply.append(rest)
            if i + 1 < len(lines) and review_date(lines[i + 1], today):
                i += 1
        elif _STOP_RE.match(x):
            if state == "body":
                state = "after"
        elif state == "body" and not body and (_STAR_RE.match(x) or low in _BADGE):
            star = _STAR_RE.match(x)
            if star:
                out["rating"] = int(star.group(1) or len(star.group(2)))
        elif low in _TRUNC:
            out["warnings"].append("評論好像沒有展開（有「更多」），請在 Google 地圖按「更多」後重新複製。")
        elif _TRANSLATED.search(x):
            out["warnings"].append("這則有 Google 翻譯，請確認貼的是評論者寫的原文。")
        elif state == "body":
            body.append(x)
        elif state == "reply":
            reply.append(x)
        else:                         # 讚、分享之後又有字：下一則評論
            out["warnings"].append("好像貼了不只一則評論，只取第一則。")
            break
        i += 1
    out["original"] = "\n".join(body)
    out["owner_reply"] = "\n".join(reply)
    if not out["original"]:
        out["warnings"].append("沒拆出評論內容，請直接把評論文字貼到「評論原文」。")
    return out


def review_draft(text: str = "", original: str = "", today: Optional[date] = None) -> Dict[str, Any]:
    """後台「拆解／檢查」按鈕：貼整段（text）就先拆解；只給原文（original）就只做用語檢查和建議節錄。"""
    if len(text or "") > PASTE_MAX or len(original or "") > PASTE_MAX:
        raise Invalid("貼上的內容太長了，一次貼一則評論就好。")
    d = parse_review(text, today) if text else {"original": (original or "").strip(), "warnings": []}
    flags = [f for f in flag_sentences(d["original"]) if f["bad"]]
    for f in flags:
        del f["start"], f["end"], f["bad"]
    d["person"] = mask_name(d.get("author", ""))
    d["flags"] = flags
    d["suggested"] = suggest_excerpt(d["original"]) if flags else d["original"]
    return d


# ── 一次貼很多則（John 2026-11-08「把 Google 上的好評轉貼過來」）─────────────────
#   Google 不准程式去抓評論（Places／Business Profile API 的條款也不能拿來轉貼，見 y1web docs/好評轉貼.md），
#   所以一樣是人在 Google 地圖上複製；差別是可以把評論區整片複製下來，這裡切成一則一則，建成「草稿」。
#   發佈仍然要店員問過評論者、勾「已取得當事人同意」。星星複製不到：預設 5 顆，請在 Google 地圖先按「評分最高」排序。
BATCH_MAX = 3_000_000        # 整片評論區的 HTML（開發人員工具「複製 outerHTML」）150 則約 70 萬字
BATCH_LIMIT = 200


def split_reviews(text: str, today: Optional[date] = None) -> List[str]:
    """整片複製的評論區 → 一則一則（每則從評論帳號那一行開始）。開頭的店名、總評分等雜訊會變成拆不出內容的一段。"""
    today = today or date.today()
    lines = [x.strip() for x in (text or "").replace("\r", "").split("\n")]
    lines = [x for x in lines if x]
    starts = [i for i in range(len(lines)) if _next_review(lines, i, today)
              and not _is_meta(lines[i]) and not review_date(lines[i], today) and not _STAR_RE.match(lines[i])]
    if not starts or starts[0] != 0:
        starts = [0] + starts
    ends = starts[1:] + [len(lines)]
    return ["\n".join(lines[a:b]) for a, b in zip(starts, ends)]


# 評論區的 HTML（John 2026-11-08 存成檔案的 re.txt）：每則評論是最外層帶 data-review-id 的元素，
#   裡面：aria-label＝帳號、「5 顆星」、.rsqaWe 時間、.MyEned 評論文字、.CDe7pd 業主回應、.Tya61d 照片。
#   class 名稱是 Google 編譯出來的，改版會變：找不到時改用結構（aria-label、時間格式）判斷。
_VOID = {"img", "br", "input", "meta", "link", "hr", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
_HTML_STAR_RE = re.compile(r"([1-5])\s*(?:顆星|stars?)", re.I)


class _Node:
    __slots__ = ("tag", "attrs", "parent", "items")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["_Node"]):
        self.tag, self.attrs, self.parent, self.items = tag, attrs, parent, []

    def cls(self) -> List[str]:
        return (self.attrs.get("class") or "").split()

    def walk(self):
        yield self
        for x in self.items:
            if isinstance(x, _Node):
                yield from x.walk()

    def text(self) -> str:
        out = []
        for x in self.items:
            out.append(x if isinstance(x, str) else ("\n" if x.tag == "br" else x.text()))
        return "".join(out)

    def find(self, cls: str) -> List["_Node"]:
        return [n for n in self.walk() if cls in n.cls()]


class _DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = _Node(tag, {k: v or "" for k, v in attrs}, self.cur)
        self.cur.items.append(n)
        if tag not in _VOID:
            self.cur = n

    def handle_endtag(self, tag):
        c = self.cur
        while c is not self.root and c.tag != tag:
            c = c.parent
        if c is not self.root:
            self.cur = c.parent

    def handle_data(self, data):
        self.cur.items.append(data)


def is_review_html(text: str) -> bool:
    return (text or "").lstrip().startswith("<") and "data-review-id" in (text or "")


def _lines(s: str) -> str:
    return "\n".join(x.strip() for x in s.replace("\r", "").split("\n") if x.strip())


def reviews_from_html(text: str, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """評論區 HTML → 和 parse_review 一樣的欄位，另外有 photos（評論附了幾張照片）。"""
    today = today or date.today()
    b = _DomBuilder()
    b.feed(text)
    out = []
    for n in b.root.walk():
        if "data-review-id" not in n.attrs:
            continue
        p = n.parent
        while p is not None and "data-review-id" not in p.attrs:
            p = p.parent
        if p is not None:
            continue                                  # 只要最外層那個（裡面的按鈕也帶 data-review-id）
        reply_nodes = n.find("CDe7pd")
        in_reply = {id(x) for r in reply_nodes for x in r.walk()}
        rid = n.attrs["data-review-id"].strip()
        d: Dict[str, Any] = {"author": "", "posted": "", "posted_prec": "", "rating": None, "original": "",
                             "owner_reply": "", "warnings": [], "photos": 0,
                             "review_id": rid if REVIEW_ID_RE.match(rid) else ""}
        names = n.find("d4r55")
        d["author"] = _lines(names[0].text() if names else n.attrs.get("aria-label", ""))[:LIMITS["person"] * 4]
        for x in n.walk():
            m = _HTML_STAR_RE.search(x.attrs.get("aria-label", ""))
            if m:
                d["rating"] = int(m.group(1))
                break
        dates = [x.text().strip() for x in n.find("rsqaWe")] or \
                [s.strip() for x in n.walk() if id(x) not in in_reply for s in x.items if isinstance(s, str)]
        for t in dates:
            when = review_date(t, today)
            if when:
                d["posted"], d["posted_prec"] = when.isoformat(), "year" if _YEAR_RE.search(t) else ""
                break
        body = n.find("MyEned") or [x for x in n.find("wiI7pd") if id(x) not in in_reply][:1]
        d["original"] = _lines(body[0].find("wiI7pd")[0].text() if body and body[0].find("wiI7pd")
                               else (body[0].text() if body else ""))
        if reply_nodes:
            rt = reply_nodes[0].find("wiI7pd")
            d["owner_reply"] = _lines(rt[0].text() if rt else reply_nodes[0].text())
        d["photos"] = len(n.find("Tya61d")) or sum(1 for x in n.walk()
                                                   if x.tag == "button" and "background-image" in x.attrs.get("style", ""))
        all_text = n.text()
        if any(_TRANSLATED.search(t) for t in all_text.split("\n")):
            d["warnings"].append("這則有 Google 翻譯，請確認是評論者寫的原文。")
        if n.find("w8nwRe") or any(x.tag == "button" and x.text().strip() in _TRUNC for x in n.walk()):
            d["warnings"].append("評論沒有展開（有「更多」），請在 Google 地圖按「更多」後重新複製。")
        if "?" in d["original"] or "\ufffd" in d["original"]:
            d["warnings"].append("有「?」：可能是表情符號或罕用字在存檔時遺失，請對照 Google 地圖上的原文。")
        if not d["posted"]:
            d["warnings"].append("沒找到評論時間，請自己填日期（官網只顯示年月）。")
        out.append(d)
    return out


def decode_paste(data: bytes) -> str:
    """存成檔案的評論區：UTF-8（含 BOM）、UTF-16，或 Windows 記事本的「ANSI」（Big5／cp950）。
    Big5 裡沒有的 Google 圖示字、表情符號會壞掉，換成 \ufffd 或已經是 ?。"""
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp950", errors="replace")


def batch_reviews(cur, text: str, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """拆成一則一則，每則附上：官網顯示名稱、建議節錄、要注意的句子、是不是已經轉貼過、能不能建草稿。
    text 可以是整片複製的文字，或評論區的 HTML（reviews_from_html）。"""
    if len(text or "") > BATCH_MAX:
        raise Invalid("貼上的內容太長了，請分幾次貼。")
    parsed = (reviews_from_html(text, today) if is_review_html(text)
              else [parse_review(chunk, today) for chunk in split_reviews(text, today)])
    out: List[Dict[str, Any]] = []
    for d in parsed[:BATCH_LIMIT]:
        d["warnings"] = [w for w in d["warnings"] if "不只一則" not in w]
        if not d["original"] or not d["author"] or not d["posted"]:
            continue                                   # 店名、總評分、排序選單這些不是評論
        flags = [f for f in flag_sentences(d["original"]) if f["bad"]]
        for f in flags:
            del f["start"], f["end"], f["bad"]
        h = hashlib.sha256(_squash(d["original"]).encode()).hexdigest()[:16]
        cur.execute(f"SELECT id FROM {TABLE} WHERE kind='share' AND (JSON_UNQUOTE(JSON_EXTRACT(extra, '$.hash'))=%s "
                    "OR JSON_UNQUOTE(JSON_EXTRACT(extra, '$.review_id'))=%s)", (h, d.get("review_id") or "-"))
        dup = cur.fetchone()
        out.append({**d, "person": mask_name(d["author"]), "flags": flags,
                    "suggested": suggest_excerpt(d["original"]) if flags else d["original"],
                    "duplicate": dup["id"] if dup else None, "rating": d["rating"] or 5, "photos": d.get("photos", 0),
                    "review_id": d.get("review_id", ""), "posted_prec": d.get("posted_prec", "")})
    return out


def create_share_drafts(cur, items: List[Dict[str, Any]], actor: str, url: str = "",
                        platform: str = "google") -> Dict[str, Any]:
    """勾選的評論建成草稿（未勾同意）。不合格或重複的跳過，原因回給店員。"""
    created, skipped, with_photos = [], [], []
    stamp = datetime.now().strftime("%y%m%d%H%M%S")
    for i, it in enumerate(items[:BATCH_LIMIT]):
        body = {"kind": "share", "format": "quote", "platform": platform, "status": "draft", "consent": False,
                "slug": f"q{stamp}-{i + 1}",           # 同一秒建很多則：自動代稱會撞，這裡自己編號
                "url": it.get("url") or url, "rating": it.get("rating") or 5, "posted": it.get("posted") or "",
                "author": it.get("author") or "", "person": it.get("person") or "",
                "original": it.get("original") or "", "body_md": it.get("shown") or "",
                "review_id": it.get("review_id") or "", "posted_prec": it.get("posted_prec") or "",
                "review_photos": it.get("photos") or 0}
        try:
            clean = clean_post("share", body)
            pid = save_post(cur, clean, actor)
        except Invalid as e:
            skipped.append({"person": body["person"] or mask_name(body["author"]), "reason": str(e)})
            continue
        created.append(pid)
        if clean["extra"].get("review_photos"):
            with_photos.append(pid)
    return {"created": created, "skipped": skipped, "with_photos": with_photos}


def _host_ok(url: str, domains: Optional[Tuple[str, ...]]) -> bool:
    if domains is None:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _clean_quote(body: Dict[str, Any], out: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    plat = body.get("platform") or ""
    if plat not in SHARE_PLATFORMS:
        raise Invalid("請選擇評論來自哪個平台。")
    label, domains, stars = SHARE_PLATFORMS[plat]
    url = "" if domains == () else _url(body, "url", "原文連結")
    if url and not _host_ok(url, domains):
        raise Invalid(f"原文連結不是 {label} 的網址。")
    rating = None
    if stars:
        try:
            rating = int(body.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        if not 1 <= rating <= 5:
            raise Invalid("請選星等（1–5 顆）。")
    posted = str(body.get("posted") or "").strip()
    if posted:
        try:
            if date.fromisoformat(posted) > now.date():
                raise Invalid("評論日期不能在未來。")
        except ValueError:
            raise Invalid("評論日期格式不正確。")
    author = _text(body, "author", False, "評論帳號") or ""
    person = _text(body, "person", False, "官網顯示的名稱") or mask_name(author)
    if not person:
        raise Invalid("請填評論帳號（官網會把末 3 個字換成 *）。")
    original = _text(body, "original", True, "評論原文")
    shown = out["body_md"].strip() or original
    problem = excerpt_problem(original, shown)
    if problem:
        raise Invalid(problem)
    out["body_md"], out["summary"] = shown, None
    out["title"] = f"{label}｜{person}" + (f"｜{posted[:7]}" if posted else "")
    if out["status"] == "published":
        p = wording.price_word(shown)
        if p:
            raise Invalid(f"官網顯示的文字提到價格（「{p}」），請用節錄刪掉那一句。")
        out["warnings"] = [f"「{w}」可能被當成療效或誇大宣傳" for w in wording.claim_words(shown)]
    extra = {"format": "quote", "platform": plat, "url": url, "rating": rating, "posted": posted,
             "author": author, "person": person, "original": original,
             "hash": hashlib.sha256(_squash(original).encode()).hexdigest()[:16]}
    rid = str(body.get("review_id") or "").strip()
    if REVIEW_ID_RE.match(rid):
        extra["review_id"] = rid                     # Google 評論的代號：原文存檔時有字遺失也認得出是同一則
    if posted and body.get("posted_prec") == "year":
        extra["posted_prec"] = "year"                # 「1 年前」推回去的：官網只顯示年
    try:
        photos = max(0, min(30, int(body.get("review_photos") or 0)))
    except (TypeError, ValueError):
        photos = 0
    if photos:
        extra["review_photos"] = photos              # 評論在 Google 上附了照片：不畫示意插圖，店員問過再上傳照片
    return extra


# 好評分享頁的「到 Google 地圖看全部評論」這類連結：y1web.web_settings k='share_links'，{平台代碼: 網址}
def clean_share_links(body: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    for code, (label, domains, _) in SHARE_PLATFORMS.items():
        v = str(body.get(code) or "").strip()
        if not domains or not v:          # 門市心得卡、其他：沒有「看全部」的頁面
            continue
        if len(v) > LIMITS["url"] or not URL_RE.match(v) or not _host_ok(v, domains):
            raise Invalid(f"{label}的連結請貼完整網址（https:// 開頭），而且要是 {label} 的網址。")
        out[code] = v
    m = map_embed_src(body.get("map"))
    if m:
        out["map"] = m
    return out


# 好評分享彈窗裡的 Google 地圖（門市位置＋Google 上的總評分）。Google 的評論頁不准別的網站嵌入，
# 只有官方「分享 → 嵌入地圖」的網址可以；沒設定時官網用門市名稱搜尋（y1web content.share_map）
MAP_EMBED_RE = re.compile(r"^https://www\.google\.com/maps/embed\?pb=[A-Za-z0-9!._%:+*-]+$")
MAP_EMBED_MAX = 2000


def map_embed_src(v: Any) -> str:
    """貼整段 <iframe src="..."> 或只貼網址都可以 → 嵌入網址；空白回 ""，格式不對丟 Invalid。"""
    v = str(v or "").strip()
    if not v:
        return ""
    m = re.search(r'src\s*=\s*["\']([^"\']+)["\']', v)
    v = html.unescape(m.group(1) if m else v).strip()
    if len(v) > MAP_EMBED_MAX or not MAP_EMBED_RE.match(v):
        raise Invalid("Google 地圖嵌入碼請在 Google 地圖的門市頁按「分享 → 嵌入地圖 → 複製 HTML」，整段貼上。")
    return v


def get_share_links(cur) -> Dict[str, str]:
    try:
        cur.execute("SELECT v FROM y1web.web_settings WHERE k='share_links'")
        r = cur.fetchone()
    except Exception:
        r = None
    v = r["v"] if r else None
    v = json.loads(v) if isinstance(v, (str, bytes)) else (v or {})
    return {k: u for k, u in v.items() if (k in SHARE_PLATFORMS or k == "map") and isinstance(u, str)}


def set_share_links(cur, body: Dict[str, Any], actor: str) -> Dict[str, str]:
    links = clean_share_links(body)
    cur.execute("INSERT INTO y1web.web_settings (k, v, updated_by) VALUES ('share_links', %s, %s) "
                "ON DUPLICATE KEY UPDATE v=VALUES(v), updated_by=VALUES(updated_by)",
                (json.dumps(links), actor))
    return links


# ── 圖片 ─────────────────────────────────────────────────────

def process_image(data: bytes) -> Tuple[bytes, str]:
    """上傳的圖 → (WebP 內容, 檔名)。轉正（手機照片的 EXIF 方向）、縮到 1600px、去掉 EXIF（含 GPS）。"""
    if not data:
        raise Invalid("沒有收到圖片。")
    Image.MAX_IMAGE_PIXELS = IMAGE_MAX_PIXELS
    try:
        im = Image.open(io.BytesIO(data))
        if im.format not in IMAGE_FORMATS:
            raise Invalid("只接受 JPG、PNG、WebP、GIF 圖片。")
        if im.width * im.height > IMAGE_MAX_PIXELS:
            raise Invalid("圖片尺寸太大，請縮小後再上傳。")
        im.load()
    except Invalid:
        raise
    except Exception:
        raise Invalid("無法讀取這個圖片檔，請換一張或改存成 JPG 再試。")
    im = ImageOps.exif_transpose(im)
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if has_alpha else "RGB")
    im.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=82, method=4)
    out = buf.getvalue()
    return out, hashlib.sha256(out).hexdigest()[:16] + ".webp"


def make_thumb(data: bytes) -> bytes:
    """處理過的 WebP → 卡片圖：縮到剛好蓋滿 720×480（裁切交給網頁 object-fit），小圖不放大。"""
    im = Image.open(io.BytesIO(data))
    im.load()
    w, h = im.size
    s = min(1.0, max(THUMB_W / w, THUMB_H / h))
    if s < 1:
        im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=78, method=4)
    return buf.getvalue()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o644)
    tmp.replace(path)


def save_image(media_dir: Path, data: bytes, thumb: bool = False) -> str:
    """處理後存到官網的 media/uploads；同一張圖只存一次。thumb＝順便做卡片圖（封面圖）。回傳檔名。"""
    out, name = process_image(data)
    return store_image(media_dir, out, name, thumb)


def store_image(media_dir: Path, out: bytes, name: str, thumb: bool = False) -> str:
    """process_image 處理好的圖存檔（已經存過就不再寫）。"""
    path = media_dir / name
    if not path.exists():
        _write(path, out)
    if thumb:
        ensure_thumb(media_dir, name)
    return name


def ensure_thumb(media_dir: Path, name: str) -> bool:
    """卡片圖不在就從原圖做一張（舊的封面圖也能補）；原圖不在回 False。"""
    src, dst = image_path(media_dir, name), thumb_path(media_dir, name)
    if not src or not src.exists():
        return False
    if not dst.exists():
        _write(dst, make_thumb(src.read_bytes()))
    return True


def image_path(media_dir: Path, name: str) -> Optional[Path]:
    """檔名合格才組路徑（擋 ../ 之類）。"""
    return media_dir / name if IMAGE_NAME_RE.match(name or "") else None


def thumb_path(media_dir: Path, name: str) -> Optional[Path]:
    return media_dir / THUMB_DIR / name if IMAGE_NAME_RE.match(name or "") else None


# ── 官網網址（John 2026-10-22：存一個地方，換網域時一次改好）──────────────
#   存在 y1web.web_settings k='site'。官網的分享連結、QR 貼紙、sitemap，和 y1crm 的「在官網查看」、
#   保養提醒訊息都讀這一個；兩邊 config.toml 的網址只在讀不到時備用。

def clean_site_url(v: Any) -> str:
    """https://ap888.duckdns.org:5177/ → https://ap888.duckdns.org:5177；格式不對丟 Invalid。"""
    v = str(v or "").strip().rstrip("/")
    scheme, sep, rest = v.partition("://")
    v = scheme.lower() + sep + rest.lower()
    if not SITE_URL_RE.match(v):
        raise Invalid("官網網址請寫成 https://網域 或 https://網域:port，不要加路徑。")
    return v


def get_site_url(cur, fallback: str) -> str:
    try:
        cur.execute("SELECT v FROM y1web.web_settings WHERE k='site'")
        r = cur.fetchone()
    except Exception:
        r = None
    v = r["v"] if r else None
    v = json.loads(v) if isinstance(v, (str, bytes)) else (v or {})
    url = str(v.get("base_url") or "").rstrip("/")
    return url if SITE_URL_RE.match(url) else fallback.rstrip("/")


def set_site_url(cur, url: str, actor: str) -> str:
    url = clean_site_url(url)
    cur.execute("INSERT INTO y1web.web_settings (k, v, updated_by) VALUES ('site', %s, %s) "
                "ON DUPLICATE KEY UPDATE v=VALUES(v), updated_by=VALUES(updated_by)",
                (json.dumps({"base_url": url}), actor))
    return url


# 門市 YouTube 頻道：官網頁尾、服務據點和 LINE、FB、Google 圖示並列（John 2026-11-08），也放進結構化資料 sameAs。
#   存在 y1web.web_settings k='youtube'；scripts/youtube_auth.py 授權（login）和檢查（check）時寫入。
#   用頻道 id 的網址：帳號代號（@…）可以改，id 不會變。和 y1web app/content.py YOUTUBE_CHANNEL_RE 同規則
YOUTUBE_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def set_youtube_channel(cur, channel_id: str, actor: str) -> str:
    if not YOUTUBE_CHANNEL_ID_RE.match(channel_id or ""):
        raise Invalid("YouTube 頻道 id 格式不對。")
    url = f"https://www.youtube.com/channel/{channel_id}"
    cur.execute("INSERT INTO y1web.web_settings (k, v, updated_by) VALUES ('youtube', %s, %s) "
                "ON DUPLICATE KEY UPDATE v=VALUES(v), updated_by=VALUES(updated_by)",
                (json.dumps({"url": url, "channel_id": channel_id}), actor))
    return url


# ── 資料存取（cur＝y1crm 的 DictCursor）────────────────────────

def _row(r: Dict[str, Any]) -> Dict[str, Any]:
    extra = r.get("extra")
    extra = json.loads(extra) if isinstance(extra, (str, bytes)) else (extra or {})
    pub = r.get("publish_at")
    return {**r, "extra": extra, "publish_date": pub.date().isoformat() if pub else "",
            "updated_at": r["updated_at"].strftime("%Y-%m-%d %H:%M") if r.get("updated_at") else ""}


def list_posts(cur, kind: str) -> List[Dict[str, Any]]:
    # 關於我們、使用小教室、補助小幫手照排序（就是官網上由上而下的順序）；專欄與好評新的在前
    order = "sort, id" if kind in ("about", "care", "subsidy") else "(publish_at IS NULL) DESC, publish_at DESC, id DESC"
    cur.execute(f"SELECT id, kind, slug, title, summary, body_md, category, status, publish_at, sort, "
                f"cover, extra, updated_by, updated_at FROM {TABLE} WHERE kind=%s ORDER BY {order}", (kind,))
    return [_row(r) for r in cur.fetchall()]


def get_post(cur, pid: int) -> Optional[Dict[str, Any]]:
    cur.execute(f"SELECT id, kind, slug, title, cover, body_md, extra FROM {TABLE} "
                f"WHERE id=%s AND kind IN ({','.join(repr(k) for k in KINDS)})", (pid,))
    return cur.fetchone()


def _slug_taken(cur, kind: str, slug: str, pid: Optional[int] = None) -> bool:
    cur.execute(f"SELECT id FROM {TABLE} WHERE kind=%s AND slug=%s", (kind, slug))
    r = cur.fetchone()
    return bool(r) and r["id"] != pid


def save_post(cur, clean: Dict[str, Any], actor: str, pid: Optional[int] = None) -> int:
    if _slug_taken(cur, clean["kind"], clean["slug"], pid):
        raise Invalid(f"網址代稱「{clean['slug']}」已經有人用了，請換一個。")
    h = clean["extra"].get("hash")
    if h:                                   # 評論轉貼：同一則（原文相同、或 Google 評論代號相同）只轉貼一次
        cur.execute(f"SELECT id, title FROM {TABLE} WHERE kind='share' AND id<>%s "
                    f"AND (JSON_UNQUOTE(JSON_EXTRACT(extra, '$.hash'))=%s "
                    f"OR JSON_UNQUOTE(JSON_EXTRACT(extra, '$.review_id'))=%s)",
                    (pid or 0, h, clean["extra"].get("review_id") or "-"))
        r = cur.fetchone()
        if r:
            raise Invalid(f"這則評論已經轉貼過了（{r['title']}）。")
    vals = (clean["slug"], clean["title"], clean["summary"], clean["body_md"], clean["category"],
            clean["status"], clean["publish_at"], clean["sort"], json.dumps(clean["extra"], ensure_ascii=False), actor)
    if pid is None:
        cur.execute(f"INSERT INTO {TABLE} (slug, title, summary, body_md, category, status, publish_at, sort, extra, "
                    f"updated_by, kind) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", vals + (clean["kind"],))
        return cur.lastrowid
    cur.execute(f"UPDATE {TABLE} SET slug=%s, title=%s, summary=%s, body_md=%s, category=%s, status=%s, "
                f"publish_at=%s, sort=%s, extra=%s, updated_by=%s WHERE id=%s AND kind=%s",
                vals + (pid, clean["kind"]))
    return pid


def image_in_use(cur, name: str) -> bool:
    """圖片還有沒有被任何內容用到（封面或內文、粉專貼文的配圖），沒有才能刪檔。"""
    cur.execute(f"SELECT COUNT(*) AS n FROM {TABLE} WHERE cover=%s OR body_md LIKE %s", (name, f"%{name}%"))
    if cur.fetchone()["n"] > 0:
        return True
    cur.execute("SELECT COUNT(*) AS n FROM fb_post_drafts WHERE image=%s OR card=%s",   # 同步到專欄時兩邊用同一個檔；card＝粉專圖卡
                (name, name))
    if cur.fetchone()["n"] > 0:
        return True
    from api import stock_art                  # 圖庫照片：暫時沒人用也要留著
    return any(x.get("file") == name for x in stock_art.load())


def remove_if_unused(cur, media_dir: Path, name: Optional[str]) -> None:
    if not name or image_in_use(cur, name):
        return
    for path in (image_path(media_dir, name), thumb_path(media_dir, name)):
        if path and path.exists():
            path.unlink()
