"""
member_points.py — 後台「會員集點」：所有點數來源合起來看，店員加點、扣點（John 2026-11-19）。

| 來源 | 規則 | 資料在哪 |
| 粉專活動 | 同一人同一則貼文留言 1 點 | crm1.fb_comment_points（api/fb_points.py），綁定在 y1member.member_fb |
| 每日聽練 | 一天練完 1 回 1 點 | y1member.member_points source=practice（官網 app/points.py 記） |
| 聽練連續 7 天 | 連續滿 7、14、21…天各 +10，斷掉從頭算（John 11-19 晚） | source=streak（官網記，note＝「連續 N 天」） |
| 線上聽力測驗 | 會員保存結果，每個月第一次 10 點 | y1member.member_points source=hearing_online（官網記） |
| 來店打卡 | 客人在 Facebook 粉專打卡給店員看，店員在這裡一鍵加點（預設 10 點；同月第二次要再確認） | source=hearing_store（沿用，adjust() 記） |
| 兌換贈品 | 店員在這裡扣點、寫原因；點數不夠不能扣 | source=redeem，點數是負的 |
| 點數調整 | 其他加點／扣點，要寫原因 | source=adjust，正負都有 |

John 11-19：「門市聽檢讓店員在點數管理中增加，並註明增加原因。…活動兌換時，也讓店員在點數管理中減少，
並註明減少原因。會員登入後，也要能查到每次增加或減少的明細。」
- 加點、扣點：有 customers 選單的人都可以（店員）；原因會員在官網看得到，登記的店員名字會員看不到。
- 作廢／恢復：經理以上（粉專活動用 fb_points.void／restore，其他用這裡的 void／restore）。
  恢復一筆扣點、新扣點都要點數夠；作廢加點可能讓餘額變負的（照實顯示，經理自己判斷）。
- 官網會員自己存的線上聽力測驗「結果」門市看不到，這裡只看得到點數。
"""
from __future__ import annotations

import logging
import secrets
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pymysql
import pymysql.cursors

import config
from api import dbpool, fb_points

log = logging.getLogger("y1crm.member_points")

# 11-20 John：門市聽檢不再給點（門市說登記很累）→ 改「來店打卡」：客人 Facebook 粉專打卡給店員看，店員一鍵加點，每月第一次 10 點。
# 帳本來源沿用 hearing_store（不改 ENUM），只改顯示名稱；11-19 那一筆門市聽檢的原因欄照舊寫著「門市聽力檢查」。
STORE_POINTS = 10        # 來店打卡預設幾點（店員可以改）
MAX_ADJUST = 1000        # 一次最多加減幾點（打錯多一個 0 擋得住）
NOTE_MAX = 100           # 原因字數（y1member.member_points.note）
SOURCES = ("practice", "streak", "hearing_online", "hearing_store", "fb", "redeem", "adjust")
SOURCE_LABEL = {"practice": "每日聽練", "streak": "聽練連續 7 天", "hearing_online": "線上聽力測驗",
                "hearing_store": "來店打卡", "fb": "粉專活動", "redeem": "兌換贈品", "adjust": "點數調整"}
STAFF_SOURCES = ("hearing_store", "redeem", "adjust")
# 店員登記的種類：畫面上的選項 → 帳本來源、正負、預設點數
STAFF_KINDS = {
    "hearing_store": {"source": "hearing_store", "sign": 1, "label": "來店打卡（加點）", "default": STORE_POINTS},
    "redeem": {"source": "redeem", "sign": -1, "label": "兌換贈品（扣點）", "default": None},
    "add": {"source": "adjust", "sign": 1, "label": "其他加點", "default": None},
    "sub": {"source": "adjust", "sign": -1, "label": "其他扣點", "default": None},
}
LEDGER_STATUS = {"counted": "計分", "void": "作廢"}


class NeedConfirm(Exception):
    """要店員再確認一次才記（同一個月已經有來店打卡的點數）。"""


def _conn():
    return dbpool.connect(**config.db_params(), cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def _at(v: Any) -> str:
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return str(v or "")


# ── 依會員 ─────────────────────────────────────────────────

_FB_SUM = ("SELECT COALESCE(SUM(p.points), 0) FROM fb_comment_points p JOIN y1member.member_fb f "
           "ON f.person_hash = SHA2(p.person_id, 256) WHERE f.member_id = {m} AND p.status = 'counted'")


def totals(by: Dict[str, int]) -> Dict[str, int]:
    """各來源點數 → 目前點數、兌換／調整合計（給畫面用）。"""
    return {"total": sum(by.get(s, 0) for s in SOURCES),
            "other": by.get("redeem", 0) + by.get("adjust", 0)}


def members(cur) -> List[Dict[str, Any]]:
    """每位官網會員一列：各來源點數、目前點數（扣掉兌換的）。phone 只給搜尋用。"""
    cur.execute(
        "SELECT m.id, m.display_name, m.customer_id, c.name AS customer_name, c.phone_mobile AS phone, "
        f" ({_FB_SUM.format(m='m.id')}) AS fb, "
        " (SELECT COUNT(*) FROM y1member.member_fb f WHERE f.member_id = m.id) AS fb_links "
        "FROM y1member.members m LEFT JOIN customers c ON c.id = m.customer_id WHERE m.disabled = 0")
    found = list(cur.fetchall())
    cur.execute("SELECT member_id, source, SUM(points) AS n FROM y1member.member_points "
                "WHERE status = 'counted' GROUP BY member_id, source")
    ledger: Dict[int, Dict[str, int]] = {}
    for r in cur.fetchall():
        ledger.setdefault(r["member_id"], {})[r["source"]] = int(r["n"])
    rows = []
    for r in found:
        d = {k: r[k] for k in ("id", "display_name", "customer_id", "customer_name", "phone")}
        by = {s: 0 for s in SOURCES}
        by.update(ledger.get(r["id"], {}))
        by["fb"] = int(r["fb"])
        d.update(by)
        d.update(totals(by))
        d["fb_linked"] = bool(r["fb_links"])
        rows.append(d)
    rows.sort(key=lambda d: (-d["total"], d["display_name"] or ""))
    return rows


def balance(cur, member_id: int) -> int:
    """目前點數：帳本（加點減扣點）＋粉專留言。"""
    cur.execute("SELECT COALESCE(SUM(points), 0) AS n FROM y1member.member_points "
                "WHERE member_id = %s AND status = 'counted'", (member_id,))
    n = int(cur.fetchone()["n"])
    cur.execute(f"SELECT ({_FB_SUM.format(m='%s')}) AS n", (member_id,))
    return n + int(cur.fetchone()["n"])


def _row(kind: str, rid: Any, at: Any, source: str, who: str, detail: str, points: int, status: str,
         status_label: str, **extra: Any) -> Dict[str, Any]:
    return {"kind": kind, "id": rid, "at": _at(at), "source": source, "source_label": SOURCE_LABEL[source],
            "who": who, "detail": detail, "points": points, "status": status, "status_label": status_label, **extra}


def _ledger_rows(cur, member_id: Optional[int] = None, limit: int = 300) -> List[Dict[str, Any]]:
    where, args = ("WHERE x.member_id = %s", [member_id]) if member_id else ("", [])
    cur.execute(f"SELECT x.id, x.member_id, x.source, x.ref, x.points, x.note, x.created_by, x.status, "
                f"       x.void_by, x.void_reason, x.created_at, m.display_name, "
                f"       JSON_EXTRACT(pg.data, CONCAT('$.days.\"', x.ref, '\".n')) AS rounds "
                f"FROM y1member.member_points x JOIN y1member.members m ON m.id = x.member_id "
                f"LEFT JOIN y1member.member_progress pg ON pg.member_id = x.member_id AND pg.app = 'practice' "
                f"  AND x.source = 'practice' "
                f"{where} ORDER BY x.created_at DESC, x.id DESC LIMIT %s", (*args, int(limit)))
    out = []
    for r in cur.fetchall():
        if r["source"] == "practice":
            at, detail = r["ref"], (f"練完 {int(r['rounds'])} 回" if r["rounds"] else "")
        elif r["source"] == "hearing_online":
            at, detail = r["created_at"], f"{r['ref'][:4]} 年 {int(r['ref'][5:7])} 月（會員保存結果）"
        elif r["source"] == "streak":
            at, detail = r["ref"], r["note"] or ""
        else:                                   # 店員登記：原因（會員看得到）
            at, detail = r["created_at"], r["note"] or ""
        pts = int(r["points"])
        out.append(_row("ledger", r["id"], at, r["source"], r["display_name"] or "會員", detail,
                        pts if r["status"] == "counted" else 0, r["status"],
                        LEDGER_STATUS.get(r["status"], r["status"]), orig=pts, by=r["created_by"] or "",
                        member_id=r["member_id"], void_by=r["void_by"], void_reason=r["void_reason"]))
    return out


def _fb_rows(cur, member_id: Optional[int] = None, limit: int = 300) -> List[Dict[str, Any]]:
    """粉專留言：有綁會員的顯示會員稱呼，沒綁的顯示 Facebook 名字。"""
    if member_id:
        cur.execute("SELECT p.person_id FROM fb_comment_points p JOIN y1member.member_fb f "
                    "ON f.person_hash = SHA2(p.person_id, 256) WHERE f.member_id = %s GROUP BY p.person_id",
                    (member_id,))
        rows = []
        for r in cur.fetchall():
            rows += fb_points.records(cur, r["person_id"], limit)
    else:
        rows = fb_points.records(cur, None, limit)
    mem = fb_points._members_by_hash(cur, list({fb_points.person_hash(r["person_id"]) for r in rows}))
    out = []
    for r in rows:
        m = mem.get(fb_points.person_hash(r["person_id"]))
        who = m["display_name"] if m else f"{r['person_name'] or '（沒有名字）'}（Facebook，未綁定）"
        detail = (r["post_title"] or "粉專貼文") + (f"：{r['excerpt']}" if r["excerpt"] else "")
        out.append(_row("fb", r["id"], r["created_at"], "fb", who, detail, r["points"], r["status"],
                        r["status_label"], orig=1, by="", member_id=m["member_id"] if m else None,
                        void_by=r["void_by"], void_reason=r["void_reason"]))
    return out


def member_records(cur, member_id: int) -> List[Dict[str, Any]]:
    rows = _ledger_rows(cur, member_id, 1000) + _fb_rows(cur, member_id, 1000)
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows


def all_records(cur, limit: int = 300) -> List[Dict[str, Any]]:
    rows = _ledger_rows(cur, None, limit) + _fb_rows(cur, None, limit)
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:limit]


# ── 店員加點、扣點 ─────────────────────────────────────────

class _MemberLock:
    """同一位會員的加減點一次一個（扣點前要算餘額）。用 GET_LOCK，不用表格鎖，也不必開交易。"""

    def __init__(self, cur, member_id: int):
        self.cur, self.name = cur, f"y1member.points.{int(member_id)}"

    def __enter__(self):
        self.cur.execute("SELECT GET_LOCK(%s, 5) AS ok", (self.name,))
        if (self.cur.fetchone() or {}).get("ok") != 1:
            raise RuntimeError("這位會員的點數正在被別人修改，請稍後再試。")
        return self

    def __exit__(self, *exc):
        self.cur.execute("SELECT RELEASE_LOCK(%s)", (self.name,))
        self.cur.fetchall()
        return False


def _clean_points(v: Any) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise ValueError("點數要填數字。")
    if not 1 <= n <= MAX_ADJUST:
        raise ValueError(f"點數要在 1～{MAX_ADJUST} 之間。")
    return n


def store_this_month(cur, member_id: int, now: Optional[datetime] = None) -> Optional[datetime]:
    """這個月（月曆月）已經登記過來店打卡（11-19 以前是門市聽檢）點數的話，回第一筆的時間。"""
    now = now or datetime.now()
    first = datetime(now.year, now.month, 1)
    cur.execute("SELECT MIN(created_at) AS t FROM y1member.member_points WHERE member_id = %s "
                "AND source = 'hearing_store' AND status = 'counted' AND created_at >= %s", (member_id, first))
    r = cur.fetchone()
    return r["t"] if r else None


def adjust(member_id: int, kind: str, points: Any, reason: str, staff: str, confirm: bool = False) -> Dict[str, Any]:
    """店員登記一筆加點／扣點。回 {id, points（帶正負）, balance（登記後的目前點數）}。
    ValueError：輸入不對、點數不夠；LookupError：沒有這位會員；NeedConfirm：同月第二次來店打卡。"""
    k = STAFF_KINDS.get(kind)
    if not k:
        raise ValueError("請選要加點還是扣點。")
    n = _clean_points(points)
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("請寫原因（會員在官網看得到）。")
    if len(reason) > NOTE_MAX:
        raise ValueError(f"原因最多 {NOTE_MAX} 個字。")
    pts = n * k["sign"]
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT id FROM y1member.members WHERE id = %s AND disabled = 0", (member_id,))
        if not cur.fetchone():
            raise LookupError("找不到這位官網會員（可能已經停用或合併），請重新整理。")
        with _MemberLock(cur, member_id):
            if k["source"] == "hearing_store" and not confirm:
                t = store_this_month(cur, member_id)
                if t:
                    raise NeedConfirm(f"這位會員這個月 {t.month}/{t.day} 已經加過來店打卡的點數了。確定還要再加 {n} 點嗎？")
            bal = balance(cur, member_id)
            if pts < 0 and bal + pts < 0:
                raise ValueError(f"點數不夠：目前 {bal} 點，不能扣 {n} 點。")
            cur.execute("INSERT INTO y1member.member_points (member_id, source, ref, points, note, created_by) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (member_id, k["source"], "s" + secrets.token_hex(8), pts, reason, (staff or "")[:45]))
            rid = cur.lastrowid
    log.info("會員集點：%s 幫會員 #%s %s %+d（%s）", staff, member_id, k["label"], pts, reason[:30])
    return {"id": rid, "points": pts, "balance": bal + pts}


# ── 作廢／恢復（帳本裡的：聽練、線上測驗、店員登記的）──────────────

def void(rid: int, staff: str, reason: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("請寫作廢原因。")
    with _conn() as db, db.cursor() as cur:
        cur.execute("UPDATE y1member.member_points SET status='void', void_by=%s, void_at=NOW(), void_reason=%s "
                    "WHERE id=%s AND status='counted'", (staff, reason[:100], rid))
        if cur.rowcount != 1:
            raise LookupError("這筆已經作廢了，請重新整理。")
    log.info("會員集點：%s 作廢 #%s（%s）", staff, rid, reason[:30])


def restore(rid: int, staff: str) -> str:
    """恢復作廢的一筆。扣點那筆要恢復，點數要夠扣。"""
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT member_id, points FROM y1member.member_points WHERE id=%s AND status='void'", (rid,))
        row = cur.fetchone()
        if not row:
            raise LookupError("這筆不是作廢狀態，請重新整理。")
        with _MemberLock(cur, row["member_id"]):
            pts = int(row["points"])
            if pts < 0:
                bal = balance(cur, row["member_id"])
                if bal + pts < 0:
                    raise ValueError(f"點數不夠：目前 {bal} 點，恢復這筆要扣 {-pts} 點。")
            cur.execute("UPDATE y1member.member_points SET status='counted', void_by=NULL, void_at=NULL, "
                        "void_reason=NULL WHERE id=%s AND status='void'", (rid,))
            if cur.rowcount != 1:
                raise LookupError("這筆不是作廢狀態，請重新整理。")
    log.info("會員集點：%s 恢復 #%s", staff, rid)
    return "counted"
