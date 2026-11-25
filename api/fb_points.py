"""
fb_points.py — 粉專集點：客人在粉專貼文留言就累積點數。

John 2026-11-19：「先做後台記錄和會員頁的記錄，只算留言分……每次累積 1 分。」
（Facebook 不給「誰按讚、誰分享」的名單，2026-11-19 實測 77 則有心情的貼文全部回 0 筆，所以只能依留言計分。）

規則（改規則只改這裡＋docs）：
- 來源只有 webhook 的留言事件（fb_page.handle 呼叫 record_comment）；粉專自己的留言不算。
- 同一人在同一則貼文只算一次（POINTS_PER_POST 點）；再留言記成 repeat（0 點），資料庫用 counted_key 唯一鍵保證。
- 客人刪了留言 → removed；他在同一則貼文還有別的留言，就改算那一則。
- 店員可以作廢（void，要寫原因）或恢復。

會員怎麼對上留言者：
- 官網會員頁產生 6 位數綁定碼（y1member.fb_link_codes，只存 SHA-256，30 分鐘、一次），
  客人用 Messenger 傳給粉專 → try_bind() 寫 y1member.member_fb（只存粉專編號的 SHA-256 與 FB 名字）。
- 留言者編號＝Messenger 的 PSID（2026-11-19 資料庫比對同一人兩邊相同）。官網透過 crm1.v_member_fb_points 讀，也只有雜湊。
- 綁定前的留言照樣記分；綁定後會員頁自動看得到。

資料表：crm1.fb_comment_points（scripts/schema_fb.py）；y1member.member_fb、fb_link_codes、
crm1.v_member_fb_points 與官網權限在 y1web scripts/setup_db.py。
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import pymysql
import pymysql.cursors

import config
from api import dbpool

log = logging.getLogger("y1crm.fb_points")

POINTS_PER_POST = 1
STATUS_LABEL = {"counted": "計分", "repeat": "同一則貼文已計分", "removed": "留言已刪除", "void": "作廢"}
CODE_FAIL_MAX = 5                 # 同一個 Messenger 帳號一小時內打錯幾次就先不收
CODE_FAIL_WINDOW = 3600

# 「綁定碼 123456」「FB123456」「綁定：１２３４５６」＝明確要綁定；只傳 6 個數字也接受，但對不到就當一般訊息
_CODE_RE = re.compile(r"(?:綁定碼?|FB)\s*(?:是|為)?\s*[:：]?\s*(\d{6})(?!\d)", re.I)
_BARE_RE = re.compile(r"^\s*(\d{6})\s*$")

_fails: Dict[str, List[float]] = {}
_fails_lock = threading.Lock()


def _conn():
    return dbpool.connect(**config.db_params(),
                          cursorclass=pymysql.cursors.DictCursor, autocommit=True)


# ── 純函式（有單元測試）────────────────────────────────────

def person_hash(person_id: str) -> str:
    """粉專編號（PSID／留言者編號）→ SHA-256。官網只看得到這個。"""
    return hashlib.sha256(str(person_id).encode()).hexdigest()


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def find_code(text: str) -> Tuple[Optional[str], bool]:
    """回 (綁定碼, 是否明確寫了「綁定／FB」)。全形數字也認。"""
    t = unicodedata.normalize("NFKC", text or "")
    m = _CODE_RE.search(t)
    if m:
        return m.group(1), True
    m = _BARE_RE.match(t)
    return (m.group(1), False) if m else (None, False)


def points_of(row: Dict[str, Any]) -> int:
    """這一筆實際算幾點：只有「計分」的才算。"""
    return int(row.get("points") or 0) if row.get("status") == "counted" else 0


def bound_reply(display_name: str, total: int) -> str:
    who = f"{display_name} 您好，" if display_name else ""
    return (f"{who}Facebook 和官網會員綁定好了 ✅\n"
            f"之後您在粉專參加活動累積的點數，都會記在會員裡，到官網「會員」頁就看得到（目前 {total} 點）。")


FAIL_REPLY = "找不到這個綁定碼，或已經超過 30 分鐘。請到官網「會員」頁重新取得綁定碼，再傳一次。"
LOCK_REPLY = "綁定碼輸入錯誤太多次，請一小時後再試，或直接來電門市。"


# ── 記分（webhook 呼叫；每句自動提交，靠唯一鍵防重複）──────────────

def record_comment(comment_id: str, post_id: Optional[str], person_id: Optional[str],
                   person_name: Optional[str]) -> Optional[str]:
    """一則新留言 → 記一筆。回狀態（counted／repeat）；重送、粉專自己的留言、資料不全回 None。"""
    if not (comment_id and post_id and person_id):
        return None
    name = (person_name or None) and person_name[:100]
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT status FROM fb_comment_points WHERE comment_id=%s", (comment_id,))
        if cur.fetchone():
            return None                                   # webhook 重送
        try:
            cur.execute("INSERT INTO fb_comment_points (comment_id, post_id, person_id, person_name, points, status) "
                        "VALUES (%s,%s,%s,%s,%s,'counted')",
                        (comment_id, post_id, person_id, name, POINTS_PER_POST))
            status = "counted"
        except pymysql.err.IntegrityError:
            # 同一人同一則貼文已經算過（counted_key 唯一）；或剛好同一則留言重送搶先寫入（IGNORE 掉）
            cur.execute("INSERT IGNORE INTO fb_comment_points (comment_id, post_id, person_id, person_name, points, status) "
                        "VALUES (%s,%s,%s,%s,0,'repeat')", (comment_id, post_id, person_id, name))
            if cur.rowcount == 0:
                return None
            status = "repeat"
        if name:   # 名字以最新的為準（客人改名）
            cur.execute("UPDATE fb_comment_points SET person_name=%s WHERE person_id=%s AND "
                        "(person_name IS NULL OR person_name<>%s)", (name, person_id, name))
    log.info("粉專集點：留言 %s… → %s", comment_id[:20], status)
    return status


def _promote(cur, person_id: str, post_id: str) -> None:
    """同一則貼文的計分那筆沒了，就改算他在同一則貼文的下一則留言。"""
    cur.execute("SELECT id FROM fb_comment_points WHERE person_id=%s AND post_id=%s AND status='repeat' "
                "ORDER BY id LIMIT 1", (person_id, post_id))
    r = cur.fetchone()
    if r:
        try:
            cur.execute("UPDATE fb_comment_points SET status='counted', points=%s WHERE id=%s AND status='repeat'",
                        (POINTS_PER_POST, r["id"]))
        except pymysql.err.IntegrityError:
            pass                                          # 同時有別筆先補上了


def remove_comment(comment_id: str) -> None:
    """客人刪了留言：這筆不算。"""
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT person_id, post_id, status FROM fb_comment_points WHERE comment_id=%s", (comment_id,))
        r = cur.fetchone()
        if not r or r["status"] not in ("counted", "repeat"):
            return
        cur.execute("UPDATE fb_comment_points SET status='removed' WHERE comment_id=%s", (comment_id,))
        if r["status"] == "counted":
            _promote(cur, r["person_id"], r["post_id"])


def void(rid: int, staff: str, reason: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("請寫作廢原因。")
    with _conn() as db, db.cursor() as cur:
        cur.execute("UPDATE fb_comment_points SET status='void', void_by=%s, void_at=NOW(), void_reason=%s "
                    "WHERE id=%s AND status='counted'", (staff, reason[:100], rid))
        if cur.rowcount != 1:
            raise LookupError("這筆已經不是計分狀態（可能已作廢或留言已刪除），請重新整理。")
    log.info("粉專集點：%s 作廢 #%s（%s）", staff, rid, reason[:30])


def restore(rid: int, staff: str) -> str:
    """作廢的恢復成計分；同一則貼文那人已經有別筆在計分，就恢復成「同一則貼文已計分」。"""
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT status FROM fb_comment_points WHERE id=%s", (rid,))
        r = cur.fetchone()
        if not r or r["status"] != "void":
            raise LookupError("這筆不是作廢狀態，請重新整理。")
        try:
            cur.execute("UPDATE fb_comment_points SET status='counted', points=%s, void_by=NULL, void_at=NULL, "
                        "void_reason=NULL WHERE id=%s AND status='void'", (POINTS_PER_POST, rid))
            status = "counted"
        except pymysql.err.IntegrityError:
            cur.execute("UPDATE fb_comment_points SET status='repeat', points=0, void_by=NULL, void_at=NULL, "
                        "void_reason=NULL WHERE id=%s AND status='void'", (rid,))
            status = "repeat"
    log.info("粉專集點：%s 恢復 #%s → %s", staff, rid, status)
    return status


def total_for(cur, person_id: str) -> int:
    cur.execute("SELECT COALESCE(SUM(points), 0) n FROM fb_comment_points WHERE person_id=%s AND status='counted'",
                (person_id,))
    return int(cur.fetchone()["n"])


# ── 會員綁定（Messenger 傳綁定碼）──────────────────────────────

def bind_code(psid: str, code: str, fb_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """綁定碼有效就把這個 Messenger 帳號綁到那位會員。回 {member_id, display_name, total}；無效回 None。"""
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT c.id, c.member_id, m.display_name FROM y1member.fb_link_codes c "
                    "JOIN y1member.members m ON m.id=c.member_id AND m.disabled=0 "
                    "WHERE c.code_hash=%s AND c.used_at IS NULL AND c.expires_at > NOW()", (code_hash(code),))
        c = cur.fetchone()
        if not c:
            return None
        cur.execute("UPDATE y1member.fb_link_codes SET used_at=NOW() WHERE id=%s AND used_at IS NULL", (c["id"],))
        if cur.rowcount != 1:
            return None
        # 一個 Facebook 帳號只綁一位會員：綁過別人的就改綁到這位（拿得到綁定碼＝本人登入了會員頁）
        cur.execute("INSERT INTO y1member.member_fb (member_id, person_hash, fb_name, linked_at) "
                    "VALUES (%s,%s,%s,NOW()) ON DUPLICATE KEY UPDATE member_id=VALUES(member_id), "
                    "fb_name=COALESCE(VALUES(fb_name), fb_name), linked_at=NOW()",
                    (c["member_id"], person_hash(psid), (fb_name or None) and fb_name[:100]))
        total = total_for(cur, psid)
    log.info("粉專集點：Messenger 綁定會員 #%s", c["member_id"])
    return {"member_id": c["member_id"], "display_name": c["display_name"], "total": total}


def try_bind(psid: str, text: str, fb_name: Optional[str] = None) -> Optional[str]:
    """Messenger 收到文字：是綁定碼就處理，回要回覆客人的話；不是綁定碼回 None（照一般訊息處理）。"""
    code, explicit = find_code(text)
    if not code:
        return None
    now = time.time()
    with _fails_lock:
        fails = [t for t in _fails.get(psid, []) if now - t < CODE_FAIL_WINDOW]
        _fails[psid] = fails
    if len(fails) >= CODE_FAIL_MAX:
        return LOCK_REPLY if explicit else None
    r = bind_code(psid, code, fb_name)
    if r is None:
        if not explicit:
            return None                  # 只是剛好 6 個數字（電話末碼、型號…），當一般訊息
        with _fails_lock:
            _fails.setdefault(psid, []).append(now)
        return FAIL_REPLY
    return bound_reply(r["display_name"], r["total"])


# ── 後台查詢（/fb-points）──────────────────────────────────

_POST_TITLE = ("COALESCE((SELECT w.title FROM fb_posts fp JOIN y1web.web_posts w ON w.id=fp.web_post_id "
               "  WHERE fp.fb_post_id=p.post_id ORDER BY fp.id DESC LIMIT 1), "
               " (SELECT d.topic FROM fb_post_drafts d WHERE d.fb_post_id=p.post_id LIMIT 1))")


def _members_by_hash(cur, hashes: List[str]) -> Dict[str, Dict[str, Any]]:
    """粉專編號雜湊 → 綁定的會員（稱呼、連結的門市客人）。"""
    if not hashes:
        return {}
    cur.execute("SELECT f.person_hash, f.fb_name, f.linked_at, m.id AS member_id, m.display_name, "
                "       m.customer_id, c.name AS customer_name "
                "FROM y1member.member_fb f JOIN y1member.members m ON m.id=f.member_id "
                "LEFT JOIN customers c ON c.id=m.customer_id WHERE f.person_hash IN %s", (tuple(hashes),))
    return {r["person_hash"]: r for r in cur.fetchall()}


def people(cur) -> List[Dict[str, Any]]:
    """每位留言者一列：累積點數、計分留言數、最近留言、綁定的會員。"""
    cur.execute("SELECT person_id, MAX(person_name) AS person_name, "
                "       COALESCE(SUM(CASE WHEN status='counted' THEN points END), 0) AS points, "
                "       SUM(status='counted') AS counted, COUNT(*) AS comments, MAX(created_at) AS last_at "
                "FROM fb_comment_points GROUP BY person_id ORDER BY points DESC, last_at DESC")
    rows = list(cur.fetchall())
    mem = _members_by_hash(cur, [person_hash(r["person_id"]) for r in rows])
    for r in rows:
        r["points"], r["counted"], r["comments"] = int(r["points"]), int(r["counted"] or 0), int(r["comments"])
        r["member"] = mem.get(person_hash(r["person_id"]))
    return rows


def records(cur, person_id: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
    where, args = ("WHERE p.person_id=%s", [person_id]) if person_id else ("", [])
    cur.execute(f"SELECT p.id, p.person_id, p.person_name, p.post_id, p.points, p.status, p.void_by, p.void_at, "
                f"       p.void_reason, p.created_at, {_POST_TITLE} AS post_title, "
                f"       (SELECT LEFT(m.text, 80) FROM fb_messages m WHERE m.ext_mid=p.comment_id) AS excerpt "
                f"FROM fb_comment_points p {where} ORDER BY p.id DESC LIMIT %s", (*args, int(limit)))
    rows = list(cur.fetchall())
    for r in rows:
        r["points"] = points_of(r)
        r["status_label"] = STATUS_LABEL.get(r["status"], r["status"])
    return rows


def backfill() -> Dict[str, int]:
    """把上線前 webhook 已經收進 fb_messages 的客人第一層留言補記分（可重跑，記過的會略過）。
    回覆別人留言的那種存在同一串、沒有各自的留言者編號，補不了。"""
    out = {"seen": 0, "counted": 0, "repeat": 0, "removed": 0}
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT m.ext_mid, m.text, t.post_id, t.person_id, t.person_name FROM fb_messages m "
                    "JOIN fb_threads t ON t.id=m.thread_id WHERE t.kind='comment' AND m.source='customer' "
                    "AND m.ext_mid=t.ext_id AND t.person_id IS NOT NULL ORDER BY m.id")
        rows = list(cur.fetchall())
    for r in rows:
        out["seen"] += 1
        st = record_comment(r["ext_mid"], r["post_id"], r["person_id"], r["person_name"])
        if st:
            out[st] += 1
            if r["text"] == "[留言已刪除]":
                remove_comment(r["ext_mid"])
                out["removed"] += 1
    return out
