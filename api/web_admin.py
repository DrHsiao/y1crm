"""
web_admin.py — 「官網管理」：編輯官網（y1web，port 8006）的關於韻譯、聽力專欄、好評分享、使用小教室、補助小幫手。

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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from PIL import Image, ImageOps

from api import wording

KINDS = {"about": "關於韻譯", "article": "聽力專欄", "share": "好評分享", "care": "使用小教室", "subsidy": "補助小幫手"}
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
          "url": 300, "author": 60, "original": 5000}
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
        extra: Dict[str, Any] = {}
    elif kind == "about":
        extra = {"en": _text(body, "en", False, "英文小標") or ""}
    elif kind == "care":
        extra = {"types": _picks(body, "types", CARE_TYPES), "video": _url(body, "video", "影片連結")}
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
_REL_RE = re.compile(r"^(?:編輯時間[:：]?\s*|已編輯[:：]?\s*|edited\s+)?(?:"
                     r"(\d+|[一二兩三四五六七八九十]{1,3})\s*(分鐘|小時|天|日|週|周|星期|個月|月|年)前"
                     r"|(a|an|one|\d+)\s+(minute|hour|day|week|month|year)s?\s+ago"
                     r"|(剛剛|昨天|上週|上星期|上個月|去年|just now|yesterday|last week|last month|last year))$", re.I)
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
    out: Dict[str, Any] = {"author": "", "posted": "", "rating": None, "original": "", "owner_reply": "",
                           "warnings": []}
    if not lines:
        out["warnings"].append("沒有貼上內容。")
        return out
    # 評論時間在前幾行：時間之前是帳號、「52 則評論」、星等；之後是評論內容
    t = next((i for i, x in enumerate(lines[:6]) if review_date(x, today)), None)
    if t is not None:
        out["posted"] = review_date(lines[t], today).isoformat()
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
    return {"format": "quote", "platform": plat, "url": url, "rating": rating, "posted": posted,
            "author": author, "person": person, "original": original,
            "hash": hashlib.sha256(_squash(original).encode()).hexdigest()[:16]}


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


# ── 資料存取（cur＝y1crm 的 DictCursor）────────────────────────

def _row(r: Dict[str, Any]) -> Dict[str, Any]:
    extra = r.get("extra")
    extra = json.loads(extra) if isinstance(extra, (str, bytes)) else (extra or {})
    pub = r.get("publish_at")
    return {**r, "extra": extra, "publish_date": pub.date().isoformat() if pub else "",
            "updated_at": r["updated_at"].strftime("%Y-%m-%d %H:%M") if r.get("updated_at") else ""}


def list_posts(cur, kind: str) -> List[Dict[str, Any]]:
    # 關於韻譯、使用小教室、補助小幫手照排序（就是官網上由上而下的順序）；專欄與好評新的在前
    order = "sort, id" if kind in ("about", "care", "subsidy") else "(publish_at IS NULL) DESC, publish_at DESC, id DESC"
    cur.execute(f"SELECT id, kind, slug, title, summary, body_md, category, status, publish_at, sort, "
                f"cover, extra, updated_by, updated_at FROM {TABLE} WHERE kind=%s ORDER BY {order}", (kind,))
    return [_row(r) for r in cur.fetchall()]


def get_post(cur, pid: int) -> Optional[Dict[str, Any]]:
    cur.execute(f"SELECT id, kind, slug, title, cover, body_md FROM {TABLE} "
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
    if h:                                   # 評論轉貼：同一則（原文相同）只轉貼一次
        cur.execute(f"SELECT id, title FROM {TABLE} WHERE kind='share' AND id<>%s "
                    f"AND JSON_UNQUOTE(JSON_EXTRACT(extra, '$.hash'))=%s", (pid or 0, h))
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
    """圖片還有沒有被任何內容用到（封面或內文），沒有才能刪檔。"""
    cur.execute(f"SELECT COUNT(*) AS n FROM {TABLE} WHERE cover=%s OR body_md LIKE %s", (name, f"%{name}%"))
    return cur.fetchone()["n"] > 0


def remove_if_unused(cur, media_dir: Path, name: Optional[str]) -> None:
    if not name or image_in_use(cur, name):
        return
    for path in (image_path(media_dir, name), thumb_path(media_dir, name)):
        if path and path.exists():
            path.unlink()
