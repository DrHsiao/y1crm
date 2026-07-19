"""
line_bot.py — LINE Messaging API 整合（獨立模組，不混入 app.py）

範圍：webhook 接收（簽章驗證）、訊息發送（reply/push）、客戶綁定。
- 綁定流程：客戶加好友 → 輸入建檔手機號碼 → 比對 customers.phone_mobile →
  寫入 customers.line_user_id（唯一索引，一個 LINE 帳號只能綁一位客戶）。
- 開發模式：config.line.channel_access_token 未設時，所有發送只記 log 不打 LINE API，
  webhook 邏輯（驗簽/綁定/資料庫）照常運作，可在區網完整測試。
- 上線前置：webhook URL 需公網 HTTPS（Cloudflare Tunnel 只放行本路徑）。

安全：
- 每個請求都驗 X-Line-Signature（HMAC-SHA256, channel secret），驗不過一律 401。
- 本端點不走 session 登入（LINE 平台呼叫），身分靠簽章。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import urllib.request
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pymysql
import pymysql.cursors
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.templating import Jinja2Templates

import config

log = logging.getLogger("y1crm.line")
router = APIRouter()

_API = "https://api.line.me/v2/bot"
_TPL = Jinja2Templates(directory=os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui", "templates"))


def _conn():
    return pymysql.connect(**config.db_params(),
                           cursorclass=pymysql.cursors.DictCursor, autocommit=True)


# ── 簽章與發送 ───────────────────────────────────────────────

def _verify_signature(body: bytes, signature: str) -> bool:
    secret = config.LINE.get("channel_secret") or ""
    if not secret or not signature:
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature)


def _call_api(path: str, payload: Dict[str, Any]) -> bool:
    """呼叫 LINE API；未設 access token＝開發模式，只記 log。"""
    token = config.LINE.get("channel_access_token") or ""
    if not token:
        log.info("[LINE 開發模式，未真發] %s %s", path,
                 json.dumps(payload, ensure_ascii=False))
        return False
    req = urllib.request.Request(
        _API + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:  # noqa: BLE001 — 發送失敗不可拖垮 webhook 回應
        log.warning("LINE API 失敗 %s: %s", path, e)
        return False


def reply(reply_token: str, texts: List[str]) -> bool:
    """回覆訊息（免費、不佔推播額度）。最多 5 則。"""
    if not reply_token:
        return False
    return _call_api("/message/reply", {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": t} for t in texts[:5]]})


def push(line_user_id: str, texts: List[str]) -> bool:
    """主動推播（預約提醒用；佔每月推播額度）。"""
    return _call_api("/message/push", {
        "to": line_user_id,
        "messages": [{"type": "text", "text": t} for t in texts[:5]]})


# ── 客戶綁定 ─────────────────────────────────────────────────

_PHONE_RE = re.compile(r"^09\d{8}$")

WELCOME = ("歡迎加入！\n"
           "點下方選單即可線上預約、查詢常見問題。\n"
           "門市人員稍後會為您完成帳號綁定，即可接收預約與保養提醒；\n"
           "或您也可以直接輸入建檔時的手機號碼（例：0912345678）自助完成綁定。")

# ── 圖文選單各按鈕的回覆內容（v1 文案；實際地址/電話/活動由 John 提供後更新）──
FAQ_TEXT = ("❓ 常見問題\n"
            "① 助聽器沒聲音／變小聲？\n"
            "→ 先更換新電池，並檢查出聲孔有無耳垢；仍無改善請帶回門市檢查。\n"
            "② 電池一顆可以用多久？\n"
            "→ 依型號約 5–10 天，建議隨身攜帶備用電池。\n"
            "③ 助聽器可以戴著洗澡、游泳嗎？\n"
            "→ 不行，請先取下；受潮請盡快擦乾並送門市除濕保養。\n"
            "④ 保固與保養？\n"
            "→ 依購買機型保固卡為準，門市提供基本清潔保養服務。\n"
            "\n其他問題歡迎直接留言，門市人員會盡快回覆您 😊")
STORE_INFO = ("🏪 睿聲助聽器-中正門市\n"
              "營業時間：週一至週六 09:00–18:00\n"
              "📍 地址：台北市中正區羅斯福路一段105號\n"
              "☎️ 電話：02-2341-5968")
EVENTS_TEXT = "📢 目前沒有進行中的活動，新優惠與活動將在這裡公告，敬請期待！"
AGENT_TEXT = ("💬 請直接在此輸入您的問題，"
              "門市人員於營業時間（週一至週六 09:00–18:00）會盡快回覆您。")


def _get_profile(uid: str) -> Dict[str, Any]:
    """取 LINE 個人檔案（暱稱/頭像）；失敗或開發模式回空 dict。"""
    token = config.LINE.get("channel_access_token") or ""
    if not token or not uid:
        return {}
    req = urllib.request.Request(f"{_API}/profile/{uid}",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return {}


def _upsert_follower(uid: str) -> None:
    """把互動過的 LINE 帳號記進 line_followers（店員配對名單的來源）。"""
    if not uid:
        return
    p = _get_profile(uid)
    with _conn() as db, db.cursor() as cur:
        cur.execute(
            "INSERT INTO line_followers (line_user_id, display_name, picture_url) "
            "VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE "
            "  display_name=COALESCE(VALUES(display_name), display_name), "
            "  picture_url=COALESCE(VALUES(picture_url), picture_url), "
            "  last_seen_at=NOW()",
            (uid, p.get("displayName"), p.get("pictureUrl")))


def _norm_phone(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _handle_text(ev: Dict[str, Any]) -> None:
    uid = (ev.get("source") or {}).get("userId")
    text = ((ev.get("message") or {}).get("text") or "").strip()
    rt = ev.get("replyToken") or ""
    if not uid:
        return
    if "預約" in text:          # 綁定與否都可預約（表單本就要填稱呼/電話）
        _send_booking_link(uid, rt)
        return
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT id, name FROM customers WHERE line_user_id=%s", (uid,))
        bound = cur.fetchone()
        if bound:
            return   # 已綁定者自由留言不自動回（留給門市人員在官方帳號後台回覆）
        _upsert_follower(uid)   # 未綁定者任何互動都進店員配對名單
        ph = _norm_phone(text)
        if not _PHONE_RE.match(ph):
            reply(rt, [WELCOME])
            return
        # 比對建檔手機（容忍資料庫裡有 - 或空白的舊格式）
        cur.execute("SELECT id, name, line_user_id FROM customers "
                    "WHERE REPLACE(REPLACE(phone_mobile,'-',''),' ','')=%s", (ph,))
        rows = cur.fetchall()
        if len(rows) == 0:
            reply(rt, ["找不到這個手機號碼的建檔資料，"
                       "請確認號碼是否正確，或聯絡門市人員協助綁定。"])
            return
        if len(rows) > 1:   # 同號多筆（如家人共用）：不自動綁，避免綁錯人
            reply(rt, ["這個手機號碼有多筆建檔資料，"
                       "為避免綁定錯誤，請聯絡門市人員協助綁定。"])
            return
        row = rows[0]
        if row["line_user_id"] and row["line_user_id"] != uid:
            reply(rt, ["這筆客戶資料已綁定其他 LINE 帳號，請聯絡門市人員處理。"])
            return
        cur.execute("UPDATE customers SET line_user_id=%s WHERE id=%s",
                    (uid, row["id"]))
        log.info("LINE 綁定成功 customer_id=%s（自助輸入手機）", row["id"])
        reply(rt, [f"綁定成功！{row['name']} 您好，"
                   "之後的預約通知與提醒都會透過這裡傳送給您。"])


# ── 線上預約（輕量版：結構化留言單，不進行事曆）──────────────
#   流程：LINE 輸入「預約」→ 回一次性連結（綁 userId，30 分鐘）→
#   表單選 稱呼/電話/日期(近2週)/時間(09–18 整點)/需求 → 存 booking_requests
#   → 推播確認給客戶；店員在 CRM「預約留言」頁收單。
_BOOKING_TTL = 1800                                  # 連結有效 30 分鐘
_HOURS = list(range(9, 19))                          # 09:00–18:00 整點

_WD = ["一", "二", "三", "四", "五", "六", "日"]


def _booking_dates() -> List[Dict[str, str]]:
    """明天起 14 天（民國顯示、ISO 值）。"""
    out = []
    for i in range(1, 15):
        d = date.today() + timedelta(days=i)
        out.append({"v": d.isoformat(),
                    "label": f"{d.year - 1911}/{d.month:02d}/{d.day:02d}（{_WD[d.weekday()]}）"})
    return out


def _make_booking_token(uid: str) -> str:
    """一次性預約連結 token（存 DB：服務重啟不失效、跨行程一致）。"""
    tok = secrets.token_urlsafe(16)
    with _conn() as db, db.cursor() as cur:
        cur.execute("DELETE FROM booking_tokens WHERE expires_at < NOW()")   # 順手清過期
        cur.execute("INSERT INTO booking_tokens (token, line_user_id, expires_at) "
                    "VALUES (%s,%s,DATE_ADD(NOW(), INTERVAL %s SECOND))",
                    (tok, uid, _BOOKING_TTL))
    return tok


def _token_uid(tok: str, consume: bool = False) -> Optional[str]:
    if not tok:
        return None
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT line_user_id FROM booking_tokens "
                    "WHERE token=%s AND expires_at > NOW()", (tok,))
        row = cur.fetchone()
        if row and consume:
            cur.execute("DELETE FROM booking_tokens WHERE token=%s", (tok,))  # 單次使用
    return row["line_user_id"] if row else None


def _send_booking_link(uid: str, rt: str) -> None:
    base = (config.LINE.get("public_base_url") or "").rstrip("/")
    url = f"{base}/lp/booking?t={_make_booking_token(uid)}"
    reply(rt, ["請點下方連結填寫預約資訊（連結 30 分鐘內有效）：\n" + url])


@router.get("/lp/booking")
def lp_booking(request: Request, t: str = ""):
    uid = _token_uid(t)
    if uid is None:
        return _TPL.TemplateResponse(request, "lp_booking.html",
                                     {"expired": True, "token": "", "dates": [], "hours": []},
                                     status_code=410)
    # 已綁定客戶帶入稱呼/電話（仍可修改）
    name = phone = ""
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT name, phone_mobile FROM customers WHERE line_user_id=%s", (uid,))
        row = cur.fetchone()
        if row:
            name, phone = row["name"] or "", row["phone_mobile"] or ""
    return _TPL.TemplateResponse(request, "lp_booking.html", {
        "expired": False, "token": t, "dates": _booking_dates(), "hours": _HOURS,
        "name": name, "phone": phone,
    }, headers={"Cache-Control": "no-store"})


@router.post("/lp/booking/submit")
def lp_booking_submit(body: Dict[str, Any] = Body(...)):
    uid = _token_uid(str(body.get("token") or ""), consume=True)
    if uid is None:
        raise HTTPException(410, "連結已逾期，請回 LINE 重新輸入「預約」取得新連結。")
    name = (body.get("contact_name") or "").strip()[:50]
    phone = re.sub(r"[^\d\-]", "", str(body.get("phone") or ""))[:20]
    note = (body.get("note") or "").strip()[:500] or None
    req_date = str(body.get("req_date") or "")
    if not name:
        raise HTTPException(422, "請填寫稱呼。")
    if len(re.sub(r"\D", "", phone)) < 7:
        raise HTTPException(422, "請填寫有效的聯絡電話。")
    if req_date not in {d["v"] for d in _booking_dates()}:
        raise HTTPException(422, "日期不在可預約範圍（明天起兩週內）。")
    try:
        req_hour = int(body.get("req_hour"))
    except (TypeError, ValueError):
        raise HTTPException(422, "請選擇時段。")
    if req_hour not in _HOURS:
        raise HTTPException(422, "時段不在可預約範圍（09–18 時，整點）。")

    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE line_user_id=%s", (uid,))
        row = cur.fetchone()
        cur.execute(
            "INSERT INTO booking_requests "
            "(line_user_id, customer_id, contact_name, phone, req_date, req_hour, note) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (uid, row["id"] if row else None, name, phone, req_date, req_hour, note))
        bid = cur.lastrowid
    log.info("預約留言 #%s %s %s %s %02d:00", bid, name, phone, req_date, req_hour)
    d = date.fromisoformat(req_date)
    push(uid, [f"已收到您的預約需求 ✅\n"
               f"{name} 您好，您預約的時間是："
               f"{d.year - 1911}/{d.month:02d}/{d.day:02d}（{_WD[d.weekday()]}）{req_hour:02d}:00\n"
               + (f"需求：{note}\n" if note else "")
               + "門市人員將盡快與您電話確認，謝謝！"])
    return {"ok": True}


# ── 圖文選單 postback（六顆按鈕；data 形如 action=booking）────
def _my_bookings_text(uid: str) -> str:
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT req_date, req_hour, note, status FROM booking_requests "
                    "WHERE line_user_id=%s AND req_date >= CURDATE() "
                    "AND status <> 'cancelled' "
                    "ORDER BY req_date, req_hour LIMIT 5", (uid,))
        rows = cur.fetchall()
    if not rows:
        return "您目前沒有預約。點選單「我要預約」即可線上預約 😊"
    st = {"new": "（待門市確認）", "handled": "（已確認）"}
    lines = ["📅 您的預約："]
    for r in rows:
        d = r["req_date"]
        lines.append(f"‧{d.year - 1911}/{d.month:02d}/{d.day:02d}（{_WD[d.weekday()]}）"
                     f"{r['req_hour']:02d}:00{st.get(r['status'], '')}"
                     + (f"—{r['note']}" if r["note"] else ""))
    lines.append("\n如需更改或取消，請直接留言告知門市人員。")
    return "\n".join(lines)


def _handle_postback(ev: Dict[str, Any]) -> None:
    uid = (ev.get("source") or {}).get("userId")
    rt = ev.get("replyToken") or ""
    data = (ev.get("postback") or {}).get("data") or ""
    act = dict(p.split("=", 1) for p in data.split("&") if "=" in p).get("action")
    if not uid:
        return
    log.info("圖文選單點擊 action=%s uid=%s…", act, uid[:12])
    with _conn() as db, db.cursor() as cur:   # 未綁定者互動也要進配對名單
        cur.execute("SELECT id FROM customers WHERE line_user_id=%s", (uid,))
        if cur.fetchone() is None:
            _upsert_follower(uid)
    if act == "booking":
        _send_booking_link(uid, rt)
    elif act == "mybookings":
        reply(rt, [_my_bookings_text(uid)])
    elif act == "faq":
        reply(rt, [FAQ_TEXT])
    elif act == "events":
        reply(rt, [EVENTS_TEXT])
    elif act == "info":
        reply(rt, [STORE_INFO])
    elif act == "agent":
        reply(rt, [AGENT_TEXT])


# ── Webhook ──────────────────────────────────────────────────

def _handle_event(ev: Dict[str, Any]) -> None:
    t = ev.get("type")
    uid = (ev.get("source") or {}).get("userId")
    if t == "follow":                       # 加好友 → 記入配對名單＋歡迎詞
        _upsert_follower(uid)
        reply(ev.get("replyToken") or "", [WELCOME])
    elif t == "message":
        if (ev.get("message") or {}).get("type") == "text":
            _handle_text(ev)
        else:                               # 貼圖/圖片等：未綁定者也要進配對名單
            with _conn() as db, db.cursor() as cur:
                cur.execute("SELECT id FROM customers WHERE line_user_id=%s", (uid,))
                if cur.fetchone() is None:
                    _upsert_follower(uid)
                    reply(ev.get("replyToken") or "", [WELCOME])
    elif t == "postback":
        _handle_postback(ev)   # 圖文選單六顆按鈕
    elif t == "unfollow":                   # 封鎖：保留綁定（重加好友即恢復），只記 log
        log.info("LINE unfollow: %s", uid)


@router.post("/api/line/webhook")
async def line_webhook(request: Request):
    body = await request.body()
    if not _verify_signature(body, request.headers.get("X-Line-Signature", "")):
        raise HTTPException(401, "signature 驗證失敗")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "非法 JSON")
    events = payload.get("events", [])
    # 進站記錄（LINE Console 的 Verify 是 0 個事件的合法請求，也要看得見）
    log.info("LINE webhook 進站：%d 個事件 %s", len(events),
             [e.get("type") for e in events])
    for ev in payload.get("events", []):
        try:
            _handle_event(ev)
        except Exception:  # noqa: BLE001 — 單一事件失敗不影響其他事件與 200 回應
            log.exception("處理 LINE 事件失敗: %s", ev.get("type"))
    return {"ok": True}
