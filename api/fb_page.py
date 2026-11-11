"""
fb_page.py — Facebook 粉專整合（獨立模組，比照 line_bot.py）

範圍：
- 發文：官網文章分享到粉專（連結貼文；Facebook 自己抓官網的分享圖與標題）。
- 收訊：webhook 接 Messenger 私訊與粉專貼文留言，存進 fb_threads／fb_messages，
  店員在 CRM「粉專訊息」頁直接回覆（兩種回覆都不收費，也不佔 LINE 推播額度）。
- AI：Messenger 依 config.facebook.ai_reply（off／draft／auto）；**公開留言一律只產草稿**，
  店員看過才送（公開回覆說錯收不回，醫療器材用語也有限制）。

開發模式：config.facebook.page_token 未設時，所有發送只記 log 不打 Graph API；
webhook（驗簽、存檔、AI 草稿）照常運作，可在區網完整測試。

安全：
- POST 每個請求都驗 X-Hub-Signature-256（HMAC-SHA256, app secret），驗不過一律 401。
- GET 訂閱驗證比對 hub.verify_token，對不上 403。
- 呼叫 Graph API 一律帶 appsecret_proof；權杖放在 Authorization 標頭，不進網址也不進 log。
- 留言與私訊是任何人都寫得進來的內容：只當資料存、只交給本機 AI 產文字，不觸發其他動作。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pymysql
import pymysql.cursors
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

import config
from api import ai_chat, dbpool, wording

log = logging.getLogger("y1crm.fb")
router = APIRouter()

_GRAPH = "https://graph.facebook.com"
_GRAPH_VIDEO = "https://graph-video.facebook.com"     # 上傳影片要用這個網域
WINDOW_HOURS = 24          # Messenger 標準回覆視窗：客人最後一則訊息後 24 小時內
MAX_MESSENGER = 2000       # Messenger 單則文字上限
MAX_COMMENT = 8000
MAX_POST = 5000            # 粉專貼文我們自己的上限（Facebook 允許更長，但沒必要）


class GraphError(RuntimeError):
    """Graph API 失敗：訊息直接顯示給店員。"""


def _conn():
    return dbpool.connect(**config.db_params(),
                          cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def _cfg(key: str) -> str:
    return str(config.FACEBOOK.get(key) or "")


def dev_mode() -> bool:
    return not _cfg("page_token")


# ── 簽章與 Graph API ─────────────────────────────────────────

def verify_signature(body: bytes, header: str, secret: str) -> bool:
    """X-Hub-Signature-256: sha256=<hex>（HMAC-SHA256，金鑰＝App 密鑰）。"""
    if not secret or not header or not header.startswith("sha256="):
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac.encode(), header[7:].encode())   # 用 bytes：str 版遇到非 ASCII 會丟例外


def appsecret_proof(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def _friendly(err: Dict[str, Any], http: int) -> str:
    code, sub = err.get("code"), err.get("error_subcode")
    if code == 190:
        return "粉專權杖失效或過期，請到 Meta 企業管理平台重新產生系統使用者權杖。"
    if code == 10 and sub == 2018278:
        return f"已超過 {WINDOW_HOURS} 小時，Messenger 不允許再回覆這位客人；請改用電話或 LINE 聯絡。"
    if code == 551:
        return "這位客人目前無法接收訊息（可能封鎖了粉專）。"
    if code == 368:
        return "Facebook 暫時擋下這個動作（可能被判定為大量發送），請稍後再試。"
    if code in (10, 200) or (isinstance(code, int) and 200 <= code < 300):
        return f"權限不足：{err.get('message') or '請確認系統使用者權杖勾了需要的權限'}"
    return f"Facebook 回應錯誤（HTTP {http}）：{err.get('message') or '原因不明'}"


def _graph(path: str, payload: Optional[Dict[str, Any]] = None,
           method: str = "POST", files: Optional[Dict[str, Tuple[str, bytes, str]]] = None,
           host: str = _GRAPH, timeout: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """用粉專權杖呼叫 Graph API。開發模式只記 log、回 None；失敗丟 GraphError。
    files＝{欄位: (檔名, 內容, 類型)}：上傳檔案（multipart）。"""
    if dev_mode():
        log.info("[FB 開發模式，未真發] %s %s %s%s", method, path,
                 json.dumps(payload or {}, ensure_ascii=False)[:500],
                 f"（附檔 {', '.join(f'{k} {len(v[1])} bytes' for k, v in files.items())}）" if files else "")
        return None
    extra: Dict[str, Any] = {"files": files} if files else {}
    if host != _GRAPH:
        extra["host"] = host
    if timeout:
        extra["timeout"] = timeout
    return _call(_page_token(), path, payload, method, **extra)


_tok_cache: Dict[str, str] = {}   # config 的權杖 → 實際使用的粉專權杖


def _page_token() -> str:
    """config 的 page_token 可以直接是粉專權杖，也可以是系統使用者權杖（Meta 後台產生的那串）：
    後者第一次使用時換成這個粉專的權杖（一樣不會過期），之後用快取。"""
    raw = _cfg("page_token")
    if raw not in _tok_cache:
        page = _cfg("page_id")
        me = _call(raw, "me", {"fields": "id"}, "GET")
        if not page or str(me.get("id")) == page:
            tok = raw
        else:
            tok = _call(raw, page, {"fields": "access_token"}, "GET").get("access_token")
            if not tok:
                raise GraphError("系統使用者沒有這個粉專的權限：請在企業管理平台把粉專指派給系統使用者（完整控制）。")
        _tok_cache.clear()
        _tok_cache[raw] = tok
    return _tok_cache[raw]


def multipart(fields: Dict[str, Any], files: Dict[str, Tuple[str, bytes, str]], boundary: str) -> bytes:
    """multipart/form-data 的內容（上傳相片用）。"""
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (fname, data, ctype) in files.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                   f'Content-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out)


def _call(token: str, path: str, payload: Optional[Dict[str, Any]],
          method: str, files: Optional[Dict[str, Tuple[str, bytes, str]]] = None,
          host: str = _GRAPH, timeout: Optional[int] = None) -> Dict[str, Any]:
    ver = _cfg("graph_version") or "v24.0"
    data = dict(payload or {})
    if _cfg("app_secret"):
        data["appsecret_proof"] = appsecret_proof(token, _cfg("app_secret"))
    url = f"{host}/{ver}/{path}"
    body = None
    ctype = "application/json"
    if method == "GET":
        url += "?" + urllib.parse.urlencode(data)
    elif files:
        boundary = "y1crm" + secrets.token_hex(12)
        body, ctype = multipart(data, files, boundary), f"multipart/form-data; boundary={boundary}"
    else:
        body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": ctype, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or (60 if files else 15)) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}").get("error") or {}
        except ValueError:
            err = {}
        log.warning("Graph API 失敗 %s %s: HTTP %s code=%s %s", method, path, e.code,
                    err.get("code"), err.get("message"))
        if err.get("code") == 190:
            _tok_cache.clear()        # 權杖失效：下次重新換
        raise GraphError(_friendly(err, e.code)) from None
    except Exception as e:  # noqa: BLE001
        log.warning("Graph API 連線失敗 %s %s: %s", method, path, e)
        raise GraphError("連不上 Facebook，請稍後再試。") from None


def publish_link(message: str, link: str) -> Optional[str]:
    """粉專發一則連結貼文。回貼文 id；開發模式回 None。"""
    return publish_post(message, link)


def publish_post(message: str, link: Optional[str] = None) -> Optional[str]:
    """粉專發一則貼文（有 link 就是連結貼文，Facebook 會自己抓那頁的圖和標題）。回貼文 id；開發模式回 None。"""
    payload = {"message": message}
    if link:
        payload["link"] = link
    r = _graph(f"{_cfg('page_id') or 'me'}/feed", payload)
    return (r or {}).get("id")


def publish_photo(message: str, image: bytes) -> Optional[str]:
    """粉專發一則相片貼文（一張圖＋文字，圖是 JPEG 內容）。回貼文 id（不是相片 id）；開發模式回 None。
    Facebook 不讓程式替已經發出的貼文加圖，所以圖要在發文時一起送。"""
    r = _graph(f"{_cfg('page_id') or 'me'}/photos", {"message": message, "published": "true"},
               files={"source": ("image.jpg", image, "image/jpeg")})
    return (r or {}).get("post_id") or (r or {}).get("id")


def publish_video(message: str, title: str, video: bytes, thumb: Optional[bytes] = None) -> Optional[str]:
    """粉專發一則影片貼文（MP4＋文字，可附封面 JPEG）。回影片 id；開發模式回 None。
    影片直接放在 Facebook 上，不貼 YouTube 連結：外部連結的觸及比原生影片低（John 2026-11-08 先拿 #42 試）。"""
    files = {"source": ("video.mp4", video, "video/mp4")}
    if thumb:
        files["thumb"] = ("thumb.jpg", thumb, "image/jpeg")
    r = _graph(f"{_cfg('page_id') or 'me'}/videos", {"description": message, "title": title, "published": "true"},
               files=files, host=_GRAPH_VIDEO, timeout=300)
    return (r or {}).get("id")


FB_NO_NAME = "臉書用戶"     # Graph API 不給推薦者的名字：先放這個，店員問同意時再改成對方的稱呼


def recommendations() -> List[Dict[str, Any]]:
    """粉專的「推薦」（正面、有文字的）→ 好評轉貼草稿要的欄位。開發模式回空清單。
    Facebook 不給推薦者名字、也沒有單則推薦的網址：名字先放 FB_NO_NAME，原文連結指到粉專的評論頁。"""
    page = _cfg("page_id") or "me"
    rows: List[Dict[str, Any]] = []
    after = None
    for _ in range(20):
        q: Dict[str, Any] = {"fields": "created_time,recommendation_type,review_text", "limit": 100}
        if after:
            q["after"] = after
        r = _graph(f"{page}/ratings", q, method="GET")
        if r is None:
            return []
        rows += r.get("data") or []
        after = ((r.get("paging") or {}).get("cursors") or {}).get("after")
        if not (r.get("paging") or {}).get("next"):
            break
    out = []
    for x in rows:
        text = (x.get("review_text") or "").strip()
        if x.get("recommendation_type") != "positive" or not text:
            continue
        out.append({"author": "", "person": FB_NO_NAME, "posted": (x.get("created_time") or "")[:10],
                    "rating": None, "original": text, "url": f"https://www.facebook.com/{page}/reviews"})
    return out


def reply_comment(comment_id: str, text: str) -> Optional[str]:
    """以粉專身分回覆留言。回新留言 id；開發模式回 None。"""
    r = _graph(f"{comment_id}/comments", {"message": text[:MAX_COMMENT]})
    return (r or {}).get("id")


def send_message(psid: str, text: str) -> Optional[str]:
    """Messenger 回覆（RESPONSE＝24 小時視窗內的一般回覆）。回訊息 id；開發模式回 None。"""
    r = _graph(f"{_cfg('page_id') or 'me'}/messages", {
        "recipient": {"id": psid}, "messaging_type": "RESPONSE",
        "message": {"text": text[:MAX_MESSENGER]}})
    return (r or {}).get("message_id")


def _profile_name(psid: str) -> Optional[str]:
    try:
        r = _graph(psid, {"fields": "name"}, method="GET")
    except GraphError:
        return None
    return (r or {}).get("name")


# ── webhook 內容整理（純函式）────────────────────────────────

_ATTACH = {"image": "[客人傳了圖片]", "video": "[影片]", "audio": "[語音訊息]",
           "file": "[檔案]", "location": "[位置資訊]", "fallback": "[連結]"}


def _attach_label(msg: Dict[str, Any]) -> str:
    kinds = [a.get("type") for a in msg.get("attachments") or []]
    if not kinds:
        return "[非文字訊息]"
    if msg.get("sticker_id"):
        return "[貼圖]"
    return "、".join(_ATTACH.get(k, f"[{k}]") for k in kinds)


def parse(payload: Dict[str, Any], page_id: str, app_id: str = "") -> List[Dict[str, Any]]:
    """把 webhook 內容整理成要處理的事件。

    每筆：{op: add/edit/remove, kind: messenger/comment, ext_id, post_id, person_id, person_name,
           direction: in/out, source: customer/staff, mid, text, plain（有文字＝可以交給 AI）}
    - Messenger 回音（粉專自己送出的訊息）：我們的 app 送的略過（送出當下已記錄），
      其他（店員在 Meta Business Suite 直接回）記成店員訊息。
    - 留言：第一層留言自成一串；回覆併進它的第一層留言（Facebook 的回覆只有一層）。
    """
    out: List[Dict[str, Any]] = []
    if payload.get("object") != "page":
        return out
    for entry in payload.get("entry") or []:
        for m in entry.get("messaging") or []:
            msg = m.get("message")
            if not isinstance(msg, dict):
                continue                       # 已讀、送達、按讚表情等都不處理
            sender = str((m.get("sender") or {}).get("id") or "")
            recipient = str((m.get("recipient") or {}).get("id") or "")
            text = msg.get("text") or _attach_label(msg)
            if msg.get("is_echo"):
                if app_id and str(msg.get("app_id") or "") == str(app_id):
                    continue
                out.append({"op": "add", "kind": "messenger", "ext_id": recipient, "post_id": None,
                            "person_id": recipient, "person_name": None, "direction": "out",
                            "source": "staff", "mid": msg.get("mid"), "text": text,
                            "plain": bool(msg.get("text"))})
            elif sender and sender != page_id:
                out.append({"op": "add", "kind": "messenger", "ext_id": sender, "post_id": None,
                            "person_id": sender, "person_name": None, "direction": "in",
                            "source": "customer", "mid": msg.get("mid"), "text": text,
                            "plain": bool(msg.get("text"))})
        for ch in entry.get("changes") or []:
            v = ch.get("value") or {}
            if ch.get("field") != "feed" or v.get("item") != "comment":
                continue
            cid, post = v.get("comment_id"), v.get("post_id")
            if not cid:
                continue
            parent = v.get("parent_id")
            top = parent if parent and parent != post else cid
            who = v.get("from") or {}
            mine = str(who.get("id") or "") == page_id
            op = {"add": "add", "edited": "edit", "remove": "remove"}.get(v.get("verb"))
            if not op:
                continue                       # hide／unhide 等不處理
            text = v.get("message") or ("[留言附圖片]" if v.get("photo") else "")
            out.append({"op": op, "kind": "comment", "ext_id": top, "post_id": post,
                        "person_id": None if mine else who.get("id"),
                        "person_name": None if mine else who.get("name"),
                        "direction": "out" if mine else "in",
                        "source": "staff" if mine else "customer", "mid": cid, "text": text,
                        "plain": bool(v.get("message"))})
    return out


# ── 粉專貼文的文字檢查（純函式）──────────────────────────────
#   規則在 api/wording.py（官網好評轉貼也用同一套）：價格一律擋；療效／最高級用語只提醒，由店員決定。
def check_post_text(text: str) -> Tuple[List[str], List[str]]:
    """回 (錯誤, 提醒)。錯誤不能發；提醒要店員確認後才發。"""
    t = (text or "").strip()
    errors: List[str] = []
    if not t:
        errors.append("請填寫貼文內容。")
    if len(t) > MAX_POST:
        errors.append(f"貼文太長（{len(t)} 字），請控制在 {MAX_POST} 字以內。")
    p = wording.price_word(t)
    if p:
        errors.append(f"貼文不可以提到價格（找到「{p}」）。")
    warns = [f"「{w}」可能被當成療效或誇大宣傳" for w in wording.claim_words(t)]
    return errors, warns


SHARE_PATHS = {"article": "/column/", "care": "/care/"}   # 只有這兩種有自己的網頁


def post_url(site: str, kind: str, slug: str) -> Optional[str]:
    path = SHARE_PATHS.get(kind)
    return f"{site.rstrip('/')}{path}{urllib.parse.quote(slug)}" if path and slug else None


def default_post_text(title: str, summary: str) -> str:
    return "\n\n".join(x for x in ((title or "").strip(), (summary or "").strip()) if x)


# ── 對話與訊息（資料庫）──────────────────────────────────────

def _thread(cur, ev: Dict[str, Any]) -> int:
    cur.execute("INSERT INTO fb_threads (kind, ext_id, post_id, person_id, person_name) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id), post_id=COALESCE(post_id, VALUES(post_id)), "
                "  person_id=COALESCE(person_id, VALUES(person_id)), "
                "  person_name=COALESCE(person_name, VALUES(person_name))",
                (ev["kind"], ev["ext_id"], ev.get("post_id"), ev.get("person_id"),
                 (ev.get("person_name") or None) and ev["person_name"][:100]))
    return cur.lastrowid


def log_msg(cur, tid: int, direction: str, source: str, text: str, mid: Optional[str] = None,
            status: str = "new", meta: Optional[str] = None, by: Optional[str] = None) -> Tuple[int, bool]:
    """記一則訊息。回 (id, 是否新的)。ext_mid 重複＝webhook 重送或自己送出的回音，不重複記。"""
    cur.execute("INSERT IGNORE INTO fb_messages (thread_id, direction, source, ext_mid, text, status, meta, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (tid, direction, source, mid or None, (text or "")[:5000], status,
                 (meta or None) and meta[:255], by))
    if cur.rowcount == 0:
        row = None
        if mid:
            cur.execute("SELECT id FROM fb_messages WHERE ext_mid=%s", (mid,))
            row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"fb_messages 寫入失敗 thread={tid}")
        return row["id"], False
    rid = cur.lastrowid      # ⚠ 緊接 INSERT 讀，否則會被下一句蓋掉
    sets = ["last_msg_at=NOW()"]
    if source == "customer":
        sets.append("last_in_at=NOW()")
    if direction == "out" and status == "sent":
        sets.append("last_out_at=NOW()")
    cur.execute(f"UPDATE fb_threads SET {', '.join(sets)} WHERE id=%s", (tid,))
    return rid, True


def _claim_sent(cur, tid: int, source: str, text: str, mid: Optional[str], by: Optional[str],
                meta: Optional[str] = None) -> int:
    """記下我們自己送出的回覆。webhook 的回音可能先到、已經用同一個 id 記成店員訊息，這時改回正確來源。"""
    rid, new = log_msg(cur, tid, "out", source, text, mid, status="sent", meta=meta, by=by)
    if not new:
        cur.execute("UPDATE fb_messages SET source=%s, created_by=%s, meta=%s WHERE id=%s",
                    (source, by, (meta or None) and meta[:255], rid))
        cur.execute("UPDATE fb_threads SET last_out_at=NOW() WHERE id=%s", (tid,))
    return rid


def get_thread(cur, tid: int) -> Optional[Dict[str, Any]]:
    cur.execute("SELECT * FROM fb_threads WHERE id=%s", (tid,))
    return cur.fetchone()


def _mode(cur, tid: int) -> str:
    """對話模式（順便處理人工接手逾時）。"""
    t = get_thread(cur, tid)
    if t is None:
        return "bot"
    if t["mode"] == "human" and t["human_until"] and t["human_until"] <= datetime.now():
        cur.execute("UPDATE fb_threads SET mode='bot', human_until=NULL, assigned_to=NULL, "
                    "handoff_reason=NULL WHERE id=%s", (tid,))
        return "bot"
    return t["mode"]


def set_human(cur, tid: int, reason: str, staff: Optional[str] = None) -> None:
    minutes = int(float(config.AI.get("human_hold_hours") or 4) * 60)
    cur.execute("UPDATE fb_threads SET mode='human', human_until=DATE_ADD(NOW(), INTERVAL %s MINUTE), "
                "assigned_to=%s, handoff_reason=%s WHERE id=%s", (minutes, staff, reason, tid))


def set_bot(cur, tid: int) -> None:
    cur.execute("UPDATE fb_threads SET mode='bot', human_until=NULL, assigned_to=NULL, "
                "handoff_reason=NULL WHERE id=%s", (tid,))


def _history(cur, tid: int) -> List[Dict[str, str]]:
    cur.execute("SELECT direction, text FROM fb_messages WHERE thread_id=%s "
                "AND (source='customer' OR (direction='out' AND status='sent')) "
                "ORDER BY id DESC LIMIT 6", (tid,))
    rows = list(reversed(cur.fetchall()))
    return [{"role": "user" if r["direction"] == "in" else "assistant", "content": r["text"]}
            for r in rows]


def in_window(last_in_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    return bool(last_in_at) and (now or datetime.now()) - last_in_at < timedelta(hours=WINDOW_HOURS)


# ── AI ───────────────────────────────────────────────────────

def ai_reply_mode() -> str:
    m = _cfg("ai_reply") or "draft"
    return m if m in ("off", "draft", "auto") else "draft"


def _ai_worker(tid: int, kind: str, psid: str, text: str) -> None:
    """背景執行：webhook 已經回 200，這裡慢慢算。"""
    try:
        with _conn() as db, db.cursor() as cur:
            hist = _history(cur, tid)[:-1]          # 最後一則就是這次的 text
        res = ai_chat.answer(text, hist, channel=kind)
    except Exception:  # noqa: BLE001
        log.exception("粉專 AI 產生回覆失敗 thread=%s", tid)
        return
    meta = f"{res['kind']}/{res.get('reason') or '-'} {res.get('elapsed')}s"
    auto = kind == "messenger" and ai_reply_mode() == "auto"
    try:
        _ai_save(tid, kind, psid, res, meta, auto)
    except RuntimeError:
        log.warning("粉專 AI 回覆沒有存到：對話 %s 已被刪除", tid)   # AI 還在想的時候店員刪了對話


def _ai_save(tid: int, kind: str, psid: str, res: Dict[str, Any], meta: str, auto: bool) -> None:
    with _conn() as db, db.cursor() as cur:
        if kind == "messenger" and res["kind"] in ("handoff", "timeout"):
            set_human(cur, tid, res.get("reason") or "ai")
        if not auto:
            log_msg(cur, tid, "out", "ai_draft", res["text"], status="new", meta=meta)
            return
        try:
            mid = send_message(psid, res["text"])
        except GraphError as e:
            log_msg(cur, tid, "out", "ai", res["text"], status="failed", meta=f"{meta}｜{e}")
            return
        if mid:
            _claim_sent(cur, tid, "ai", res["text"], mid, None, meta)
        else:
            log_msg(cur, tid, "out", "ai", res["text"], status="discarded", meta=f"{meta}｜開發模式，沒有真的送出")
    log.info("粉專 AI %s thread=%s %s", "已回覆" if auto else "草稿已產生", tid, meta)


def _fill_name(tid: int, psid: str) -> None:
    name = _profile_name(psid)
    if name:
        with _conn() as db, db.cursor() as cur:
            cur.execute("UPDATE fb_threads SET person_name=%s WHERE id=%s AND person_name IS NULL",
                        (name[:100], tid))


def handle(ev: Dict[str, Any]) -> None:
    with _conn() as db, db.cursor() as cur:
        if ev["op"] == "edit":
            cur.execute("UPDATE fb_messages SET text=%s, meta='客人編輯過留言' WHERE ext_mid=%s",
                        ((ev["text"] or "")[:5000], ev["mid"]))
            return
        if ev["op"] == "remove":
            # 客人刪了留言：這邊也不留內容
            cur.execute("UPDATE fb_messages SET text='[留言已刪除]', meta='客人刪除了留言' WHERE ext_mid=%s",
                        (ev["mid"],))
            return
        tid = _thread(cur, ev)
        rid, new = log_msg(cur, tid, ev["direction"], ev["source"], ev["text"], ev["mid"],
                           status="sent" if ev["direction"] == "out" else "new")
        if not new:
            return                                # webhook 重送
        if ev["direction"] == "out":
            if ev["kind"] == "messenger":
                set_human(cur, tid, "fb_inbox")   # 店員在 Meta Business Suite 回了，AI 先閉嘴
            return
        need_name = ev["kind"] == "messenger" and not (get_thread(cur, tid) or {}).get("person_name")
        mode = _mode(cur, tid)
    if need_name:
        threading.Thread(target=_fill_name, args=(tid, ev["ext_id"]), daemon=True).start()
    if not ai_chat.enabled() or not ev.get("plain"):
        return                                    # 圖片、貼圖等不送 AI
    if ev["kind"] == "messenger" and (ai_reply_mode() == "off" or mode == "human"):
        return
    threading.Thread(target=_ai_worker, args=(tid, ev["kind"], ev["ext_id"], ev["text"]),
                     daemon=True).start()


def staff_reply(tid: int, text: str, staff: str, draft_id: Optional[int] = None) -> Dict[str, Any]:
    """店員從 CRM 回覆。Messenger 要在 24 小時視窗內；留言回覆在第一層留言底下。"""
    text = (text or "").strip()
    if not text:
        raise GraphError("請輸入要回覆的內容。")
    with _conn() as db, db.cursor() as cur:
        t = get_thread(cur, tid)
        if t is None:
            raise LookupError(tid)
        if t["kind"] == "messenger":
            if not in_window(t["last_in_at"]):
                raise GraphError(f"客人最後一則訊息已超過 {WINDOW_HOURS} 小時，Messenger 不允許再回覆；"
                                 "請改用電話或 LINE 聯絡。")
            mid = send_message(t["ext_id"], text)
        else:
            mid = reply_comment(t["ext_id"], text)
        if mid:
            rid = _claim_sent(cur, tid, "staff", text, mid, staff)
        else:
            rid, _ = log_msg(cur, tid, "out", "staff", text, status="discarded", by=staff,
                             meta="開發模式，沒有真的送出")
        if draft_id:
            cur.execute("UPDATE fb_messages SET status='discarded' WHERE id=%s AND thread_id=%s "
                        "AND source='ai_draft'", (draft_id, tid))
        if t["kind"] == "messenger":
            set_human(cur, tid, "staff", staff)   # 店員回過了，AI 先別插話
        cur.execute("UPDATE fb_threads SET handled_at=NOW() WHERE id=%s", (tid,))
    return {"id": rid, "sent": bool(mid), "dev": dev_mode()}


# ── Webhook ──────────────────────────────────────────────────

@router.get("/api/fb/webhook")
def fb_webhook_verify(request: Request):
    """Meta 後台按「驗證並儲存」時打這裡：verify_token 對得上就把 challenge 原樣回去。"""
    q = request.query_params
    want = _cfg("verify_token")
    got = (q.get("hub.verify_token") or "").encode()
    if q.get("hub.mode") == "subscribe" and want and hmac.compare_digest(got, want.encode()):
        log.info("FB webhook 訂閱驗證成功")
        return PlainTextResponse(q.get("hub.challenge") or "")
    raise HTTPException(403, "verify_token 不符")


@router.post("/api/fb/webhook")
async def fb_webhook(request: Request):
    body = await request.body()
    if not verify_signature(body, request.headers.get("X-Hub-Signature-256", ""), _cfg("app_secret")):
        raise HTTPException(401, "signature 驗證失敗")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "非法 JSON")
    events = parse(payload, _cfg("page_id"), _cfg("app_id"))
    log.info("FB webhook 進站：%d 個事件 %s", len(events),
             [f"{e['kind']}/{e['op']}/{e['direction']}" for e in events])
    for ev in events:
        try:
            handle(ev)
        except Exception:  # noqa: BLE001 — 單一事件失敗不影響其他事件與 200 回應（否則 Meta 會一直重送）
            log.exception("處理 FB 事件失敗：%s", ev.get("kind"))
    return {"ok": True}
