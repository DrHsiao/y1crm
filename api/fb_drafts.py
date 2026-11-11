"""
fb_drafts.py — 粉專貼文草稿：每天由本機 AI 寫幾則，在 CRM「粉專貼文」勾選發佈，同步到官網聽力專欄。

John 2026-11-08：「本機 AI 建立 FB 貼文……先從貼文開始，每天產生後讓管理者勾選發佈」；
「生活新聞、醫療時事都可以納入」；「每天早上 6:00 建立貼文，預設勾選。10:30 前如果門市人員沒有拿掉
就是同意貼出」。→ 每日草稿 picked=1，auto_publish 時間（config [social]）到了自動發；
但有錯誤或提醒的、太晚才寫好的（離期限不到 AUTO_REVIEW_MIN 分鐘）不自動發，留給人看。

題材怎麼來的（CATEGORIES）：
- 粉專 62 篇舊貼文（2026-11-08 用本機模型分類）：交機紀錄 17、門市公告 15、節日問候 13、優惠 4，
  衛教和保養各只有 1 篇。交機紀錄要真照片和客人同意，不由 AI 寫；AI 補的是衛教這一塊。
- 國外聽力門市的社群建議（美國聽力學會、Hearing Review）：小技巧、新聞與可靠資料、節日、
  互動提問、自己官網的文章。
每天的組合（plan）：節日（前 3 天起）＞時事新聞＞題庫（保養／家人溝通／聽力保健／互動提問輪流）
＞官網文章或工具。

⚠ 本機 AI＝config.AI（573 那張卡，純文字），**永不帶 images**。和 LINE 客服共用同一個常駐模型，
  所以排程放在開店前（config [social] draft_hour），一則寫完再寫下一則。
⚠ 新聞來自外部 RSS：標題、摘要只當題材交給本機模型，請它用自己的話寫；發文附原文連結。
⚠ 發佈前的檢查和官網分享到粉專相同（價格擋、療效提醒），另外擋【】待填處、提醒品牌與促銷字眼；
  自動發佈只發「完全沒有錯誤和提醒」的。
配圖（api/post_art.py）：寫好就從圖庫挑一張真實照片；有圖的發成相片貼文（連結寫進文字），同步專欄時當封面。
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import random
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

import pymysql
import pymysql.cursors
from PIL import Image

import config
from api import ai_chat, dbpool, fb_page, wording

log = logging.getLogger("y1crm.fbdraft")

CATEGORIES = {
    "news": "時事話題", "care": "保養小教室", "health": "聽力保健", "family": "家人溝通",
    "ask": "互動提問", "season": "節日季節", "web": "官網文章", "tool": "官網工具", "own": "自己出題",
}
STORE_TAG = "#睿聲助聽器中正店"      # 粉專舊貼文一直用的店名標籤
USED_DAYS = 60                         # 用過的題目、新聞、官網文章，這幾天內不再出
PENDING_DAYS = 14                      # 沒處理的草稿只列最近這幾天
NEWS_FRESH_DAYS = 3


def _cfg(key: str, default: Any) -> Any:
    v = (getattr(config, "SOCIAL", None) or {}).get(key)
    return default if v is None else v


def _conn():
    return dbpool.connect(**config.db_params(),
                          cursorclass=pymysql.cursors.DictCursor, autocommit=True)


# ── 題庫 ─────────────────────────────────────────────────────

class Topic(NamedTuple):
    key: str
    cat: str
    seed: str
    path: str = ""                 # 官網工具的路徑（附連結）
    months: Tuple[int, ...] = ()   # 只在這些月份出（季節題）


TOPICS: Tuple[Topic, ...] = (
    # 保養小教室（內容以 ai_chat.KNOWLEDGE 為準）
    Topic("care-dry", "care", "每天睡前把助聽器放進乾燥盒；電池式的要把電池門打開"),
    Topic("care-wax", "care", "耳垢擋板和出聲孔要定期檢查：「沒聲音、變小聲」最常見的原因就是被耳垢堵住"),
    Topic("care-water", "care", "不小心沾到水或流汗受潮：取出電池、擦乾、放乾燥盒，不要用吹風機或微波爐烘"),
    Topic("care-heat", "care", "高溫是助聽器的敵人：不要放在車內、吹風機或烘碗機旁邊"),
    Topic("care-battery", "care", "鋅空電池撕開貼紙後先放 1 分鐘再裝上；長時間不用要把電池取出，避免漏液"),
    Topic("care-charge", "care", "充電式助聽器的好習慣：每晚放回充電盒，充電接點保持乾燥清潔"),
    Topic("care-feedback", "care", "助聽器一直嗶嗶叫（回饋音）：多半是沒戴到位或耳塞尺寸不合，重新戴好；常常發生就回門市調整"),
    Topic("care-visit", "care", "定期回門市做清潔保養和機況檢查，順便請驗配師依使用感受微調"),
    Topic("care-travel", "care", "出門旅行的助聽器小清單：收納盒、乾燥盒、備用電池或充電盒"),
    Topic("care-pets", "care", "拿下來的助聽器要放進收納盒，別隨手放桌上，免得被小孩或寵物拿去玩"),
    Topic("care-glasses", "care", "戴口罩、眼鏡、帽子的人，拿下來時動作慢一點，免得把助聽器一起勾掉、弄丟"),
    Topic("care-cold", "care", "天氣冷的時候電池可能比較不耐用，出門多帶一組備用電池；從冷的室外進到溫暖室內，"
          "助聽器容易起水氣，晚上記得除濕", months=(11, 12, 1, 2)),
    Topic("care-humid", "care", "梅雨季、春天比較潮濕，乾燥盒每天都要用，乾燥劑變色了就要更換", months=(3, 4, 5, 6)),
    Topic("care-sweat", "care", "夏天流汗多，汗水會跑進助聽器：運動後擦乾，晚上一定要除濕；戲水、泡溫泉前先拿下來",
          months=(6, 7, 8, 9)),
    # 聽力保健
    Topic("hl-earbuds", "health", "戴耳機的音量習慣：很多專家建議音量不超過六成、聽一小時讓耳朵休息一下"),
    Topic("hl-noise", "health", "工地、KTV、放鞭炮、看演唱會等很吵的地方，用耳塞保護耳朵"),
    Topic("hl-qtip", "health", "掏耳朵的迷思：棉花棒可能把耳垢推得更深；耳朵不舒服或塞住，請找耳鼻喉科處理"),
    Topic("hl-signs", "health", "聽力可能在變化的生活小徵兆：常請人重複、電視越開越大聲、聚餐聽不清楚；"
          "有這些情形可以做一次聽力檢查"),
    Topic("hl-tinnitus", "health", "耳鳴：先找耳鼻喉科醫師檢查原因，不要自己亂買偏方"),
    Topic("hl-sudden", "health", "突然一邊耳朵聽不到或悶塞，可能是突發性聽損，要儘快到耳鼻喉科就醫，不要拖"),
    Topic("hl-check", "health", "定期做聽力檢查，就像量血壓一樣，可以了解自己的聽力有沒有變化"),
    Topic("hl-firststep", "health", "覺得聽不清楚，第一步先到耳鼻喉科檢查原因，之後再和聽力專業人員討論需不需要助聽器"),
    Topic("hl-highfreq", "health", "為什麼「聽得到、聽不清楚」：很多人是高頻的聲音先變弱，ㄙ、ㄕ、ㄘ、ㄑ 這些音最容易聽錯"),
    Topic("hl-adapt", "health", "剛開始戴助聽器要有適應期：前幾週逐步拉長配戴時間，有不舒服就回門市微調"),
    # 家人溝通
    Topic("fam-face", "family", "和聽力不好的家人說話：先叫名字、面對面、說慢一點、咬字清楚，不用大吼"),
    Topic("fam-restaurant", "family", "家族聚餐選位子：讓長輩背對吵雜的地方、坐靠牆或角落，大家輪流說話"),
    Topic("fam-phone", "family", "長輩講電話聽不清楚：說慢一點、換句話說，或改用視訊讓他看得到嘴型"),
    Topic("fam-tv", "family", "看電視音量常常意見不合：可以開字幕、坐近一點，或到門市問問有沒有適合的收聽配件"),
    Topic("fam-repeat", "family", "長輩說「你說什麼？」的時候，換一個說法往往比大聲重複同一句更有用"),
    Topic("fam-mask", "family", "戴口罩說話看不到嘴型，聽力不好的人會更吃力：說慢、靠近，必要時寫字或用手機打字"),
    Topic("fam-reluctant", "family", "長輩不想戴助聽器的心情：不要說「你耳背」，先陪他去做一次聽力檢查，多一點耐心和鼓勵"),
    Topic("fam-tap", "family", "叫長輩之前，先走到他面前或輕拍肩膀，不要從背後突然說話"),
    Topic("fam-group", "family", "多人聊天時，一次一個人說；要換話題時先提一下，聽力不好的人比較跟得上"),
    # 互動提問（短、邀請留言）
    Topic("ask-tv", "ask", "問大家：家裡長輩看電視，音量通常開到幾格？"),
    Topic("ask-sound", "ask", "問大家：你最喜歡、最想一直聽到的聲音是什麼？例如孫子的笑聲、鳥叫、雨聲"),
    Topic("ask-tip", "ask", "問大家：你和家裡長輩溝通，有什麼好用的小撇步？"),
    Topic("ask-noise", "ask", "問大家：出去吃飯，最怕哪一種吵？"),
    Topic("ask-myth", "ask", "是非題：用棉花棒每天掏耳朵比較乾淨，對不對？請大家在留言猜，答案晚點公布"),
    # 官網工具（附連結）
    Topic("tool-test", "tool", "官網的「線上聽力小測驗」：戴耳機在安靜的地方做，幾分鐘就好；只是自我檢測、不是診斷，"
          "結果僅供參考", path="/hearing-test"),
    Topic("tool-sim", "tool", "官網的「聽損模擬」：讓家人聽聽看，聽力變差的人聽到的聲音是什麼樣子，比較能體諒長輩",
          path="/hearing-sim"),
    Topic("tool-practice", "tool", "官網的「每日聽練」：每天幾分鐘的聽聲音小練習，像打卡一樣養成習慣（不要說會讓聽力變好）",
          path="/practice/"),
    Topic("tool-subsidy", "tool", "官網的「補助小幫手」：整理輔具補助的資格和申請流程，不確定的可以問門市（不要寫任何金額）",
          path="/subsidy"),
    Topic("tool-booking", "tool", "官網可以線上預約到門市，子女也可以幫爸媽預約", path="/booking"),
    Topic("tool-care", "tool", "官網的「使用小教室」：電池、充電、清潔、除濕的圖文教學，忘記了隨時可以查", path="/care"),
)

# 每天的題庫類別輪流（同一天不重複類別）
ROTATION = ("care", "family", "health", "care", "ask", "health", "family")


# ── 節日 ─────────────────────────────────────────────────────
#   農曆節日查表（春節初一、中秋、重陽）；表外的年份就不出，**每隔幾年要補**。
LUNAR = {
    2026: {"cny": (2, 17), "moon": (9, 25), "double9": (10, 18)},
    2027: {"cny": (2, 6), "moon": (9, 15), "double9": (10, 8)},
    2028: {"cny": (1, 26), "moon": (10, 3), "double9": (10, 26)},
    2029: {"cny": (2, 13), "moon": (9, 22), "double9": (10, 16)},
    2030: {"cny": (2, 3), "moon": (9, 12), "double9": (10, 5)},
}
EVENT_LEAD = 3          # 節日前幾天開始出草稿
_HOURS_NOTE = "如果要提節日的營業時間，請寫「【營業時間請店員填寫】」，不要自己編。"
EVENT_NAMES = {"newyear": "新年", "cny": "春節", "hearingday": "世界聽力日", "mother": "母親節",
               "father": "父親節", "moon": "中秋節", "double9": "重陽節"}
EVENT_SEEDS = {
    "newyear": "新年：祝大家新年快樂，新的一年也幫家人安排一次聽力檢查。",
    "cny": "春節：過年家族團聚，提醒大家和長輩聊天的小技巧（面對面、說慢一點、輪流說），也祝大家新年快樂。" + _HOURS_NOTE,
    "hearingday": "3 月 3 日是世界衛生組織訂的「世界聽力日」：提醒大家關心自己和家人的聽力，保護耳朵、定期檢查。",
    "mother": "母親節：感謝媽媽，也提醒大家留意媽媽最近是不是常常聽不清楚，陪她做一次聽力檢查。",
    "father": "父親節（8 月 8 日）：感謝爸爸，也提醒大家留意爸爸的聽力，陪他做一次聽力檢查。",
    "moon": "中秋節：全家團圓烤肉聊天，提醒大家讓長輩坐在聽得清楚的位置、輪流說話，祝大家中秋快樂。" + _HOURS_NOTE,
    "double9": "重陽節（敬老節）：祝長輩身體健康，提醒子女關心爸媽的聽力和生活。" + _HOURS_NOTE,
}


def _second_sunday_may(year: int) -> date:
    d = date(year, 5, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7 + 7)


def events(year: int) -> List[Tuple[date, str]]:
    out = [(date(year, 1, 1), "newyear"), (date(year, 3, 3), "hearingday"),
           (_second_sunday_may(year), "mother"), (date(year, 8, 8), "father")]
    for key, (m, d) in LUNAR.get(year, {}).items():
        when = date(year, m, d)
        out.append((when - timedelta(days=1) if key == "cny" else when, key))   # 春節的貼文放除夕
    return sorted(out)


def event_spec(day: date, used: Set[str]) -> Optional[Dict[str, Any]]:
    for when, key in events(day.year) + events(day.year + 1):
        if 0 <= (when - day).days <= EVENT_LEAD:
            tk = f"ev:{key}:{when.year}"
            if tk not in used:
                return _spec("season", tk, f"{EVENT_NAMES[key]}（{when.month}/{when.day}）",
                             f"節日：{when.month} 月 {when.day} 日。{EVENT_SEEDS[key]}")
    return None


# ── 新聞（生活新聞、醫療時事）──────────────────────────────────
NEWS_FEEDS: Tuple[Tuple[str, str], ...] = (
    ("中央社", "https://feeds.feedburner.com/rsscna/lifehealth"),          # 生活醫藥
    ("衛福部", "https://www.mohw.gov.tw/rss-16-1.html"),                   # 焦點新聞
    ("疾管署", "https://www.cdc.gov.tw/RSS/RssXml/Hh094B49-DRwe2RR4eFfrQ?type=1"),
    ("自由時報", "https://news.ltn.com.tw/rss/life.xml"),                  # 生活
    # 11-09 John「其他媒體也找找，不要每天都自由時報的」：試過能讀、有生活健康新聞的
    # （聯合的 RSS 是空的，中時、TVBS、民視、華視、NOWnews、三立、國健署沒有能用的 RSS）
    ("ETtoday", "https://feeds.feedburner.com/ettoday/health"),           # 健康
    ("公視", "https://news.pts.org.tw/xml/newsfeed.xml"),                 # 全部（Atom 格式）
    ("鏡週刊", "https://www.mirrormedia.mg/rss/rss.xml"),                 # 全部
    ("Yahoo新聞", "https://tw.news.yahoo.com/rss/health"),                # 健康（各家媒體轉載）
    ("Heho健康", "https://heho.com.tw/feed"),
    ("關鍵評論網", "https://feeds.feedburner.com/TheNewsLens"),
)
NEWS_SOURCE_DAYS = 7            # 最近這幾天用過的媒體往後排（不要每天都同一家）
NEWS_SOURCE_PENALTY = 5         # 每用過一次扣幾分：同一家要比別家的相關得多（例如標題有「聽力」）才會再選到
# 關鍵字權重：和聽力直接相關的最高，長輩照顧次之，天氣與一般醫療時事再次之
NEWS_WORDS: Tuple[Tuple[str, int], ...] = (
    ("聽力", 5), ("聽損", 5), ("助聽", 5), ("耳鳴", 5), ("耳朵", 4), ("耳鼻喉", 4), ("中耳炎", 4),
    ("噪音", 4), ("聽覺", 4), ("失智", 3), ("長輩", 3), ("長者", 3), ("高齡", 3), ("銀髮", 3),
    ("老人", 2), ("長照", 2), ("輔具", 3), ("照顧者", 2), ("失能", 2),
    ("寒流", 2), ("低溫", 2), ("東北季風", 2), ("冷氣團", 2), ("降溫", 1), ("高溫", 2), ("熱傷害", 2),
    ("中暑", 2), ("梅雨", 1), ("流感", 2), ("疫苗", 1), ("健保", 1), ("健檢", 2), ("國健署", 2),
    ("衛福部", 1), ("補助", 1), ("防疫", 1), ("健康", 1),
)
# 不碰的題材：災難、犯罪、政治、博弈、炒作
NEWS_BLOCK = ("539", "樂透", "威力彩", "大樂透", "開獎", "命案", "身亡", "死亡", "喪命", "罹難", "車禍", "墜樓",
              "自殺", "輕生", "性侵", "猥褻", "詐騙", "毒品", "槍", "判刑", "起訴", "地檢", "法院", "羈押",
              "立委", "議員", "選舉", "總統", "政黨", "國民黨", "民進黨", "民眾黨", "罷免", "兩岸", "共軍",
              "行政院", "政院", "立法院", "副署", "朝野", "藍白", "藍綠",
              "違建", "房價", "股價", "爆料", "網紅", "藝人")
NEWS_MIN_SCORE = 3
_TAG_RE = re.compile(r"<[^>]+>")


def _plain(s: Optional[str]) -> str:
    t = html.unescape(_TAG_RE.sub(" ", s or ""))
    return re.sub(r"\s+", " ", t).strip()


_BAD_XML = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")     # XML 不允許的控制字元（11-09 自由時報的 RSS 夾了 \x08，整份讀不進來）
_ATOM = "{http://www.w3.org/2005/Atom}"


def _when(s: Optional[str]) -> Optional[datetime]:
    """RSS 的 pubDate（RFC 822）或 Atom 的 updated（ISO 8601）→ 本機時間；沒有或看不懂回 None。"""
    s = (s or "").strip()
    try:
        when = datetime.fromisoformat(s) if s[:4].isdigit() else parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        return None                     # 疾管署的 RSS 沒有日期：清單本身只放最近的
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def parse_rss(data: bytes, source: str) -> List[Dict[str, Any]]:
    """RSS 2.0 或 Atom（公視）→ [{source, title, desc, link, when}]。外部資料：只取純文字、截長度。"""
    root = ET.fromstring(_BAD_XML.sub(b"", data))
    rows = [(it.findtext("title"), it.findtext("link"), it.findtext("description"), it.findtext("pubDate"))
            for it in root.iter("item")]
    for it in root.iter(f"{_ATOM}entry"):
        links = it.findall(f"{_ATOM}link")
        href = next((x.get("href") for x in links if x.get("rel", "alternate") == "alternate"), None)
        rows.append((it.findtext(f"{_ATOM}title"), href,
                     it.findtext(f"{_ATOM}summary") or it.findtext(f"{_ATOM}content"),
                     it.findtext(f"{_ATOM}published") or it.findtext(f"{_ATOM}updated")))
    out: List[Dict[str, Any]] = []
    for title, link, desc, when in rows:
        title, link = _plain(title), (link or "").strip()
        if not title or not link.startswith("https://") or len(link) > 500:
            continue
        out.append({"source": source, "title": title[:200], "desc": _plain(desc)[:400], "link": link,
                    "when": _when(when)})
    return out


def news_score(item: Dict[str, Any]) -> int:
    text = f"{item['title']} {item['desc']}"
    if any(w in text for w in NEWS_BLOCK):
        return 0
    return (sum(w * 2 for k, w in NEWS_WORDS if k in item["title"])
            + sum(w for k, w in NEWS_WORDS if k in item["desc"]))


def news_key(link: str) -> str:
    return "news:" + hashlib.sha1(link.encode()).hexdigest()[:16]


def pick_news(items: Iterable[Dict[str, Any]], used: Set[str],
              now: Optional[datetime] = None, recent: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """可以用的新聞，分數高的在前（同分新的在前）。recent＝{媒體: 最近用過幾次}：每次扣 NEWS_SOURCE_PENALTY 分，
    換別家的新聞；扣完不影響門檻（相關的新聞還是可以用，只是排後面）。"""
    now = now or datetime.now()
    recent = recent or {}
    ok = []
    for it in items:
        s = news_score(it)
        if s < NEWS_MIN_SCORE or news_key(it["link"]) in used:
            continue
        if it["when"] and (now - it["when"]).days >= NEWS_FRESH_DAYS:
            continue
        ok.append((s - NEWS_SOURCE_PENALTY * recent.get(it["source"], 0), it["when"] or now, it))
    ok.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [it for _, _, it in ok]


def fetch_news(feeds: Sequence[Tuple[str, str]] = NEWS_FEEDS) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for source, url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (y1crm)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                out += parse_rss(r.read(2_000_000), source)
        except Exception as e:  # noqa: BLE001 — 一個來源壞了不影響其他
            log.warning("新聞來源 %s 讀取失敗：%s", source, e)
    return out


def news_spec(it: Dict[str, Any]) -> Dict[str, Any]:
    brief = (f"新聞標題：{it['title']}\n新聞摘要：{it['desc'] or '（沒有摘要）'}\n"
             f"來源：{it['source']}")
    return _spec("news", news_key(it["link"]), it["title"], brief, link=it["link"],
                 source_name=it["source"], source_url=it["link"])


# ── 每天的組合 ───────────────────────────────────────────────

def _spec(cat: str, key: str, topic: str, brief: str, link: Optional[str] = None,
          web_post_id: Optional[int] = None, source_name: Optional[str] = None,
          source_url: Optional[str] = None) -> Dict[str, Any]:
    return {"category": cat, "topic_key": key, "topic": topic[:200], "brief": brief, "link": link,
            "web_post_id": web_post_id, "source_name": source_name, "source_url": source_url}


def topic_spec(cat: str, day: date, used: Set[str], rng: random.Random,
               site: str = "") -> Optional[Dict[str, Any]]:
    pool = [t for t in TOPICS if t.cat == cat and (not t.months or day.month in t.months)]
    fresh = [t for t in pool if t.key not in used]
    if not (fresh or pool):
        return None
    t = rng.choice(fresh or pool)
    link = f"{site.rstrip('/')}{t.path}" if t.path and site else None
    return _spec(cat, t.key, topic_title(t.seed), f"題材：{t.seed}", link=link)


def topic_title(seed: str) -> str:
    """題庫的題材 → 畫面上的短標題：「問大家：…」「是非題：…」取冒號後面，其他取冒號前面。"""
    head, _, rest = seed.partition("：")
    return (rest if rest and len(head) <= 4 else head)[:40]


def web_spec(items: Sequence[Dict[str, Any]], used: Set[str]) -> Optional[Dict[str, Any]]:
    for w in items:
        key = f"web:{w['id']}"
        if key not in used:
            brief = f"官網文章標題：{w['title']}\n摘要：{w.get('summary') or '（沒有摘要）'}"
            if w.get("excerpt"):
                brief += f"\n內文開頭：{w['excerpt']}"
            return _spec("web", key, w["title"], brief, link=w["url"], web_post_id=w["id"])
    return None


def rotation(day: date) -> List[str]:
    i = day.toordinal() % len(ROTATION)
    seq = list(ROTATION[i:] + ROTATION[:i])
    return list(dict.fromkeys(seq))          # 去重、保留順序


def plan(day: date, n: int, used: Set[str], news: Sequence[Dict[str, Any]] = (),
         web_items: Sequence[Dict[str, Any]] = (), site: str = "",
         rng: Optional[random.Random] = None) -> List[Dict[str, Any]]:
    """這一天要寫哪幾則（純函式）：節日＞新聞＞題庫＞官網文章或工具＞其他題庫類別。"""
    rng = rng or random.Random(day.toordinal())
    out: List[Dict[str, Any]] = []

    def add(s: Optional[Dict[str, Any]]) -> None:
        if s and len(out) < n and all(s["topic_key"] != x["topic_key"] for x in out):
            out.append(s)

    add(event_spec(day, used))
    add(news_spec(news[0]) if news else None)
    cats = rotation(day)
    add(topic_spec(cats[0], day, used, rng, site))
    add(web_spec(web_items, used) or topic_spec("tool", day, used, rng, site))
    for c in cats[1:]:
        if len(out) >= n:
            break
        if all(x["category"] != c for x in out):
            add(topic_spec(c, day, used, rng, site))
    return out


def single_spec(cat: str, day: date, used: Set[str], news: Sequence[Dict[str, Any]] = (),
                web_items: Sequence[Dict[str, Any]] = (), site: str = "") -> Optional[Dict[str, Any]]:
    """管理者按「多寫一則」：指定類別挑一個題目。"""
    rng = random.Random()
    if cat == "news":
        return news_spec(news[0]) if news else None
    if cat == "season":
        return event_spec(day, used)
    if cat == "web":
        return web_spec(web_items, used)
    return topic_spec(cat, day, used, rng, site)


def own_spec(brief: str, link: Optional[str] = None) -> Dict[str, Any]:
    b = brief.strip()
    return _spec("own", "own:" + hashlib.sha1(b.encode()).hexdigest()[:16], b[:40], f"題材：{b}", link=link)


# ── 提示詞與清稿 ─────────────────────────────────────────────
CAT_GUIDE = {
    "news": "這是今天的一則新聞。請從新聞延伸到讀者（長輩和家屬）的生活或聽力照顧，給一個實用的小提醒。"
            "新聞內容只能用下面寫的，不可以補充細節或數字，用自己的話說，開頭簡短帶到新聞即可，"
            "可以寫「根據{source}報導」。"
            "和聽力的連結只從生活面切入：陪長輩出門辦事、和醫護或家人溝通、助聽器的日常照顧；"
            "不要推測新聞裡的事會讓身體或耳朵出現什麼狀況。",
    "care": "題材是助聽器的日常保養，內容以門市知識為準。",
    "health": "題材是聽力保健。講到身體或聽力狀況，一律建議找耳鼻喉科醫師或聽力專業人員評估；"
              "寫到突然聽不到，請讀者儘快看耳鼻喉科。",
    "family": "題材是和聽力不好的家人溝通的小技巧，寫給子女和家人看。",
    "ask": "這是互動貼文，全文 60～120 字：用輕鬆的語氣問大家一個問題，邀請在留言分享。",
    "season": "這是節日或季節貼文：先給讀者溫暖的祝福，再帶一個和聽力或助聽器照顧有關的小提醒。",
    "web": "請替官網的這篇文章寫一則導讀，全文 80～150 字，點出 1～2 個重點，最後請大家點連結看全文。",
    "tool": "請介紹官網上的這個工具，全文 80～150 字，說明誰適合用、怎麼用，最後請大家點連結試試。",
    "own": "題材是門市管理者指定的，照著寫。",
}


def store_lines(store: Dict[str, Any]) -> str:
    hours = store.get("hours")
    if isinstance(hours, list):
        hours = "、".join(f"{h.get('days', '')} {h.get('time', '')}".strip() for h in hours if isinstance(h, dict))
    rows = [f"店名：{store.get('name') or '睿聲助聽器-中正門市'}",
            f"電話：{store.get('phone') or ''}", f"地址：{store.get('address') or ''}",
            f"營業時間：{hours or '週一至週六 09:00–18:00，週日公休'}",
            "預約：LINE 官方帳號輸入「預約」，或官網線上預約"]
    return "\n".join(r for r in rows if not r.endswith("："))


def system_prompt(store: Dict[str, Any]) -> str:
    return f"""你是台北市中正區一家助聽器門市的 Facebook 粉專小編。讀者多半是長輩和他們的子女。

寫法：
1. 繁體中文、台灣用語，口語、溫暖、實用，像門市的人在跟熟客聊天。
2. 除非另有指定，全文 120～250 字，分 2～4 小段；可以用 1～3 個表情符號。
3. 不要用 markdown：不要 **粗體**、不要 # 標題；要條列就用「・」開頭。
   稱呼一律用「助聽器」，不要叫「耳機」。
4. 開頭一句用生活情境或提問吸引注意，中間給 1～3 個具體、做得到的小建議，
   結尾一句溫和的邀請（例如聽力或助聽器有問題歡迎來電或到門市聊聊），不要硬推銷。
5. 最後一行放 2～4 個 hashtag，第一個固定是 {STORE_TAG}。
6. 只寫貼文本身：不要標題、前言、說明、引號，也不要寫網址。
   官網連結（每日聽練、線上聽力測驗、預約）系統會另外加在文末，內文不用再介紹。

一定要遵守（助聽器是醫療器材，廣告受法規限制）：
- 不提任何價格、費用、折扣、優惠、免費、贈品、抽獎。
- 不寫療效或保證：不說「戴了就聽得清楚」「改善聽力」「預防失智」「延緩退化」，
  不用「最好、第一、唯一、保證」這類用語，不做使用前後的比較。
- 不提助聽器品牌、型號。
- 不做醫療診斷；身體或聽力狀況一律建議找耳鼻喉科醫師或聽力專業人員評估。
- 身體不適要就醫：寫到身體不舒服，一律請讀者看醫師或問醫師、藥師；不要自己推測什麼事會引起什麼症狀。
  「歡迎來電或到門市」只能接在聽力、助聽器的問題後面，不可以請讀者為了身體不適來電或到門市；
  也不用在貼文裡寫門市不處理什麼。
- 不編造客人故事或對話，不寫「有位客人……」；不編造數字、研究、日期或活動。
- 助聽器的使用和保養做法，只能用下面「門市知識」裡有的；知識裡沒有的，就請讀者帶到門市
  讓驗配師看，不要自己發明做法（例如不可以叫人出門前關機）。
- 不談政治、宗教，不描寫災難或事故的細節。

門市資料（要提時以此為準）：
{store_lines(store)}

保養與使用知識（寫保養題材時以此為準）：
{_care_knowledge()}
"""


def _care_knowledge() -> str:
    """LINE 客服知識庫裡的保養、電池、常見狀況三段（門市地址電話另外給，不重複）。"""
    k = ai_chat.KNOWLEDGE
    a, b = k.find("【日常保養】"), k.find("【服務】")
    return k[a:b].strip() if 0 <= a < b else k.strip()


def user_prompt(spec: Dict[str, Any]) -> str:
    guide = CAT_GUIDE.get(spec["category"], "").format(source=spec.get("source_name") or "新聞")
    return f"類別：{CATEGORIES.get(spec['category'], '')}\n{guide}\n\n{spec['brief']}"


_HEADING_RE = re.compile(r"^#{1,6}[ \t]+", re.M)          # markdown 標題（hashtag 後面沒有空白，不受影響）
_BULLET_RE = re.compile(r"^[ \t]*[-*•][ \t]+", re.M)
_URL_RE = re.compile(r"\(?https?://[^\s）)]+\)?")
_TAIL_TAGS_RE = re.compile(r"(?:[ \t]*#[^\s#]+)+[ \t]*$")


def clean_post(text: str) -> str:
    t = ai_chat._clean_reply(text)                 # 思考鏈、標頭、粗體、整段引號
    t = _HEADING_RE.sub("", t)
    t = _BULLET_RE.sub("・", t)
    t = _URL_RE.sub("", t)                          # 模型自己寫的網址不可信，連結另外附
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    m = _TAIL_TAGS_RE.search(t)
    if m and m.start() > 0 and t[m.start() - 1] != "\n":      # 結尾的 hashtag 黏在本文後面：拆成獨立一行
        t = f"{t[:m.start()].rstrip()}\n\n{m.group(0).strip()}"
    if t and STORE_TAG not in t:
        lines = t.split("\n")
        if lines[-1].lstrip().startswith("#"):
            lines[-1] = f"{STORE_TAG} {lines[-1].strip()}"
        else:
            lines += ["", STORE_TAG]
        t = "\n".join(lines)
    return t


# ── 每篇固定的官網宣傳（John 2026-11-08：「每篇貼文下方都留有網站的宣傳，包含主打的聽力鍛練和線上聽檢」）──
#   由程式加，不讓 AI 寫（網址、名稱一字不差）；放在本文和 hashtag 之間。名稱和官網選單一致。
FOOTER_HEAD = "—— 韻譯助聽器・官網 ——"
FOOTER_LINKS: Tuple[Tuple[str, str, str], ...] = (
    ("🎧", "每日聽練（每天幾分鐘的聽聲音練習）", "/practice/"),
    ("👂", "線上聽力測驗（在家先做自我檢測）", "/hearing-test"),
    ("📅", "線上預約到門市", "/booking"),
)


def footer(site: str) -> str:
    site = site.rstrip("/")
    return "\n".join([FOOTER_HEAD] + [f"{icon} {label}：{site}{path}" for icon, label, path in FOOTER_LINKS])


def add_footer(text: str, site: str) -> str:
    """本文 → 官網宣傳 → hashtag。已經有了就不重複加；沒有官網網址就不加。"""
    if not site or FOOTER_HEAD in text:
        return text
    lines = text.rstrip().split("\n")
    tags: List[str] = []
    while lines and lines[-1].lstrip().startswith("#"):
        tags.insert(0, lines.pop())
    body = "\n".join(lines).rstrip()
    return "\n\n".join(x for x in (body, footer(site), "\n".join(tags)) if x)


# ── 發文前檢查 ───────────────────────────────────────────────
PLACEHOLDER_RE = re.compile(r"【[^】]{0,30}(填|確認)[^】]{0,30}】")
PROMO_WORDS = ("免費", "贈品", "抽獎", "預防失智", "延緩失智", "降低失智", "延緩退化")
BRANDS = ("phonak", "峰力", "resound", "瑞聲達", "interton", "audibel", "oticon", "奧迪康", "signia",
          "西嘉", "widex", "唯聽", "starkey", "史達克", "unitron", "bernafon")


def check(text: str) -> Tuple[List[str], List[str]]:
    """回 (錯誤, 提醒)：錯誤不能發；提醒要管理者確認。"""
    errors, warns = fb_page.check_post_text(text)
    if PLACEHOLDER_RE.search(text or ""):
        errors.append("還有【】裡要填的地方，請改好再發。")
    warns += [f"「{w}」可能被當成促銷或療效宣傳" for w in PROMO_WORDS if w in (text or "")]
    low = (text or "").lower()
    warns += [f"提到品牌「{b}」" for b in BRANDS if b in low]
    if "耳機" in (text or ""):
        warns.append("寫到「耳機」：如果指的是助聽器，請改成「助聽器」")
    return errors, warns


def column_errors(r: Dict[str, Any]) -> List[str]:
    """要同步到聽力專欄的：專欄文章的欄位先檢查過，免得粉專發了、專欄卻存不進去。"""
    from api import web_admin
    seo = _json(r.get("seo"))
    if not seo.get("title"):
        return ["要同步到聽力專欄，請填官網標題。"]
    try:
        body = column_post(r)
        body["slug"] = body["slug"] or "x"
        web_admin.clean_post("article", body)
    except web_admin.Invalid as e:
        return [f"聽力專欄：{e}"]
    return []


# ── 本機模型 ─────────────────────────────────────────────────

def _chat(messages: List[Dict[str, str]], timeout: float, json_mode: bool = False,
          temperature: float = 0.7, num_predict: int = 700) -> str:
    """⚠ 純文字（config.AI＝573 那張卡），永不帶 images。"""
    payload = {
        "model": config.AI.get("model"), "messages": messages, "stream": False,
        "think": False, "keep_alive": -1,
        "options": {"temperature": temperature, "num_predict": num_predict, "num_ctx": 8192},
    }
    if json_mode:
        payload["format"] = "json"
    req = urllib.request.Request(config.AI.get("url", "").rstrip("/") + "/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ((json.loads(r.read()).get("message") or {}).get("content") or "").strip()


def write(spec: Dict[str, Any], store: Dict[str, Any], site: str = "",
          timeout: float = 180) -> Dict[str, Any]:
    """寫一則（最後加上官網宣傳）。價格字眼會請模型重寫一次；回 {message, meta}。失敗丟例外。"""
    t0 = time.time()
    msgs = [{"role": "system", "content": system_prompt(store)},
            {"role": "user", "content": user_prompt(spec)}]
    text, best = "", None
    for attempt in range(3):
        raw = _chat(msgs, timeout)
        text = clean_post(raw)
        if not text or wording.price_word(text):
            fix = "不可以提到價格、費用、優惠或免費，也不能是空的，請整篇重寫。"
        else:
            warns = check(text)[1]
            if best is None or len(warns) < best[1]:
                best = (text, len(warns))
            if not warns or attempt == 2:
                break
            # 有提醒的不會自動發：請模型改掉那幾個字眼再試一次（例如把助聽器寫成「耳機」）
            fix = "請改掉這些問題，其他內容照舊，整篇重新給我：" + "；".join(warns)
        msgs += [{"role": "assistant", "content": raw}, {"role": "user", "content": fix}]
    if best is None:
        raise RuntimeError("本機 AI 沒有寫出內容")
    text = best[0]
    text = add_footer(text, site)[:fb_page.MAX_POST]
    try:
        seo = seo_meta(text, spec["category"], timeout)
    except Exception as e:  # noqa: BLE001 — 官網版資料擬不出來不影響貼文本身，管理者可以自己填
        log.warning("官網版標題、關鍵字沒擬出來：%s", e)
        seo = clean_seo({}, spec["category"], text)
    return {"message": text, "seo": seo, "web_sync": spec["category"] in SYNC_DEFAULT,
            "meta": f"{config.AI.get('model')} {time.time() - t0:.0f}s"}


# ── 同步到官網「聽力專欄」────────────────────────────────────
#   John 2026-11-08：「同步在網站中，加入搜索 SEO 關鍵字」「聽力專欄中，想個方案實施」。
#   發到粉專成功後，勾了「同步到聽力專欄」的就在專欄發一篇：內文＝粉專本文（拿掉官網宣傳和 hashtag），
#   標題、摘要、分類、搜尋關鍵字、網址代稱由本機 AI 先擬（seo_meta），管理者發佈前可以改。
#   互動提問、節日、官網文章導讀、官網工具介紹不適合放專欄，預設不勾（管理者還是可以勾）。
SYNC_DEFAULT = ("care", "health", "family", "news", "own")
COLUMN_OF = {"care": "助聽器知識", "health": "聽力保健", "family": "生活樂活", "news": "生活樂活"}
LOCAL_KEYWORDS = ("台北助聽器", "中正區助聽器")     # 每篇都帶的地區詞（門市在台北市中正區）
SEO_TITLE_MAX, SEO_SUMMARY_MAX = 40, 120
_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9]+")

SEO_PROMPT = """下面是一則助聽器門市的粉專貼文，要同步放到門市官網的「聽力專欄」。請替它擬官網版的資料，只回 JSON：
{"title": "15～28 字的文章標題，自然地包含一個客人會搜尋的詞；不要驚嘆號、表情符號",
 "summary": "40～80 字的摘要（會出現在 Google 搜尋結果），平實說明這篇在講什麼",
 "category": "從 @CATS@ 選一個",
 "keywords": ["4～6 個台灣人會在 Google 搜尋的詞，每個 2～10 字，例如：助聽器保養、長輩聽不清楚"],
 "slug": "3～6 個英文小寫單字，用 - 連接，例如 hearing-aid-winter-care"}
規則：不可以有價格、優惠、免費、療效、保證、最好、第一這類字眼，也不要放助聽器品牌或型號。

貼文：
@TEXT@"""


def _json(v: Any) -> Dict[str, Any]:
    if isinstance(v, (str, bytes)):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def strip_extras(message: str) -> str:
    """粉專貼文 → 本文：拿掉官網宣傳那一段和整行都是 hashtag 的行。"""
    out: List[str] = []
    skip = False
    for line in (message or "").split("\n"):
        t = line.strip()
        if t == FOOTER_HEAD:
            skip = True
            continue
        if skip:
            skip = bool(t)              # 宣傳段到空行為止
            continue
        if t and all(w.startswith("#") for w in t.split()):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def column_body(message: str, source_name: Optional[str] = None, source_url: Optional[str] = None) -> str:
    """專欄文章的 Markdown：「・」條列改成清單；新聞附資料來源。"""
    lines: List[str] = []
    for line in strip_extras(message).split("\n"):
        item = line.lstrip().startswith("・")
        prev = lines[-1] if lines else ""
        if line.strip() and prev.strip() and item != prev.startswith("- "):
            lines.append("")            # 清單前後要空一行
        lines.append("- " + line.lstrip()[1:].strip() if item else line)
    body = "\n".join(lines).strip()
    if source_url:
        body += f"\n\n資料來源：[{source_name or '新聞原文'}]({source_url})"
    return body


def _ok_keyword(k: str) -> bool:
    from api import web_admin
    try:
        web_admin.clean_keywords([k])
    except web_admin.Invalid:
        return False
    return not any(b in k.lower() for b in BRANDS) and not any(w in k for w in PROMO_WORDS)


def clean_seo(d: Dict[str, Any], category: str, message: str) -> Dict[str, Any]:
    """AI 擬的官網版資料 → 可以用的值（不合格的丟掉或改用預設）。"""
    from api import web_admin
    body = strip_extras(message)
    first = re.split(r"[。！？!?\n]", body, maxsplit=1)[0].strip()

    def ok_text(v: Any, limit: int) -> str:
        t = _plain(str(v or "")).strip(" 「」『』\"'")
        bad = wording.price_word(t) or wording.claim_words(t) or any(w in t for w in PROMO_WORDS)
        return "" if bad else t[:limit]

    title = ok_text(d.get("title"), SEO_TITLE_MAX) or first[:28]
    summary = ok_text(d.get("summary"), SEO_SUMMARY_MAX) or re.sub(r"\s+", " ", body)[:80]
    cat = d.get("category") if d.get("category") in web_admin.COLUMN_CATEGORIES else COLUMN_OF.get(category, "生活樂活")
    kws: List[str] = []
    for k in (d.get("keywords") if isinstance(d.get("keywords"), list) else []):
        k = re.sub(r"\s+", " ", str(k)).strip().lstrip("#＃")
        if k and k not in kws and _ok_keyword(k):
            kws.append(k)
    kws = kws[:web_admin.KEYWORDS_MAX - len(LOCAL_KEYWORDS)] + [k for k in LOCAL_KEYWORDS if k not in kws]
    slug = _SLUG_CLEAN_RE.sub("-", str(d.get("slug") or "").lower()).strip("-")[:60].strip("-")
    if not web_admin.SLUG_RE.match(slug) or len(slug) < 3:
        slug = ""
    return {"title": title, "summary": summary, "category": cat, "keywords": kws, "slug": slug}


def seo_meta(message: str, category: str, timeout: float = 120) -> Dict[str, Any]:
    from api import web_admin
    prompt = (SEO_PROMPT.replace("@CATS@", "、".join(web_admin.COLUMN_CATEGORIES))
              .replace("@TEXT@", strip_extras(message)[:2000]))
    raw = _chat([{"role": "user", "content": prompt}], timeout, json_mode=True, temperature=0.3)
    return clean_seo(_json(raw), category, message)


def clean_seo_input(v: Any) -> Dict[str, Any]:
    """管理者改的官網版資料（從 CRM 頁面送來）：格式不對丟 web_admin.Invalid。"""
    from api import web_admin
    d = v if isinstance(v, dict) else {}
    title = str(d.get("title") or "").strip()
    summary = str(d.get("summary") or "").strip()
    if len(title) > SEO_TITLE_MAX:
        raise web_admin.Invalid(f"官網標題請在 {SEO_TITLE_MAX} 字以內。")
    if len(summary) > SEO_SUMMARY_MAX:
        raise web_admin.Invalid(f"官網摘要請在 {SEO_SUMMARY_MAX} 字以內。")
    cat = d.get("category") or ""
    if cat not in web_admin.COLUMN_CATEGORIES:
        raise web_admin.Invalid("請選專欄分類。")
    slug = str(d.get("slug") or "").strip().lower()
    if slug and not web_admin.SLUG_RE.match(slug):
        raise web_admin.Invalid("網址代稱只能用英文小寫、數字和「-」。")
    return {"title": title, "summary": summary, "category": cat,
            "keywords": web_admin.clean_keywords(d.get("keywords")), "slug": slug}


def column_post(r: Dict[str, Any]) -> Dict[str, Any]:
    """草稿 → 要送給 web_admin.clean_post 的專欄文章欄位。"""
    seo = _json(r.get("seo"))
    return {"kind": "article", "status": "published", "title": seo.get("title") or "",
            "summary": seo.get("summary") or "", "category": seo.get("category") or "",
            "keywords": seo.get("keywords") or [], "slug": seo.get("slug") or "",
            "body_md": column_body(r["message"], r.get("source_name"), r.get("source_url"))}


def _free_slug(cur, base: str) -> str:
    from api import web_admin
    slug, n = base, 2
    while True:
        cur.execute(f"SELECT 1 FROM {web_admin.TABLE} WHERE kind='article' AND slug=%s", (slug,))
        if cur.fetchone() is None:
            return slug
        slug, n = f"{base[:70]}-{n}", n + 1


def sync_column(cur, r: Dict[str, Any], by: str) -> Tuple[int, str]:
    """在聽力專欄發一篇（網址代稱撞到就加 -2、-3）。回 (文章 id, 網址代稱)。不合格丟 web_admin.Invalid。"""
    from api import web_admin
    body = column_post(r)
    body["slug"] = _free_slug(cur, body["slug"] or f"fb-{r['day']:%y%m%d}-{r['id']}")
    clean = web_admin.clean_post("article", body)
    aid = web_admin.save_post(cur, clean, by)
    if r.get("image"):                  # 粉專的配圖也當專欄封面（同一個檔），來源照記
        from api import review_art, stock_art
        meta, media = _json(r.get("image_meta")), Path(config.WEB["media_dir"])
        if meta.get("src") == "stock":
            stock_art.set_cover(cur, media, aid, r["image"], meta.get("credit") or {}, by, str(meta.get("at") or ""))
        else:
            review_art.set_cover(cur, media, aid, r["image"], str(meta.get("prompt") or ""),
                                 str(meta.get("scene") or ""), by, str(meta.get("model") or ""), str(meta.get("at") or ""))
    return aid, clean["slug"]


# ── 資料庫 ───────────────────────────────────────────────────

def used_keys(cur, day: date) -> Set[str]:
    cur.execute("SELECT DISTINCT topic_key FROM fb_post_drafts WHERE day >= %s AND topic_key IS NOT NULL",
                (day - timedelta(days=USED_DAYS),))
    return {r["topic_key"] for r in cur.fetchall()}


def recent_sources(cur, day: date) -> Dict[str, int]:
    """最近 NEWS_SOURCE_DAYS 天的時事草稿，各家媒體用了幾次（pick_news 換別家用）。"""
    cur.execute("SELECT source_name, COUNT(*) AS n FROM fb_post_drafts WHERE category='news' AND day >= %s "
                "AND day <= %s AND source_name IS NOT NULL GROUP BY source_name",
                (day - timedelta(days=NEWS_SOURCE_DAYS), day))
    return {r["source_name"]: int(r["n"]) for r in cur.fetchall()}


def store_info(cur) -> Dict[str, Any]:
    try:
        cur.execute("SELECT v FROM y1web.web_settings WHERE k='store'")
        r = cur.fetchone()
        v = json.loads(r["v"]) if r and isinstance(r["v"], (str, bytes)) else (r["v"] if r else {})
        return v if isinstance(v, dict) else {}
    except (pymysql.MySQLError, ValueError):
        return {}


def site_url(cur) -> str:
    from api import web_admin
    return web_admin.get_site_url(cur, config.WEB["site_url"])


def web_items(cur, site: str) -> List[Dict[str, Any]]:
    """還沒分享過粉專的官網文章（聽力專欄、使用小教室），新的在前。"""
    try:
        cur.execute("SELECT w.id, w.kind, w.slug, w.title, w.summary, LEFT(w.body_md, 300) AS excerpt "
                    "FROM y1web.web_posts w WHERE w.kind IN ('article','care') AND w.status='published' "
                    "AND (w.publish_at IS NULL OR w.publish_at <= NOW()) "
                    "AND NOT EXISTS (SELECT 1 FROM fb_posts p WHERE p.web_post_id=w.id AND p.status='sent') "
                    "ORDER BY COALESCE(w.publish_at, w.updated_at) DESC, w.id DESC")
        rows = cur.fetchall()
    except pymysql.MySQLError:
        return []
    out = []
    for r in rows:
        url = fb_page.post_url(site, r["kind"], r["slug"])
        if url:
            out.append({**r, "url": url, "excerpt": _plain(r["excerpt"])[:200]})
    return out


def insert(cur, day: date, spec: Dict[str, Any], res: Dict[str, Any], by: Optional[str]) -> int:
    """by＝None 是每日排程寫的：預設勾選（到時間沒取消就自動發）；管理者自己按「多寫一則」的不預設勾。"""
    cur.execute("INSERT INTO fb_post_drafts (day, category, topic_key, topic, brief, link, web_post_id, "
                "source_name, source_url, message, seo, web_sync, picked, meta, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (day, spec["category"], spec["topic_key"], spec["topic"], spec["brief"][:2000],
                 spec.get("link"), spec.get("web_post_id"), spec.get("source_name"), spec.get("source_url"),
                 res["message"], json.dumps(res["seo"], ensure_ascii=False), int(res["web_sync"]),
                 int(by is None), res["meta"][:255], by))
    return cur.lastrowid


_run_lock = threading.Lock()


def run_daily(day: Optional[date] = None, n: Optional[int] = None) -> int:
    """寫當天的草稿（每日排程）。當天已經寫過就不重寫。回寫了幾則。"""
    day = day or date.today()
    n = int(n or _cfg("draft_count", 3))
    with _run_lock:
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM fb_post_drafts WHERE day=%s AND created_by IS NULL", (day,))
            if cur.fetchone()["c"]:
                return 0
            used, recent = used_keys(cur, day), recent_sources(cur, day)
            store, site = store_info(cur), site_url(cur)
            webs = web_items(cur, site)
        news = pick_news(fetch_news(), used, recent=recent) if _cfg("news", True) else []
        specs = plan(day, n, used, news, webs, site)
        ids: List[int] = []
        for spec in specs:
            try:
                res = write(spec, store, site)
            except Exception as e:  # noqa: BLE001 — 一則失敗不影響其他
                log.warning("粉專草稿寫失敗（%s %s）：%s", spec["category"], spec["topic_key"], e)
                continue
            with _conn() as db, db.cursor() as cur:
                ids.append(insert(cur, day, spec, res, None))
    log.info("粉專草稿：%s 寫了 %d／%d 則（新聞候選 %d、官網文章 %d）", day, len(ids), len(specs), len(news), len(webs))
    from api import post_art            # 每則從圖庫配一張真實照片；配不到照樣能發
    try:
        post_art.illustrate(ids, [], "每日自動")
    except Exception:  # noqa: BLE001
        log.exception("粉專草稿配圖失敗")
    return len(ids)


OWN_AS = ("care", "health", "family", "ask", "season")


def write_one(cat: str, brief: str, by: str, link: Optional[str] = None) -> int:
    """管理者按「多寫一則」：有自己的題目就照題目寫，沒有就從那個類別挑。"""
    day = date.today()
    with _conn() as db, db.cursor() as cur:
        used, store, site = used_keys(cur, day), store_info(cur), site_url(cur)
        webs = web_items(cur, site) if cat == "web" else []
        recent = recent_sources(cur, day) if cat == "news" else {}
    if brief.strip():
        spec = own_spec(brief, link)
        if cat in OWN_AS:
            spec["category"] = cat          # 自己出題但指定類別：套那個類別的寫法
    else:
        news = pick_news(fetch_news(), used, recent=recent) if cat == "news" else []
        spec = single_spec(cat, day, used, news, webs, site)
        if spec is None:
            raise LookupError({"news": "現在沒有找到合適的新聞（和長輩、聽力、健康相關，而且最近三天的）。",
                               "season": "最近三天沒有節日。",
                               "web": "官網的專欄和小教室文章都已經分享過，或最近已經寫過。"}.get(cat, "這個類別沒有題目了。"))
    res = write(spec, store, site)
    with _conn() as db, db.cursor() as cur:
        did = insert(cur, day, spec, res, by)
    from api import post_art
    try:
        post_art.illustrate([did], [], by)  # 從圖庫配一張真實照片（不用 GPU，馬上好）
    except Exception:  # noqa: BLE001
        log.exception("草稿 #%s 配圖失敗", did)
    return did


def regenerate(did: int) -> None:
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT * FROM fb_post_drafts WHERE id=%s", (did,))
        r = cur.fetchone()
        if r is None:
            raise LookupError(did)
        if r["status"] != "new":
            raise ValueError("這則已經處理過了。")
        store, site = store_info(cur), site_url(cur)
    res = write({k: r[k] for k in ("category", "topic", "brief", "link", "source_name")}, store, site)
    with _conn() as db, db.cursor() as cur:       # 同步勾選照管理者原本的選擇
        cur.execute("UPDATE fb_post_drafts SET message=%s, seo=%s, meta=%s, error=NULL WHERE id=%s AND status='new'",
                    (res["message"], json.dumps(res["seo"], ensure_ascii=False), res["meta"][:255], did))


_pub_lock = threading.Lock()
PHOTO_LINK_LABEL = {"news": "📰 新聞原文：", "web": "👉 全文："}


def photo_message(message: str, link: Optional[str], category: str = "") -> str:
    """相片貼文不會有連結預覽：附的連結改寫進文字裡（官網宣傳那段之前）；文字裡已經有就不重複。"""
    if not link or link in message:
        return message
    line = PHOTO_LINK_LABEL.get(category, "🔗 ") + link
    i = message.find(FOOTER_HEAD)
    if i < 0:
        return message.rstrip() + "\n\n" + line
    return message[:i].rstrip() + "\n\n" + line + "\n\n" + message[i:]


def photo_jpeg(name: Optional[str]) -> Optional[bytes]:
    """草稿的配圖 → 給 Facebook 的 JPEG。沒有圖、檔案不見了回 None（照舊發文字或連結貼文）。"""
    from api import web_admin
    path = web_admin.image_path(Path(config.WEB["media_dir"]), name or "")
    if not path or not path.is_file():
        return None
    buf = io.BytesIO()
    with Image.open(path) as im:
        im.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def publish(ids: Sequence[int], force: bool, by: str) -> Dict[str, Any]:
    """發佈勾選的草稿。有錯誤全部不發；有提醒且沒 force 也全部不發，回給管理者確認。"""
    ids = list(dict.fromkeys(int(i) for i in ids))
    if not ids:
        raise ValueError("請先勾選要發佈的貼文。")
    with _pub_lock, _conn() as db, db.cursor() as cur:
        cur.execute(f"SELECT * FROM fb_post_drafts WHERE id IN ({','.join(['%s'] * len(ids))}) "
                    "AND status='new' ORDER BY id", ids)
        rows = cur.fetchall()
        if len(rows) != len(ids):
            raise LookupError("有幾則已經發佈或刪掉了，請重新整理頁面。")
        errors, warns = {}, {}
        for r in rows:
            e, w = check(r["message"])
            if r["web_sync"]:
                e += column_errors(r)
            if e:
                errors[r["id"]] = e
            if w:
                warns[r["id"]] = w
        if errors:
            return {"ok": False, "errors": errors}
        if warns and not force:
            return {"ok": False, "warnings": warns}
        results = []
        for r in rows:
            try:
                from api import post_art
                post_art.ensure_stock(cur, r)       # 以前 AI 畫的換成真實照片
                if r.get("image") and not r.get("card"):
                    r["card"] = post_art.make_card(cur, r["id"])
                photo = photo_jpeg(r.get("card") or r.get("image"))      # 圖卡（照片＋大字標題），沒有就原照片
                fid = (fb_page.publish_photo(photo_message(r["message"], r["link"], r["category"]), photo) if photo
                       else fb_page.publish_post(r["message"], r["link"]))
            except fb_page.GraphError as e:
                cur.execute("UPDATE fb_post_drafts SET error=%s WHERE id=%s", (str(e)[:255], r["id"]))
                results.append({"id": r["id"], "status": "failed", "error": str(e)})
                continue
            status = "published" if fid else "dev"
            cur.execute("UPDATE fb_post_drafts SET status=%s, fb_post_id=%s, error=NULL, published_by=%s, "
                        "published_at=NOW() WHERE id=%s", (status, fid, by, r["id"]))
            res = {"id": r["id"], "status": status, "fb_post_id": fid}
            if r["web_sync"]:
                try:
                    aid, slug = sync_column(cur, r, by)
                    cur.execute("UPDATE fb_post_drafts SET web_article_id=%s WHERE id=%s", (aid, r["id"]))
                    res["web_slug"] = slug
                except Exception as e:  # noqa: BLE001 — 粉專已經發了，專欄沒成功就記下來讓管理者到官網管理補
                    log.exception("草稿 #%s 同步聽力專欄失敗", r["id"])
                    msg = f"粉專已發，但同步聽力專欄失敗：{e}"
                    cur.execute("UPDATE fb_post_drafts SET error=%s WHERE id=%s", (msg[:255], r["id"]))
                    res["web_error"] = msg
            if r["web_post_id"]:      # 官網管理的「分享到粉專」紀錄也記一筆
                cur.execute("INSERT INTO fb_posts (web_post_id, fb_post_id, message, link, status, created_by) "
                            "VALUES (%s,%s,%s,%s,%s,%s)",
                            (r["web_post_id"], fid, r["message"], r["link"], "sent" if fid else "dev", by))
            results.append(res)
    log.info("粉專貼文由 %s 發佈：%s", by, [(x["id"], x["status"]) for x in results])
    return {"ok": True, "results": results}


# ── 自動發佈（到期限沒取消勾選＝同意）──────────────────────────
AUTO_BY = "自動發佈"
AUTO_REVIEW_MIN = 60        # 草稿至少要在期限前這麼多分鐘寫好，才自動發（留時間給門市看）
AUTO_LAST_HOUR = 21         # 主機停機錯過期限：當天這個鐘點前補發，再晚就不發了


def auto_deadline(day: date) -> Optional[datetime]:
    """config [social] auto_publish＝HH:MM；留空或格式不對＝不自動發。"""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(_cfg("auto_publish", "") or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return datetime.combine(day, datetime.min.time()).replace(hour=int(m.group(1)), minute=int(m.group(2)))


def auto_status(r: Dict[str, Any], now: Optional[datetime] = None, run: bool = False) -> Tuple[bool, str]:
    """這則會不會自動發（純函式）：回 (會不會, 原因)。畫面上的標示和 auto_publish()（run=True）用同一套。"""
    now = now or datetime.now()
    dl = auto_deadline(r["day"])
    if dl is None or r["created_by"] is not None or r["status"] != "new" or r["day"] != now.date():
        return False, ""
    if not run and now > dl + timedelta(minutes=5):        # 期限過了（排程 1 分鐘內就跑完）還在的
        return False, f"已過 {dl:%H:%M}，沒有自動發；要發請手動" if r["picked"] else ""
    if not r["picked"]:
        return False, "已取消勾選，不會自動發"
    if r["created_at"] > dl - timedelta(minutes=AUTO_REVIEW_MIN):
        return False, "太晚才寫好，不自動發，要發請手動"
    e, w = check(r["message"])
    if r["web_sync"]:
        e += column_errors(r)
    if e or w:
        return False, "有提醒，不會自動發，請看過再手動發"
    return True, f"{dl:%H:%M} 自動發"


def auto_publish(now: Optional[datetime] = None) -> Dict[str, Any]:
    """期限到了：今天的每日草稿、勾著、沒有錯誤和提醒的，全部發出（含同步聽力專欄）。"""
    now = now or datetime.now()
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT * FROM fb_post_drafts WHERE day=%s AND status='new' AND created_by IS NULL", (now.date(),))
        rows = cur.fetchall()
    go = [r["id"] for r in rows if auto_status(r, now, run=True)[0]]
    held = [(r["id"], auto_status(r, now, run=True)[1]) for r in rows if r["picked"] and r["id"] not in go]
    out = publish(go, False, AUTO_BY) if go else {"ok": True, "results": []}
    log.info("粉專自動發佈：發 %s；沒發 %s", [(x["id"], x["status"]) for x in out.get("results", [])], held)
    return out


# ── 每日排程 ─────────────────────────────────────────────────
_sched_started = False


def start_scheduler() -> None:
    """背景執行緒：每 60 秒看一次。
    - 到 draft_hour 後寫當天的草稿（同一天只寫一次，失敗最多再試兩次）；
    - 到 auto_publish 的時間，自動發還勾著的（同一天只跑一次；主機停機錯過的話當天 21 點前補）。"""
    global _sched_started
    if _sched_started or not _cfg("draft_enabled", True):
        return
    _sched_started = True
    hour = int(_cfg("draft_hour", 6))

    def _loop() -> None:
        tries: Dict[date, int] = {}
        last_try = 0.0
        auto_done: Optional[date] = None
        while True:
            try:
                now = datetime.now()
                d = now.date()
                if now.hour >= hour and tries.get(d, 0) < 3 and time.time() - last_try > 900:
                    with _conn() as db, db.cursor() as cur:
                        cur.execute("SELECT 1 FROM fb_post_drafts WHERE day=%s AND created_by IS NULL LIMIT 1", (d,))
                        done = cur.fetchone() is not None
                    if not done:
                        tries[d] = tries.get(d, 0) + 1
                        last_try = time.time()
                        run_daily(d)
                dl = auto_deadline(d)
                if dl and auto_done != d and dl <= now and now.hour < AUTO_LAST_HOUR:
                    auto_done = d
                    auto_publish(now)
            except Exception:  # noqa: BLE001 — 排程不可拖垮服務
                log.exception("粉專草稿排程執行失敗")
            time.sleep(60)

    threading.Thread(target=_loop, daemon=True, name="fb-drafts").start()
    dl = auto_deadline(date.today())
    log.info("粉專草稿排程已啟動（每天 %02d:00 後寫 %s 則；%s）", hour, _cfg("draft_count", 3),
             f"{dl:%H:%M} 自動發還勾著的" if dl else "不自動發")
