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
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pymysql
import pymysql.cursors
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.templating import Jinja2Templates

import config
from api import ai_chat, dbpool

log = logging.getLogger("y1crm.line")
router = APIRouter()

_API = "https://api.line.me/v2/bot"
_TPL = Jinja2Templates(directory=os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui", "templates"))


def _conn():
    return dbpool.connect(**config.db_params(),   # 連線池（2026-09-26）
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


def push_messages(line_user_id: str, messages: List[Dict[str, Any]]) -> bool:
    """主動推播任意訊息物件（樣板訊息等；佔每月推播額度）。"""
    if not line_user_id or not messages:
        return False
    return _call_api("/message/push", {"to": line_user_id,
                                       "messages": messages[:5]})


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


HINT = ("您好 😊 門市人員會盡快查看您的訊息並回覆。\n"
        "需要預約請點下方選單「我要預約」，或直接輸入「預約」。")


def _norm_phone(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _maybe_hint(uid: str, rt: str) -> None:
    """未綁定者的自動回覆：只回一次提示，之後靜音。

    每句都回歡迎詞會洗版擾人；訊息仍照常進 line_followers 配對名單，
    店員在 CRM 看得到、也隨時能人工回覆。綁定後本函式不再被呼叫。
    """
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT hint_sent_at FROM line_followers WHERE line_user_id=%s", (uid,))
        row = cur.fetchone()
        if row and row["hint_sent_at"]:
            return                      # 已提示過 → 靜音，交給店員
        cur.execute("UPDATE line_followers SET hint_sent_at=NOW() WHERE line_user_id=%s", (uid,))
    reply(rt, [HINT])


# ── 客服 AI（本機推論）與人工接手狀態機 ──────────────────────
#   mode=bot  ：AI 可回（或產草稿）
#   mode=human：人工接手中，AI 一律不出聲，避免與店員在官方帳號後台打架。
#   逾時復原＝human_until（config.ai.human_hold_hours，預設 4 小時）到點自動回 bot；
#   店員也可在 CRM 手動交還。因為 LINE 不會把店員在官方 App 的回覆送進 webhook，
#   我們無法自動察覺人工已接手，逾時是唯一的自動出口。

def _conv_mode(uid: str) -> str:
    """取得對話模式，順便處理逾時復原。"""
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT mode, human_until FROM line_conversations WHERE line_user_id=%s",
                    (uid,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT IGNORE INTO line_conversations (line_user_id) VALUES (%s)", (uid,))
            return "bot"
        if row["mode"] == "human" and row["human_until"] and row["human_until"] <= datetime.now():
            cur.execute("UPDATE line_conversations SET mode='bot', human_until=NULL, "
                        "assigned_to=NULL, handoff_reason=NULL WHERE line_user_id=%s", (uid,))
            log.info("人工接手逾時，對話交還 AI uid=%s…", uid[:12])
            return "bot"
        return row["mode"]


def set_human(uid: str, reason: str, staff: Optional[str] = None) -> None:
    """切到人工接手（AI 靜音），並設定自動復原時間。"""
    hours = float(config.AI.get("human_hold_hours") or 4)
    with _conn() as db, db.cursor() as cur:
        cur.execute(
            "INSERT INTO line_conversations (line_user_id, mode, human_until, assigned_to, handoff_reason) "
            "VALUES (%s,'human',DATE_ADD(NOW(), INTERVAL %s MINUTE),%s,%s) "
            "ON DUPLICATE KEY UPDATE mode='human', "
            "  human_until=DATE_ADD(NOW(), INTERVAL %s MINUTE), assigned_to=%s, handoff_reason=%s",
            (uid, int(hours * 60), staff, reason, int(hours * 60), staff, reason))
    log.info("轉人工 uid=%s… 原因=%s 由=%s", uid[:12], reason, staff or "系統")


def set_bot(uid: str) -> None:
    """交還 AI。"""
    with _conn() as db, db.cursor() as cur:
        cur.execute("UPDATE line_conversations SET mode='bot', human_until=NULL, "
                    "assigned_to=NULL, handoff_reason=NULL WHERE line_user_id=%s", (uid,))


def log_msg(uid: str, direction: str, source: str, text: str,
            cid: Optional[int] = None, status: str = "new",
            meta: Optional[str] = None) -> int:
    with _conn() as db, db.cursor() as cur:
        cur.execute("INSERT IGNORE INTO line_conversations (line_user_id) VALUES (%s)", (uid,))
        cur.execute("INSERT INTO line_messages "
                    "(line_user_id, customer_id, direction, source, text, status, meta) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (uid, cid, direction, source, text[:5000], status,
                     (meta or None) and meta[:255]))   # meta 欄位 varchar(255)
        mid = cur.lastrowid      # ⚠ 必須在下一句 UPDATE 之前取，否則會拿到 0
        cur.execute("UPDATE line_conversations SET last_msg_at=NOW() WHERE line_user_id=%s", (uid,))
        return mid


def _history(uid: str) -> List[Dict[str, str]]:
    """取最近對話餵給模型（只取客戶說的與實際送出的回覆，不含未送出的草稿）。"""
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT direction, source, text FROM line_messages "
                    "WHERE line_user_id=%s AND (source='customer' OR status='sent') "
                    "ORDER BY id DESC LIMIT 6", (uid,))
        rows = list(reversed(cur.fetchall()))
    return [{"role": "user" if r["direction"] == "in" else "assistant", "content": r["text"]}
            for r in rows]


def _ai_worker(uid: str, text: str, rt: str, cid: Optional[int]) -> None:
    """背景執行：webhook 已回 200，這裡慢慢算再 reply（reply token 約 1 分鐘內有效）。"""
    try:
        res = ai_chat.answer(text, _history(uid))
    except Exception:  # noqa: BLE001
        log.exception("AI 產生回覆失敗")
        return
    auto = bool(config.AI.get("auto_reply"))
    meta = f"{res['kind']}/{res.get('reason') or '-'} {res.get('elapsed')}s"
    if res["kind"] in ("handoff", "timeout"):
        set_human(uid, res.get("reason") or "ai")     # 需要人接手的，AI 先閉嘴
    if auto:
        ok = reply(rt, [res["text"]])
        log_msg(uid, "out", "ai", res["text"], cid,
                status="sent" if ok else "discarded", meta=meta)
    else:
        # 第一階段：不直接回客戶，存成草稿讓店員在 CRM 審核後使用
        log_msg(uid, "out", "ai_draft", res["text"], cid, status="new", meta=meta)
    log.info("AI %s uid=%s… %s", "已回覆" if auto else "草稿已產生", uid[:12], meta)


_MEDIA_API = "https://api-data.line.me/v2/bot"
_MEDIA_MAX = 10 * 1024 * 1024          # 單檔上限 10MB，超過只留紀錄不存檔


def _fetch_content(mid: str) -> Optional[Tuple[bytes, str]]:
    """下載客戶傳來的訊息內容。

    ⚠ LINE 只保留一段時間，必須在收到 webhook 的當下抓，不能等店員想看才抓。
    """
    token = config.LINE.get("channel_access_token") or ""
    if not token or not mid:
        return None
    req = urllib.request.Request(f"{_MEDIA_API}/message/{mid}/content",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read(_MEDIA_MAX + 1)
            mime = r.headers.get("Content-Type", "application/octet-stream").split(";")[0]
    except Exception as e:  # noqa: BLE001
        log.warning("下載 LINE 內容失敗 mid=%s: %s", mid, e)
        return None
    if len(data) > _MEDIA_MAX:
        log.warning("LINE 內容超過 %dMB，不存檔 mid=%s", _MEDIA_MAX // 1024 // 1024, mid)
        return None
    return data, mime


def _media_worker(uid: str, msg_id: str, row_id: int, cid: Optional[int], rt: str) -> None:
    """背景：下載圖片 → 存磁碟 → 交給好卡的視覺模型產草稿。"""
    got = _fetch_content(msg_id)
    if not got:
        return
    data, mime = got
    rel = config.save_file("line_media", data, mime, uid)
    with _conn() as db, db.cursor() as cur:
        cur.execute("UPDATE line_messages SET rel_path=%s, mime=%s WHERE id=%s",
                    (rel, mime, row_id))
    log.info("已存客戶圖片 uid=%s… %s", uid[:12], rel)

    if not ai_chat.enabled() or _conv_mode(uid) == "human":
        return
    res = ai_chat.describe_image(str(config.abs_path(rel)), mime)
    auto = bool(config.AI.get("auto_reply"))
    meta = f"{res['kind']}/{res.get('reason') or '-'} {res.get('elapsed')}s"
    if res.get("desc"):
        meta = f"{meta}｜照片：{res['desc']}"   # 照片描述只留在這，不會傳給客戶
    if res["kind"] in ("handoff", "timeout"):
        set_human(uid, res.get("reason") or "vision")
    if auto:
        ok = reply(rt, [res["text"]])
        log_msg(uid, "out", "ai", res["text"], cid,
                status="sent" if ok else "discarded", meta=meta)
    else:
        log_msg(uid, "out", "ai_draft", res["text"], cid, status="new", meta=meta)
    log.info("圖片%s uid=%s… %s", "已回覆" if auto else "草稿已產生", uid[:12], meta)


def _ai_dispatch(uid: str, text: str, rt: str, cid: Optional[int]) -> bool:
    """決定要不要讓 AI 出手。回 True 表示 AI 已接手這則訊息。"""
    if not ai_chat.enabled():
        return False
    if _conv_mode(uid) == "human":
        log.info("人工接手中，AI 不回應 uid=%s…", uid[:12])
        return False
    threading.Thread(target=_ai_worker, args=(uid, text, rt, cid), daemon=True).start()
    return bool(config.AI.get("auto_reply"))   # 只有自動回覆模式才算「已回應客戶」


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
            # 已綁定者：訊息入庫供 CRM 檢視，交給 AI（自動回覆或產草稿）。
            # AI 關閉／人工接手中＝不自動回，留給門市人員在官方帳號後台回覆。
            log_msg(uid, "in", "customer", text, bound["id"])
            _ai_dispatch(uid, text, rt, bound["id"])
            return
        _upsert_follower(uid)   # 未綁定者任何互動都進店員配對名單
        ph = _norm_phone(text)
        if not _PHONE_RE.match(ph):
            log_msg(uid, "in", "customer", text)
            if not _ai_dispatch(uid, text, rt, None):
                _maybe_hint(uid, rt)    # AI 沒回＝仍給一次性提示，之後靜音
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


# ── 預約提醒（前一天推播＋客戶確認按鈕）─────────────────────
#   排程：每日 config.line.reminder_hour 點掃 reminder_days_ahead 天後的預約，
#   對已綁 LINE 且未取消、且尚未提醒過（reminded_at IS NULL）者推播一則樣板訊息，
#   附「✅ 確認前往」一顆 postback 按鈕（改期/取消不做按鈕，請客戶留言由店員處理）。
#   客戶按下 → booking_requests.confirm_status/confirmed_at 記錄，店員頁即時看得到。
#   佔推播額度（1 則/筆），故 reminded_at 為冪等鎖：同一筆永遠只提醒一次。

def _slot_label(d: date, hour: int) -> str:
    return (f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
            f"（{_WD[d.weekday()]}）{hour:02d}:00")


def _reminder_message(b: Dict[str, Any]) -> Dict[str, Any]:
    """組提醒用的 buttons 樣板訊息（無 title 時 text 上限 160 字）。"""
    slot = _slot_label(b["req_date"], b["req_hour"])
    name = b.get("contact_name") or ""
    text = (f"【預約提醒】{name} 您好\n"
            f"您在 睿聲助聽器-中正門市 的預約時間為\n{slot}\n"
            "確定前往請按下方按鈕；如需改期或取消，請直接留言告知門市人員。")[:160]
    return {
        "type": "template",
        "altText": f"【預約提醒】{slot} 睿聲助聽器-中正門市",
        "template": {"type": "buttons", "text": text, "actions": [
            {"type": "postback", "label": "✅ 確認前往",
             "data": f"action=bkok&id={b['id']}", "displayText": "確認前往"},
        ]},
    }


def send_booking_reminder(bid: int, force: bool = False) -> Dict[str, Any]:
    """對單筆預約送出提醒。回 {ok, msg}；已提醒過且非 force 一律不重送。"""
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT id, line_user_id, contact_name, req_date, req_hour, "
                    "       status, reminded_at "
                    "FROM booking_requests WHERE id=%s", (bid,))
        b = cur.fetchone()
        if b is None:
            return {"ok": False, "msg": "找不到這筆預約留言。"}
        if not b["line_user_id"]:
            return {"ok": False, "msg": "這筆預約沒有 LINE 來源，無法推播提醒。"}
        if b["status"] == "cancelled":
            return {"ok": False, "msg": "這筆預約已取消，不送提醒。"}
        if b["reminded_at"] and not force:
            return {"ok": False, "msg": "這筆預約已送過提醒（避免重複佔用推播額度）。"}
        if not push_messages(b["line_user_id"], [_reminder_message(b)]):
            return {"ok": False, "msg": "LINE 推播失敗（或目前為開發模式未真發）。"}
        cur.execute("UPDATE booking_requests SET reminded_at=NOW() WHERE id=%s", (bid,))
    log.info("預約提醒已推播 #%s %s %s", bid, b["contact_name"],
             _slot_label(b["req_date"], b["req_hour"]))
    return {"ok": True, "msg": "提醒已送出。"}


def run_booking_reminders(target: Optional[date] = None) -> Dict[str, Any]:
    """掃指定日期（預設 reminder_days_ahead 天後）的待提醒預約並逐筆推播。"""
    if target is None:
        days = int(config.LINE.get("reminder_days_ahead") or 1)
        target = date.today() + timedelta(days=days)
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT id FROM booking_requests "
                    "WHERE req_date=%s AND status <> 'cancelled' "
                    "AND line_user_id IS NOT NULL AND reminded_at IS NULL "
                    "ORDER BY req_hour, id", (target,))
        ids = [r["id"] for r in cur.fetchall()]
    sent = sum(1 for i in ids if send_booking_reminder(i)["ok"])
    log.info("預約提醒排程：目標日 %s，待提醒 %d 筆，成功 %d 筆",
             target, len(ids), sent)
    return {"date": target.isoformat(), "pending": len(ids), "sent": sent}


_sched_started = False


def start_reminder_scheduler() -> None:
    """背景執行緒：每 60 秒檢查，到 reminder_hour 整點該日跑一次（比照 OCR 看門狗）。"""
    global _sched_started
    if _sched_started or not config.LINE.get("reminder_enabled", True):
        return
    _sched_started = True
    hour = int(config.LINE.get("reminder_hour") or 18)

    def _loop() -> None:
        last: Optional[date] = None      # 已跑過的日期（同日不重跑）
        while True:
            try:
                now = datetime.now()
                if now.hour >= hour and last != now.date():
                    last = now.date()
                    run_booking_reminders()
            except Exception:  # noqa: BLE001 — 排程不可拖垮服務
                log.exception("預約提醒排程執行失敗")
            time.sleep(60)

    threading.Thread(target=_loop, daemon=True, name="booking-reminder").start()
    log.info("預約提醒排程已啟動（每日 %02d:00 掃 %s 天後的預約）",
             hour, config.LINE.get("reminder_days_ahead") or 1)


def _handle_booking_reply(uid: str, rt: str, bid: str) -> None:
    """客戶按下提醒訊息的「確認前往」：只認自己的預約，寫入 confirm_status。
    改期／取消不做按鈕（客戶留言由門市人員在官方帳號後台處理）。"""
    try:
        bid_i = int(bid)
    except (TypeError, ValueError):
        return
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT id, contact_name, req_date, req_hour, status "
                    "FROM booking_requests WHERE id=%s AND line_user_id=%s",
                    (bid_i, uid))
        b = cur.fetchone()
        if b is None:      # 非本人的預約（或已刪）：不透露任何資訊
            reply(rt, ["查無這筆預約，請直接留言由門市人員為您處理。"])
            return
        if b["status"] == "cancelled":
            reply(rt, ["這筆預約已取消，如需重新預約請點選單「我要預約」。"])
            return
        cur.execute("UPDATE booking_requests SET confirm_status='confirmed', "
                    "confirmed_at=NOW() WHERE id=%s", (bid_i,))
    slot = _slot_label(b["req_date"], b["req_hour"])
    log.info("預約回覆 #%s 確認前往 uid=%s…", bid_i, uid[:12])
    reply(rt, [f"已收到您的確認 ✅\n{slot} 我們在門市恭候您的光臨，謝謝！\n"
               "如需改期或取消，請直接在此留言，門市人員會為您處理。"])


# ── 圖文選單 postback（六顆按鈕；data 形如 action=booking）────
def _my_bookings_text(uid: str) -> str:
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT req_date, req_hour, note, status, confirm_status "
                    "FROM booking_requests "
                    "WHERE line_user_id=%s AND req_date >= CURDATE() "
                    "AND status <> 'cancelled' "
                    "ORDER BY req_date, req_hour LIMIT 5", (uid,))
        rows = cur.fetchall()
    if not rows:
        return "您目前沒有預約。點選單「我要預約」即可線上預約 😊"
    st = {"new": "（待門市確認）", "handled": "（已確認）"}
    cf = {"confirmed": "✅已回覆前往"}
    lines = ["📅 您的預約："]
    for r in rows:
        lines.append(f"‧{_slot_label(r['req_date'], r['req_hour'])}"
                     f"{st.get(r['status'], '')}"
                     + (f"{cf[r['confirm_status']]}" if r["confirm_status"] in cf else "")
                     + (f"—{r['note']}" if r["note"] else ""))
    lines.append("\n如需更改或取消，請直接留言告知門市人員。")
    return "\n".join(lines)


def _handle_postback(ev: Dict[str, Any]) -> None:
    uid = (ev.get("source") or {}).get("userId")
    rt = ev.get("replyToken") or ""
    data = (ev.get("postback") or {}).get("data") or ""
    kv = dict(p.split("=", 1) for p in data.split("&") if "=" in p)
    act = kv.get("action")
    if not uid:
        return
    if act == "bkok":                   # 預約提醒訊息的「確認前往」
        _handle_booking_reply(uid, rt, kv.get("id", ""))
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
    if t == "follow":                       # 加好友 → 記入配對名單＋歡迎詞（完整版只在這裡出現）
        _upsert_follower(uid)
        reply(ev.get("replyToken") or "", [WELCOME])
    elif t == "message":
        mtype = (ev.get("message") or {}).get("type")
        if mtype == "text":
            _handle_text(ev)
        else:
            # 非文字訊息。**圖片**：下載存磁碟並交給「好卡 0002」的視覺模型看圖產草稿
            #   （⚠ 絕不可送客服 AI 那張 573，vision 推論必崩且靜默）。
            # 其餘型別（貼圖/語音/影片/檔案/位置）只記一筆，內容由店員在官方後台查看。
            with _conn() as db, db.cursor() as cur:
                cur.execute("SELECT id FROM customers WHERE line_user_id=%s", (uid,))
                row = cur.fetchone()
            cid = row["id"] if row else None
            label = {"image": "[客戶傳了圖片]", "sticker": "[貼圖]", "audio": "[語音訊息]",
                     "video": "[影片]", "file": "[檔案]", "location": "[位置資訊]"}.get(
                         mtype, f"[{mtype}]")
            rid = log_msg(uid, "in", "customer", label, cid,
                          meta=None if mtype == "image" else "非文字訊息，未送 AI")
            if mtype == "image":
                threading.Thread(target=_media_worker, daemon=True, args=(
                    uid, (ev.get("message") or {}).get("id") or "", rid, cid,
                    ev.get("replyToken") or "")).start()
            if row is None:
                _upsert_follower(uid)
                _maybe_hint(uid, ev.get("replyToken") or "")
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
