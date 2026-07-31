"""
api/app.py — y1crm 後端（port 8004）

比照 TWIII 架構：FastAPI + pymysql 直連本機 MySQL（crm1），單檔 webui。
自 Blazor 版（archive/y1crm-blazor）轉型而來，資料庫 schema 不變。

端點：
  GET    /                       → webui/index.html
  GET    /api/health             → 服務 + DB 狀態
  POST   /api/login              → 帳密驗證（bcrypt），發 session cookie
  POST   /api/logout             → 清除 session
  GET    /api/me                 → 目前登入者（含公司/門市名稱）
  POST   /api/customers/query    → 多欄位 AND 查詢（任一欄位皆可當條件，上限 200 筆）
  POST   /api/customers/dup      → 重複客戶偵測（姓名/手機/身分證 精確比對，前 5 筆）
  POST   /api/customers          → 建檔（客戶編號留空自動產生 C{yyyyMMdd}-NNN）
  GET    /api/customers/{id}     → 單筆明細
  PUT    /api/customers/{id}     → 更新
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import bcrypt
import pymysql
import pymysql.cursors
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from api import line_bot
from api.line_bot import router as line_router

# DB 連線參數取自 config（config.toml / 環境變數），機密不再寫死於程式碼
_DB = dict(config.db_params(),
           cursorclass=pymysql.cursors.DictCursor, autocommit=True)
config.ensure_dirs()   # 啟動即建立各類上傳目錄（base_dir 之下）

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGO = os.path.join(_ROOT, "webui", "imagesLogoS.png")

_SESSION_COOKIE = "y1crm_session"
_SESSION_TTL = 12 * 3600  # 12 小時

# session 簽章密鑰：首次啟動時產生並存檔，重啟不失效
_SECRET_PATH = os.path.join(_ROOT, ".session_secret")


def _load_secret() -> bytes:
    if os.path.exists(_SECRET_PATH):
        with open(_SECRET_PATH, "rb") as f:
            return f.read()
    key = secrets.token_bytes(32)
    fd = os.open(_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


_SECRET = _load_secret()


def _conn():
    return pymysql.connect(**_DB)


# ── Session：payload(JSON).hmac 的簽章 cookie ────────────────────────────────

def _sign_session(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(_SECRET, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode() + "." + base64.urlsafe_b64encode(sig).decode()


def _verify_session(token: str) -> Optional[Dict[str, Any]]:
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        raw = base64.urlsafe_b64decode(raw_b64)
        sig = base64.urlsafe_b64decode(sig_b64)
        if not hmac.compare_digest(sig, hmac.new(_SECRET, raw, hashlib.sha256).digest()):
            return None
        payload = json.loads(raw)
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _current_user(request: Request) -> Dict[str, Any]:
    token = request.cookies.get(_SESSION_COOKIE)
    payload = _verify_session(token) if token else None
    if payload is None:
        raise HTTPException(401, "未登入或連線已過期，請重新登入")
    # 防越權：level 一律以資料庫當下的值為準，不信任 cookie 內容
    # （改權限即時生效；帳號被移除的舊 session 立即失效）
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT level, realname FROM users WHERE id=%s", (payload.get("uid"),))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(401, "帳號不存在或已停用，請重新登入")
    payload["level"] = row["level"] or "user"
    payload["realname"] = row["realname"] or payload.get("realname") or payload.get("username")
    return payload


# ── 權限可視範圍（單一定義點；所有端點引用這裡，不散落各處）──────────────────

_CAPS = {
    "view_price": {"manager", "admin"},   # 價格欄位可視：經理以上
}


def _can(user: Dict[str, Any], cap: str) -> bool:
    return user.get("level") in _CAPS.get(cap, ())


# 資源 → 價格欄位（未來訂單金額欄位加在這裡即自動遮蔽）
_PRICE_FIELDS = {
    "products": ("list_price", "cost_price"),
    "purchases": ("unit_price",),
}


def _mask_row(table: str, row: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    if not _can(user, "view_price"):
        for f in _PRICE_FIELDS.get(table, ()):
            row.pop(f, None)
    return row


def _menus_for(level: str) -> List[str]:
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT menu_key FROM menu_permissions WHERE level=%s", (level,))
        return [r["menu_key"] for r in cur.fetchall()]


def _require_menu(user: Dict[str, Any], menu: str) -> None:
    """伺服器端選單權限：前端藏按鈕只是外觀，真正的關卡在這裡。"""
    if menu not in _menus_for(user["level"]):
        raise HTTPException(403, "您的帳號沒有使用此功能的權限。")


def _nav_for(level: str) -> List[Dict[str, Any]]:
    """導覽選單樹（menus 表，多層）：葉節點依 perm_key 過濾，
    群組（無 path）沒有任何可見子項就整組隱藏。權限本身仍在 menu_permissions。"""
    allowed = set(_menus_for(level))
    with _conn() as db, db.cursor() as cur:
        cur.execute("SELECT id, parent_id, title, icon, path, perm_key "
                    "FROM menus WHERE is_active=1 ORDER BY sort_order, id")
        rows = cur.fetchall()
    kids: Dict[Any, List[Dict[str, Any]]] = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)

    def build(pid) -> List[Dict[str, Any]]:
        out = []
        for r in kids.get(pid, []):
            if r["perm_key"] and r["perm_key"] not in allowed:
                continue                      # 權限鍵不符：連同子樹一起隱藏
            children = build(r["id"])
            if not r["path"] and not children:
                continue                      # 純群組但無可見子項
            node = {"title": r["title"], "icon": r["icon"] or "", "path": r["path"]}
            if children:
                node["children"] = children
            out.append(node)
        return out

    return build(None)


# ── 欄位定義：表單/查詢/回傳 共用 ────────────────────────────────────────────

# 客戶可寫欄位（key = API/前端欄名 = DB 欄名）
_TEXT_FIELDS = [
    "customer_code", "name", "national_id", "gender",
    "phone_mobile", "phone_home", "email", "contact_name", "contact_phone",
    "household_city", "household_district", "household_village", "household_detail",
    "mailing_city", "mailing_district", "mailing_village", "mailing_detail",
    "notes", "identity",
]
_DATE_FIELDS = ["birth_date", "registration_date", "consent_date"]
_BOOL_FIELDS = ["consent_signed", "subsidy_applied"]
# 身份代碼（codes 表 category='identity'：輕/中/重/極重）
_IDENTITY_KEYS = ("mild", "moderate", "severe", "profound")

_PAGE_SIZE = 10  # 查詢結果每頁筆數（空白查詢＝全部客戶，靠分頁避免一次倒太多）


def _mask_name(s: Optional[str]) -> str:
    """公開螢幕用個資遮罩：中間字以○取代（王○明；二字→王○）。"""
    s = (s or "").strip()
    if len(s) <= 1:
        return s or "○"
    if len(s) == 2:
        return s[0] + "○"
    return s[0] + "○" * (len(s) - 2) + s[-1]


def _mask_phone(s: Optional[str]) -> str:
    """公開螢幕用：電話只留尾三碼。"""
    s = (s or "").strip()
    return ("***" + s[-3:]) if len(s) >= 3 else "***"


def _clean(s: Optional[str]) -> Optional[str]:
    """空白正規化為 None（唯一索引欄位存空字串會撞索引）。"""
    if s is None:
        return None
    s = s.strip()
    return s or None


def _parse_date(s: Optional[str]) -> Optional[str]:
    s = _clean(s)
    if s is None:
        return None
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise HTTPException(422, f"日期格式錯誤：{s}（應為 YYYY-MM-DD）")


def _parse_dt(s: Optional[str]) -> Optional[str]:
    """單一時間（到分）：接受 'YYYY-MM-DD HH:MM' 或 'YYYY-MM-DDTHH:MM'，存 DB。"""
    s = _clean(s)
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s.replace("T", " ")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise HTTPException(422, f"時間格式錯誤：{s}（應為 YYYY-MM-DD HH:MM）")


def _row_json(row: Dict[str, Any]) -> Dict[str, Any]:
    """date/datetime → ISO 字串，方便 JSON 序列化。"""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.strftime("%Y-%m-%d %H:%M")
        elif isinstance(v, date):
            out[k] = v.isoformat()
        elif isinstance(v, bytes):
            out[k] = bool(v[0]) if len(v) == 1 else v.decode("utf8", "replace")
        else:
            out[k] = v
    for b in _BOOL_FIELDS:
        if b in out and out[b] is not None:
            out[b] = bool(out[b])
    return out


def _save_error(e: pymysql.err.IntegrityError) -> HTTPException:
    """把資料庫唯一索引衝突轉成人看得懂的訊息。"""
    msg = str(e)
    if "uq_national_id" in msg:
        return HTTPException(409, "此身分證字號已有其他客戶建檔，請改用查詢開啟該客戶。")
    if "uq_customer_code" in msg:
        return HTTPException(409, "客戶編號重複，請改用其他編號或留空自動產生。")
    if "uq_product_code" in msg:
        return HTTPException(409, "商品編號重複，請改用其他編號或留空。")
    if "uq_product" in msg:
        return HTTPException(409, "同品牌＋型號的商品已存在，請直接編輯該筆。")
    return HTTPException(500, f"儲存失敗：{msg}")


# ── 商品欄位定義（key = API/前端欄名 = DB 欄名）─────────────────────────────
_PRODUCT_TEXT = [
    "product_code", "barcode", "category", "brand", "model_name", "spec", "unit",
    "supplier", "ha_form", "ha_tech_level", "ha_battery", "ha_ip_rating",
    "ha_fit_levels", "subsidy_class", "compatible_models", "notes",
]
_PRODUCT_INT = ["warranty_months", "ha_channels"]
_PRODUCT_DEC = ["list_price", "cost_price"]          # 價格欄位（權限可視）
_PRODUCT_BOOL = ["ha_bluetooth", "ha_tcoil"]
# 欄位 → codes 類別（值必須是該類別現有的 key）
_PRODUCT_CODED = {"category": "product_category", "ha_form": "ha_form",
                  "ha_tech_level": "ha_tech_level", "ha_battery": "battery_type",
                  "subsidy_class": "subsidy_class"}


# ── 交易紀錄欄位定義（purchases；key = API/前端欄名 = DB 欄名）──────────────
_PURCHASE_TEXT = ["serial_number", "notes"]
_PURCHASE_DATE = ["transaction_date", "warranty_start_date", "warranty_end_date"]
# 固定 enum（DB 端 enum 欄；空值＝不填，非法值 422）
_PURCHASE_ENUMS = {
    "transaction_type": {"purchase", "rental", "repair", "return"},
    "ear_side": {"left", "right", "both"},
    "payment_status": {"unpaid", "partial", "paid"},
}


def create_app() -> FastAPI:
    app = FastAPI(title="y1crm", docs_url="/api/docs", redoc_url=None)
    app.include_router(line_router)   # LINE webhook（api/line_bot.py；簽章驗證、不走 session）
    line_bot.start_reminder_scheduler()   # 預約提醒背景排程（每日固定時間推播）

    # ── MPA：每個主功能一頁（layout 繼承共用 header/CSS/JS）────────
    #   程式(static/js)與 UI(templates)分離；切換主功能＝瀏覽器導頁＝記憶體歸零，
    #   利於低階平板。HTML 一律 no-cache；/static 以 ?v= 破快取（重啟即更新）。
    app.mount("/static", StaticFiles(directory=os.path.join(_ROOT, "webui", "static")), name="static")
    templates = Jinja2Templates(directory=os.path.join(_ROOT, "webui", "templates"))
    _V = str(int(time.time()))

    def _page(request: Request, name: str, active: str = ""):
        return templates.TemplateResponse(
            request, name, {"active": active, "v": _V},
            headers={"Cache-Control": "no-cache"})

    @app.get("/login")
    def page_login(request: Request):
        return _page(request, "login.html")

    @app.get("/calendar")
    def page_calendar(request: Request):
        return _page(request, "calendar.html", "calendar")

    @app.get("/customers")
    def page_customers(request: Request):
        return _page(request, "customers.html", "customers")

    @app.get("/products")
    def page_products(request: Request):
        return _page(request, "products.html", "products")

    @app.get("/staff")
    def page_staff(request: Request):
        return _page(request, "staff.html", "staff")

    @app.get("/bookings")
    def page_bookings(request: Request):
        return _page(request, "bookings.html", "bookings")

    @app.get("/purchases")
    def page_purchases(request: Request):
        return _page(request, "purchases.html", "purchases")

    @app.get("/line-chat")
    def page_line_chat(request: Request):
        return _page(request, "line_chat.html", "line-chat")

    @app.get("/board")
    def page_board(request: Request):
        # 店面狀態看板（電視牆）：頁面本身不驗證，資料端點 /api/board 驗金鑰
        return _page(request, "board.html")

    @app.get("/")
    def index():
        # 首頁＝行事曆（各頁的 auth 守衛未登入會自動導 /login）
        return RedirectResponse("/calendar")

    @app.get("/imagesLogoS.png")
    def logo():
        return FileResponse(_LOGO, headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    def health():
        try:
            with _conn() as db, db.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM customers")
                n = cur.fetchone()["n"]
            return {"ok": True, "db": "crm1", "customers": n}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/branding")
    def branding():
        """登入頁顯示用（不需登入）：公司/門市名稱。"""
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT `key`, `value` FROM app_settings")
            settings = {r["key"]: r["value"] for r in cur.fetchall()}
        return {"company_name": settings.get("company_name", ""),
                "branch_name": settings.get("branch_name", "")}

    # ── 認證 ──────────────────────────────────────────────────
    @app.post("/api/login")
    def login(response: Response, body: Dict[str, Any] = Body(...)):
        username = _clean(body.get("username"))
        password = body.get("password") or ""
        if not username or not password:
            raise HTTPException(422, "請輸入帳號與密碼")

        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id, username, password, level, realname FROM users "
                        "WHERE username=%s", (username,))
            user = cur.fetchone()

        stored = (user or {}).get("password") or ""
        ok = False
        if stored.startswith("$2"):
            ok = bcrypt.checkpw(password.encode(), stored.encode())
        elif stored:  # 尚未升級成 bcrypt 的舊明碼（沿用 Blazor 版行為：驗證後就地升級）
            ok = secrets.compare_digest(stored, password)
            if ok:
                new_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                with _conn() as db, db.cursor() as cur:
                    cur.execute("UPDATE users SET password=%s WHERE id=%s",
                                (new_hash, user["id"]))
        if not user or not ok:
            raise HTTPException(401, "帳號或密碼錯誤")

        payload = {
            "uid": user["id"],
            "username": user["username"],
            "realname": user["realname"] or user["username"],
            "level": user["level"] or "user",
            "exp": int(time.time()) + _SESSION_TTL,
        }
        response.set_cookie(_SESSION_COOKIE, _sign_session(payload),
                            max_age=_SESSION_TTL, httponly=True, samesite="lax", path="/")
        return {"ok": True, "realname": payload["realname"], "level": payload["level"]}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(_SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        payload = _current_user(request)
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT `key`, `value` FROM app_settings")
            settings = {r["key"]: r["value"] for r in cur.fetchall()}
        return {"uid": payload.get("uid"),
                "username": payload["username"], "realname": payload["realname"],
                "level": payload["level"],
                "menus": _menus_for(payload["level"]),
                "nav": _nav_for(payload["level"]),
                "caps": [c for c, lv in _CAPS.items() if payload["level"] in lv],
                "company_name": settings.get("company_name", ""),
                "branch_name": settings.get("branch_name", "")}

    # ── 行事曆統一饋送：回訪提醒＋客戶待辦＋個人/門市事件 ────────
    #   kind＝reminder | todo | event；個人事件只回擁有者本人的。
    @app.get("/api/calendar")
    def calendar(request: Request, start: str, end: str):
        user = _current_user(request)
        _require_menu(user, "calendar")
        s, e = _parse_date(start), _parse_date(end)
        if not s or not e:
            raise HTTPException(422, "請提供 start 與 end（YYYY-MM-DD）")
        rows: List[Dict[str, Any]] = []
        with _conn() as db, db.cursor() as cur:
            # 1) 回訪提醒（沿用舊表）
            cur.execute(
                "SELECT r.id, r.customer_id, r.scheduled_date, r.reminder_type, "
                "       r.status, r.notes, "
                "       c.name AS customer_name, c.phone_mobile AS phone "
                "FROM follow_up_reminders r "
                "JOIN customers c ON c.id = r.customer_id "
                "WHERE r.scheduled_date >= %s AND r.scheduled_date < %s "
                "  AND r.status <> 'cancelled' "
                "ORDER BY r.scheduled_date, r.id", (s, e))
            for r in cur.fetchall():
                rows.append({"kind": "reminder", **_row_json(r)})
            # 2) 客戶待辦（有 due_at 才上行事曆）
            cur.execute(
                "SELECT t.id, t.customer_id, t.category, t.description, t.priority, "
                "       t.due_at, t.done_at, c.name AS customer_name "
                "FROM customer_todos t JOIN customers c ON c.id = t.customer_id "
                "WHERE t.due_at >= %s AND t.due_at < %s "
                "ORDER BY t.due_at, t.id", (s, e))
            for r in cur.fetchall():
                rows.append({"kind": "todo", **_row_json(r)})
            # 3) 個人（只看自己的）＋門市（全店共見）事件
            cur.execute(
                "SELECT id, scope, owner, category, title, description, "
                "       start_at, end_at, all_day, created_by "
                "FROM calendar_events "
                "WHERE start_at >= %s AND start_at < %s "
                "  AND (scope='store' OR owner=%s) "
                "ORDER BY start_at, id", (s, e, user.get("username")))
            for r in cur.fetchall():
                r["all_day"] = bool(r["all_day"])
                rows.append({"kind": "event", **_row_json(r)})
        return {"rows": rows}

    # ── 個人/門市行事曆事件 CRUD（calendar_events）──────────────
    _EVENT_SCOPES = ("personal", "store")

    def _collect_event(body: Dict[str, Any]) -> Dict[str, Any]:
        title = _clean(body.get("title"))
        if not title:
            raise HTTPException(422, "請填寫標題。")
        scope = _clean(body.get("scope")) or "personal"
        if scope not in _EVENT_SCOPES:
            raise HTTPException(422, f"scope 不正確：{scope}")
        start_at = _parse_dt(body.get("start_at"))
        if not start_at:
            raise HTTPException(422, "請提供開始時間。")
        end_at = _parse_dt(body.get("end_at"))
        return {"scope": scope, "category": _clean(body.get("category")),
                "title": title, "description": _clean(body.get("description")),
                "start_at": start_at, "end_at": end_at,
                "all_day": 1 if body.get("all_day") else 0}

    def _event_or_404(cur, eid: int) -> Dict[str, Any]:
        cur.execute("SELECT * FROM calendar_events WHERE id=%s", (eid,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "找不到這筆行事曆事件（可能已被刪除）。")
        return row

    def _require_event_access(user: Dict[str, Any], row: Dict[str, Any]) -> None:
        # 個人事件只有擁有者能改；門市事件開放給所有有行事曆權限的人
        if row["scope"] == "personal" and row["owner"] != user.get("username"):
            raise HTTPException(403, "只能修改自己的個人行事曆。")

    @app.post("/api/events")
    def create_event(request: Request, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "calendar")
        d = _collect_event(body)
        owner = user.get("username") if d["scope"] == "personal" else None
        with _conn() as db, db.cursor() as cur:
            cur.execute(
                "INSERT INTO calendar_events "
                "(scope, owner, category, title, description, start_at, end_at, all_day, "
                " created_by, updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (d["scope"], owner, d["category"], d["title"], d["description"],
                 d["start_at"], d["end_at"], d["all_day"],
                 user["realname"], user["realname"]))
            row = _event_or_404(cur, cur.lastrowid)
        row["all_day"] = bool(row["all_day"])
        return _row_json(row)

    @app.put("/api/events/{eid}")
    def update_event(request: Request, eid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "calendar")
        d = _collect_event(body)
        owner = user.get("username") if d["scope"] == "personal" else None
        with _conn() as db, db.cursor() as cur:
            row = _event_or_404(cur, eid)
            _require_event_access(user, row)
            cur.execute(
                "UPDATE calendar_events SET scope=%s, owner=%s, category=%s, title=%s, "
                "description=%s, start_at=%s, end_at=%s, all_day=%s, updated_by=%s "
                "WHERE id=%s",
                (d["scope"], owner, d["category"], d["title"], d["description"],
                 d["start_at"], d["end_at"], d["all_day"], user["realname"], eid))
            row = _event_or_404(cur, eid)
        row["all_day"] = bool(row["all_day"])
        return _row_json(row)

    @app.delete("/api/events/{eid}")
    def delete_event(request: Request, eid: int):
        user = _current_user(request)
        _require_menu(user, "calendar")
        with _conn() as db, db.cursor() as cur:
            row = _event_or_404(cur, eid)
            _require_event_access(user, row)
            cur.execute("DELETE FROM calendar_events WHERE id=%s", (eid,))
        return {"ok": True}

    # ── 人員管理（users；選單權限 staff＝僅 admin）───────────────
    #   密碼一律 bcrypt 雜湊，任何端點都不回傳；只能設定新密碼、不能查看。
    _STAFF_FIELDS = ("id", "username", "realname", "level", "photo_path", "cdate")

    def _staff_row(cur, uid: int) -> Dict[str, Any]:
        cur.execute("SELECT id, username, realname, level, photo_path, cdate "
                    "FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "找不到這位人員（可能已被刪除）。")
        row["has_photo"] = bool(row.pop("photo_path"))
        return _row_json(row)

    def _valid_levels(cur) -> set:
        cur.execute("SELECT `key` FROM codes WHERE category='level'")
        return {r["key"] for r in cur.fetchall()}

    def _collect_staff(cur, body: Dict[str, Any], *, creating: bool) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        username = _clean(body.get("username"))
        realname = _clean(body.get("realname"))
        level = _clean(body.get("level"))
        password = body.get("password") or ""
        if creating and not username:
            raise HTTPException(422, "請填寫帳號。")
        if username:
            d["username"] = username
        if realname:
            d["realname"] = realname
        elif creating:
            raise HTTPException(422, "請填寫姓名。")
        if level:
            if level not in _valid_levels(cur):
                raise HTTPException(422, f"權限代碼不正確：{level}")
            d["level"] = level
        elif creating:
            raise HTTPException(422, "請選擇權限。")
        if password:
            if len(password) < 6:
                raise HTTPException(422, "密碼至少 6 個字元。")
            d["password"] = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        elif creating:
            raise HTTPException(422, "請設定密碼（至少 6 個字元）。")
        return d

    def _is_last_admin(cur, uid: int) -> bool:
        cur.execute("SELECT level FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        if not row or row["level"] != "admin":
            return False
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE level='admin'")
        return cur.fetchone()["n"] <= 1

    @app.get("/api/staff")
    def list_staff(request: Request):
        _require_menu(_current_user(request), "staff")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id, username, realname, level, photo_path, cdate "
                        "FROM users ORDER BY id")
            rows = []
            for r in cur.fetchall():
                r["has_photo"] = bool(r.pop("photo_path"))
                rows.append(_row_json(r))
            return {"rows": rows}

    @app.post("/api/staff")
    def create_staff(request: Request, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "staff")
        with _conn() as db, db.cursor() as cur:
            d = _collect_staff(cur, body, creating=True)
            cur.execute("SELECT id FROM users WHERE username=%s", (d["username"],))
            if cur.fetchone():
                raise HTTPException(422, f"帳號「{d['username']}」已存在。")
            cur.execute("INSERT INTO users (username, password, level, realname, cdate) "
                        "VALUES (%s,%s,%s,%s,NOW())",
                        (d["username"], d["password"], d["level"], d["realname"]))
            return _staff_row(cur, cur.lastrowid)

    @app.put("/api/staff/{uid}")
    def update_staff(request: Request, uid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "staff")
        with _conn() as db, db.cursor() as cur:
            _staff_row(cur, uid)   # 404 檢查
            d = _collect_staff(cur, body, creating=False)
            # 防鎖死：不能變更自己的權限；最後一位 admin 不能被降級
            if "level" in d:
                if uid == user.get("uid"):
                    raise HTTPException(422, "不能變更自己的權限（避免把自己鎖在門外）。")
                if d["level"] != "admin" and _is_last_admin(cur, uid):
                    raise HTTPException(422, "這是最後一位管理員，不能降級。")
            if "username" in d:
                cur.execute("SELECT id FROM users WHERE username=%s AND id<>%s",
                            (d["username"], uid))
                if cur.fetchone():
                    raise HTTPException(422, f"帳號「{d['username']}」已存在。")
            if not d:
                raise HTTPException(422, "沒有可更新的欄位。")
            sets = ", ".join(f"`{k}`=%s" for k in d)
            cur.execute(f"UPDATE users SET {sets} WHERE id=%s", list(d.values()) + [uid])
            return _staff_row(cur, uid)

    @app.delete("/api/staff/{uid}")
    def delete_staff(request: Request, uid: int):
        user = _current_user(request)
        _require_menu(user, "staff")
        if uid == user.get("uid"):
            raise HTTPException(422, "不能刪除自己的帳號。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT photo_path FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "找不到這位人員（可能已被刪除）。")
            if _is_last_admin(cur, uid):
                raise HTTPException(422, "這是最後一位管理員，不能刪除。")
            cur.execute("DELETE FROM users WHERE id=%s", (uid,))
        if row["photo_path"]:
            config.delete_file(row["photo_path"])
        return {"ok": True}

    # 人員照片（單張，回補即替換；檔案落磁碟 staff/）
    @app.put("/api/staff/{uid}/photo")
    async def set_staff_photo(request: Request, uid: int):
        user = _current_user(request)
        _require_menu(user, "staff")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        if not mime.startswith("image/"):
            raise HTTPException(422, "只接受圖片檔案。")
        data = await request.body()
        if not data:
            raise HTTPException(422, "沒有收到檔案內容。")
        if len(data) > _PHOTO_MAX:
            raise HTTPException(413, "圖片超過 10MB，請縮小後再上傳。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT photo_path FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "找不到這位人員（可能已被刪除）。")
            rel = config.save_file("staff_photos", data, mime, uid)
            cur.execute("UPDATE users SET photo_path=%s WHERE id=%s", (rel, uid))
        if row["photo_path"]:
            config.delete_file(row["photo_path"])
        return {"ok": True}

    @app.get("/api/staff/{uid}/photo")
    def get_staff_photo(request: Request, uid: int):
        _current_user(request)   # 登入即可看（日後頁首頭像可重用）
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT photo_path FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
        if row is None or not row["photo_path"]:
            raise HTTPException(404, "沒有照片。")
        path = config.abs_path(row["photo_path"])
        if not path.is_file():
            raise HTTPException(404, "照片檔案遺失。")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=300"})

    @app.delete("/api/staff/{uid}/photo")
    def delete_staff_photo(request: Request, uid: int):
        user = _current_user(request)
        _require_menu(user, "staff")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT photo_path FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "找不到這位人員（可能已被刪除）。")
            cur.execute("UPDATE users SET photo_path=NULL WHERE id=%s", (uid,))
        if row["photo_path"]:
            config.delete_file(row["photo_path"])
        return {"ok": True}

    # ── LINE 店員配對（客戶零輸入：加好友→名單→店員在明細頁點綁定）──
    @app.get("/api/line/followers")
    def line_followers(request: Request):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            # 只列「尚未綁定任何客戶」的好友，最近互動在前
            cur.execute(
                "SELECT f.line_user_id, f.display_name, f.picture_url, "
                "       f.followed_at, f.last_seen_at "
                "FROM line_followers f "
                "LEFT JOIN customers c ON c.line_user_id = f.line_user_id "
                "WHERE c.id IS NULL ORDER BY f.last_seen_at DESC LIMIT 20")
            return {"rows": [_row_json(r) for r in cur.fetchall()]}

    @app.post("/api/customers/{cid}/line-bind")
    def line_bind(request: Request, cid: int, body: Dict[str, Any] = Body(...)):
        _require_menu(_current_user(request), "customers")
        uid = _clean(body.get("line_user_id"))
        if not uid:
            raise HTTPException(422, "缺少 line_user_id。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id, line_user_id FROM customers WHERE id=%s", (cid,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "找不到這筆客戶資料（可能已被移除）。")
            if row["line_user_id"] and row["line_user_id"] != uid:
                raise HTTPException(422, "此客戶已綁定其他 LINE 帳號，請先解綁。")
            try:
                cur.execute("UPDATE customers SET line_user_id=%s WHERE id=%s", (uid, cid))
            except pymysql.err.IntegrityError:
                raise HTTPException(422, "此 LINE 帳號已綁定其他客戶。")
        logging.getLogger("y1crm.line").info("LINE 綁定成功 customer_id=%s（店員配對）", cid)
        return {"ok": True}

    @app.delete("/api/customers/{cid}/line-bind")
    def line_unbind(request: Request, cid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("UPDATE customers SET line_user_id=NULL WHERE id=%s", (cid,))
            if cur.rowcount == 0:
                cur.execute("SELECT id FROM customers WHERE id=%s", (cid,))
                if cur.fetchone() is None:
                    raise HTTPException(404, "找不到這筆客戶資料（可能已被移除）。")
        return {"ok": True}

    # ── 預約留言（LINE 線上預約收單；輕量版，不進行事曆）────────
    @app.get("/api/bookings")
    def list_bookings(request: Request, status: str = ""):
        _require_menu(_current_user(request), "customers")
        cond, args = "", []
        if status in ("new", "handled", "cancelled"):
            cond, args = "WHERE b.status=%s", [status]
        with _conn() as db, db.cursor() as cur:
            cur.execute(
                "SELECT b.*, c.name AS customer_name, c.customer_code, "
                "       f.display_name AS line_display_name "
                "FROM booking_requests b "
                "LEFT JOIN customers c ON c.id = b.customer_id "
                "LEFT JOIN line_followers f ON f.line_user_id = b.line_user_id "
                f"{cond} ORDER BY b.created_at DESC, b.id DESC LIMIT 200", args)
            return {"rows": [_row_json(r) for r in cur.fetchall()]}

    @app.put("/api/bookings/{bid}")
    def update_booking(request: Request, bid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "customers")
        st = _clean(body.get("status"))
        if st not in ("new", "handled", "cancelled"):
            raise HTTPException(422, "status 須為 new/handled/cancelled。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id FROM booking_requests WHERE id=%s", (bid,))
            if cur.fetchone() is None:
                raise HTTPException(404, "找不到這筆預約留言。")
            if st == "new":
                cur.execute("UPDATE booking_requests SET status='new', "
                            "handled_by=NULL, handled_at=NULL WHERE id=%s", (bid,))
            else:
                cur.execute("UPDATE booking_requests SET status=%s, "
                            "handled_by=%s, handled_at=NOW() WHERE id=%s",
                            (st, user["realname"], bid))
        return {"ok": True}

    @app.post("/api/bookings/{bid}/remind")
    def remind_booking(request: Request, bid: int, body: Dict[str, Any] = Body(None)):
        """手動補送預約提醒（客戶端收到附確認按鈕的訊息）。force=true 可重送。"""
        _require_menu(_current_user(request), "customers")
        r = line_bot.send_booking_reminder(bid, force=bool((body or {}).get("force")))
        if not r["ok"]:
            raise HTTPException(422, r["msg"])
        return r

    @app.post("/api/bookings/run-reminders")
    def run_booking_reminders(request: Request, body: Dict[str, Any] = Body(None)):
        """手動觸發整批提醒（預設同排程：reminder_days_ahead 天後）。"""
        _require_menu(_current_user(request), "customers")
        d = _clean((body or {}).get("date"))
        return line_bot.run_booking_reminders(
            date.fromisoformat(d) if d else None)

    # ── LINE 客服對話（AI 草稿 + 人工接手）────────────────────
    @app.get("/api/line/conversations")
    def line_conversations(request: Request):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute(
                # 人工接手逾時只在客戶下次來訊時才實際復原（line_bot._conv_mode），
                # 這裡先算出「有效模式」，避免清單顯示成還在人工接手中。
                "SELECT v.line_user_id, "
                "       CASE WHEN v.mode='human' AND (v.human_until IS NULL "
                "                 OR v.human_until > NOW()) THEN 'human' ELSE 'bot' END AS mode, "
                "       v.human_until, v.assigned_to, "
                "       v.handoff_reason, v.last_msg_at, "
                "       f.display_name, f.picture_url, c.id AS customer_id, c.name AS customer_name, "
                "       (SELECT text FROM line_messages m WHERE m.line_user_id=v.line_user_id "
                "          ORDER BY m.id DESC LIMIT 1) AS last_text, "
                "       (SELECT COUNT(*) FROM line_messages m WHERE m.line_user_id=v.line_user_id "
                "          AND m.source='ai_draft' AND m.status='new') AS drafts "
                "FROM line_conversations v "
                "LEFT JOIN line_followers f ON f.line_user_id = v.line_user_id "
                "LEFT JOIN customers c ON c.line_user_id = v.line_user_id "
                "ORDER BY v.last_msg_at DESC, v.updated_at DESC LIMIT 100")
            return {"rows": [_row_json(r) for r in cur.fetchall()],
                    "auto_reply": bool(config.AI.get("auto_reply")),
                    "ai_enabled": bool(config.AI.get("enabled"))}

    @app.get("/api/line/conversations/{uid}/messages")
    def line_conv_messages(request: Request, uid: str):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id, direction, source, text, status, meta, created_at, "
                        "       (rel_path IS NOT NULL) AS has_media, mime "
                        "FROM line_messages WHERE line_user_id=%s "
                        "ORDER BY id DESC LIMIT 100", (uid,))
            rows = [_row_json(r) for r in reversed(cur.fetchall())]
        return {"rows": rows}

    @app.get("/api/line/media/{mid}")
    def line_media(request: Request, mid: int):
        """客戶在 LINE 傳來的圖片（需登入；磁碟存放，DB 只有相對路徑）。"""
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT rel_path, mime FROM line_messages WHERE id=%s", (mid,))
            row = cur.fetchone()
        if row is None or not row["rel_path"]:
            raise HTTPException(404, "找不到這個檔案。")
        p = config.abs_path(row["rel_path"])
        if not p.exists():
            raise HTTPException(404, "檔案已不存在。")
        return FileResponse(p, media_type=row["mime"] or "application/octet-stream",
                            content_disposition_type="inline")

    @app.put("/api/line/conversations/{uid}/mode")
    def line_conv_mode(request: Request, uid: str, body: Dict[str, Any] = Body(...)):
        """店員手動接手／交還 AI。接手後 AI 靜音，逾時（config.ai.human_hold_hours）自動復原。"""
        user = _current_user(request)
        _require_menu(user, "customers")
        mode = _clean(body.get("mode"))
        if mode not in ("bot", "human"):
            raise HTTPException(422, "mode 須為 bot 或 human。")
        if mode == "human":
            line_bot.set_human(uid, "staff", user["realname"])
        else:
            line_bot.set_bot(uid)
        return {"ok": True, "mode": mode}

    @app.put("/api/line/drafts/{mid}")
    def line_draft_update(request: Request, mid: int, body: Dict[str, Any] = Body(...)):
        """草稿處理：discarded＝捨棄；sent＝店員已用（可選 push=true 直接由系統送出）。

        ⚠ push=true 走 Messaging API 推播，佔每月免費額度；
          店員在官方帳號後台手動回覆則不計費，故預設只標記不推播。
        """
        user = _current_user(request)
        _require_menu(user, "customers")
        st = _clean(body.get("status"))
        if st not in ("sent", "discarded"):
            raise HTTPException(422, "status 須為 sent 或 discarded。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id, line_user_id, text, source, status "
                        "FROM line_messages WHERE id=%s", (mid,))
            row = cur.fetchone()
            if row is None or row["source"] != "ai_draft":
                raise HTTPException(404, "找不到這則草稿。")
            if row["status"] != "new":
                raise HTTPException(422, "這則草稿已處理過。")
            text = _clean(body.get("text")) or row["text"]      # 店員可改寫後再用
            if st == "sent" and body.get("push"):
                if not line_bot.push(row["line_user_id"], [text]):
                    raise HTTPException(502, "LINE 推播失敗，請改用官方帳號後台回覆。")
                cur.execute("INSERT INTO line_messages (line_user_id, direction, source, text, "
                            "status, meta) VALUES (%s,'out','staff',%s,'sent',%s)",
                            (row["line_user_id"], text, f"由 {user['realname']} 送出"))
            cur.execute("UPDATE line_messages SET status=%s, text=%s WHERE id=%s",
                        (st, text, mid))
        return {"ok": True}

    # ── 客戶 ──────────────────────────────────────────────────
    @app.post("/api/customers/query")
    def query_customers(request: Request, body: Dict[str, Any] = Body(...)):
        _require_menu(_current_user(request), "customers")
        where: List[str] = []
        args: List[Any] = []

        for f in _TEXT_FIELDS:
            v = _clean(body.get(f))
            if v is None:
                continue
            if f == "national_id":
                v = v.upper()
            if f in ("gender", "identity"):   # 代碼欄位用等值比對
                where.append(f"`{f}` = %s")
                args.append(v)
            else:
                where.append(f"`{f}` LIKE %s")
                args.append(f"%{v}%")

        for f in _DATE_FIELDS:
            v = _parse_date(body.get(f))
            if v is not None:
                where.append(f"`{f}` = %s")
                args.append(v)

        # 三態："y"/"n"/其他=不限（同 Blazor 版 ConsentSel/SubsidySel）
        for f in _BOOL_FIELDS:
            v = body.get(f)
            if v == "y":
                where.append(f"`{f}` = 1")
            elif v == "n":
                where.append(f"`{f}` = 0")

        try:
            page = max(1, int(body.get("page") or 1))
        except (TypeError, ValueError):
            page = 1

        cond = (" WHERE " + " AND ".join(where)) if where else ""
        with _conn() as db, db.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM customers{cond}", args)
            total = cur.fetchone()["n"]
            pages = max(1, -(-total // _PAGE_SIZE))  # ceil
            page = min(page, pages)
            cur.execute(f"SELECT * FROM customers{cond} ORDER BY updated_at DESC "
                        f"LIMIT {_PAGE_SIZE} OFFSET {(page - 1) * _PAGE_SIZE}", args)
            rows = [_row_json(r) for r in cur.fetchall()]
        return {"total": total, "page": page, "pages": pages,
                "page_size": _PAGE_SIZE, "rows": rows}

    @app.post("/api/customers/dup")
    def duplicates(request: Request, body: Dict[str, Any] = Body(...)):
        _require_menu(_current_user(request), "customers")
        name = _clean(body.get("name"))
        mobile = _clean(body.get("phone_mobile"))
        nid = _clean(body.get("national_id"))
        if nid:
            nid = nid.upper()
        if not (name or mobile or nid):
            return {"rows": []}

        where, args = [], []
        if name:
            where.append("name = %s")
            args.append(name)
        if mobile:
            where.append("phone_mobile = %s")
            args.append(mobile)
        if nid:
            where.append("national_id = %s")
            args.append(nid)

        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id, customer_code, name, phone_mobile, birth_date "
                        "FROM customers WHERE " + " OR ".join(where) +
                        " ORDER BY updated_at DESC LIMIT 5", args)
            return {"rows": [_row_json(r) for r in cur.fetchall()]}

    def _collect_fields(body: Dict[str, Any]) -> Dict[str, Any]:
        """整理可寫欄位：空字串→NULL、身分證轉大寫、日期驗證、三態布林。"""
        data: Dict[str, Any] = {}
        for f in _TEXT_FIELDS:
            if f in body:
                v = _clean(body.get(f))
                if f == "national_id" and v:
                    v = v.upper()
                if f == "identity" and v and v not in _IDENTITY_KEYS:
                    raise HTTPException(422, f"身份代碼不正確：{v}")
                data[f] = v
        for f in _DATE_FIELDS:
            if f in body:
                data[f] = _parse_date(body.get(f))
        for f in _BOOL_FIELDS:
            if f in body:
                data[f] = 1 if body.get(f) == "y" else 0
        # 選「已簽署」但沒填日期 → 自動帶今天（同 Blazor 版）
        if data.get("consent_signed") == 1 and not data.get("consent_date"):
            data["consent_date"] = date.today().isoformat()
        return data

    def _next_customer_code(cur) -> str:
        prefix = f"C{date.today():%Y%m%d}-"
        cur.execute("SELECT COUNT(*) AS n FROM customers WHERE customer_code LIKE %s",
                    (prefix + "%",))
        return f"{prefix}{cur.fetchone()['n'] + 1:03d}"

    @app.post("/api/customers")
    def create_customer(request: Request, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "customers")
        data = _collect_fields(body)
        if not data.get("name"):
            raise HTTPException(422, "請至少輸入「姓名」後再建檔，其餘欄位可於建檔後補登。")

        data["updated_by"] = user["realname"]
        if not data.get("registration_date"):
            data["registration_date"] = date.today().isoformat()

        with _conn() as db, db.cursor() as cur:
            if not data.get("customer_code"):
                data["customer_code"] = _next_customer_code(cur)
            cols = ", ".join(f"`{k}`" for k in data)
            marks = ", ".join(["%s"] * len(data))
            try:
                cur.execute(f"INSERT INTO customers ({cols}) VALUES ({marks})",
                            list(data.values()))
            except pymysql.err.IntegrityError as e:
                raise _save_error(e)
            return {"id": cur.lastrowid, "customer_code": data["customer_code"]}

    @app.get("/api/customers/{cid}")
    def get_customer(request: Request, cid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT c.*, f.display_name AS line_display_name "
                        "FROM customers c "
                        "LEFT JOIN line_followers f ON f.line_user_id = c.line_user_id "
                        "WHERE c.id=%s", (cid,))
            row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "找不到這筆客戶資料（可能已被移除）。")
        return _row_json(row)

    @app.put("/api/customers/{cid}")
    def update_customer(request: Request, cid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "customers")
        data = _collect_fields(body)
        if not data.get("name"):
            raise HTTPException(422, "姓名不可空白。")
        if not data.get("customer_code"):
            raise HTTPException(422, "客戶編號不可空白。")

        data["updated_by"] = user["realname"]
        sets = ", ".join(f"`{k}`=%s" for k in data)
        with _conn() as db, db.cursor() as cur:
            try:
                cur.execute(f"UPDATE customers SET {sets} WHERE id=%s",
                            list(data.values()) + [cid])
            except pymysql.err.IntegrityError as e:
                raise _save_error(e)
            if cur.rowcount == 0:
                cur.execute("SELECT id FROM customers WHERE id=%s", (cid,))
                if cur.fetchone() is None:
                    raise HTTPException(404, "找不到這筆客戶資料（可能已被移除）。")
            cur.execute("SELECT * FROM customers WHERE id=%s", (cid,))
            return _row_json(cur.fetchone())

    # ── 客戶照片（customer_photos：多張、內容存 DB、可刪除）────
    _PHOTO_MAX = 10 * 1024 * 1024  # 10MB（前端已先縮圖，通常遠小於此）
    _FILE_MAX = 25 * 1024 * 1024   # 25MB（商品附件：說明書/合約等文件可能較大）

    @app.get("/api/customers/{cid}/photos")
    def list_photos(request: Request, cid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            # 排除「原始表單影像」那一份（form_image_id）——它不屬於多檔畫廊
            cur.execute("SELECT id, filename, bytes_size, created_at, created_by "
                        "FROM customer_photos WHERE customer_id=%s "
                        "AND id <> COALESCE((SELECT form_image_id FROM customers WHERE id=%s), 0) "
                        "ORDER BY id", (cid, cid))
            return {"rows": [_row_json(r) for r in cur.fetchall()]}

    @app.post("/api/customers/{cid}/photos")
    async def upload_photo(request: Request, cid: int, filename: str = ""):
        user = _current_user(request)
        _require_menu(user, "customers")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        if not mime.startswith("image/"):
            raise HTTPException(422, "只接受圖片檔案。")
        data = await request.body()
        if not data:
            raise HTTPException(422, "沒有收到檔案內容。")
        if len(data) > _PHOTO_MAX:
            raise HTTPException(413, "圖片超過 10MB，請縮小後再上傳。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id FROM customers WHERE id=%s", (cid,))
            if cur.fetchone() is None:
                raise HTTPException(404, "找不到這筆客戶資料（可能已被移除）。")
            rel = config.save_file("customer_photos", data, mime, cid)
            cur.execute("INSERT INTO customer_photos "
                        "(customer_id, filename, rel_path, mime, bytes_size, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (cid, _clean(filename), rel, mime, len(data), user["realname"]))
            return {"id": cur.lastrowid}

    # ── 原始表單影像（單一份：匯入/拍照的原稿；連結存 customers.form_image_id）──
    #   與客戶照片多檔畫廊不同：每位客戶只保留最後一份，回補/替換時刪掉舊的。
    #   影像本體存磁碟 forms/ 資料夾（DB 不存 BLOB），重用 /api/photos/{id} serving。
    @app.put("/api/customers/{cid}/form-image")
    async def set_form_image(request: Request, cid: int, filename: str = ""):
        user = _current_user(request)
        _require_menu(user, "customers")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        if not mime.startswith("image/"):
            raise HTTPException(422, "只接受圖片檔案。")
        data = await request.body()
        if not data:
            raise HTTPException(422, "沒有收到檔案內容。")
        if len(data) > _PHOTO_MAX:
            raise HTTPException(413, "圖片超過 10MB，請縮小後再上傳。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT form_image_id FROM customers WHERE id=%s", (cid,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "找不到這筆客戶資料（可能已被移除）。")
            old_id = row["form_image_id"]
            rel = config.save_file("form_images", data, mime, cid)
            cur.execute("INSERT INTO customer_photos "
                        "(customer_id, filename, rel_path, mime, bytes_size, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (cid, _clean(filename), rel, mime, len(data), user["realname"]))
            new_id = cur.lastrowid
            cur.execute("UPDATE customers SET form_image_id=%s WHERE id=%s", (new_id, cid))
            if old_id:   # 只留最後一份：刪掉前一份原稿（DB 列 + 磁碟檔）
                cur.execute("SELECT rel_path FROM customer_photos WHERE id=%s", (old_id,))
                orow = cur.fetchone()
                cur.execute("DELETE FROM customer_photos WHERE id=%s", (old_id,))
                if orow:
                    config.delete_file(orow["rel_path"])
            return {"id": new_id}

    @app.get("/api/photos/{pid}")
    def get_photo(request: Request, pid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT mime, rel_path FROM customer_photos WHERE id=%s", (pid,))
            row = cur.fetchone()
        if row is None or not row["rel_path"]:
            raise HTTPException(404, "找不到照片（可能已被刪除）。")
        path = config.abs_path(row["rel_path"])
        if not path.is_file():
            raise HTTPException(404, "照片檔案遺失。")
        return FileResponse(path, media_type=row["mime"],
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/photos/{pid}")
    def delete_photo(request: Request, pid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT rel_path FROM customer_photos WHERE id=%s", (pid,))
            row = cur.fetchone()
            cur.execute("DELETE FROM customer_photos WHERE id=%s", (pid,))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到照片（可能已被刪除）。")
        if row:
            config.delete_file(row["rel_path"])
        return {"ok": True}

    # ── 客戶待辦事項（customer_todos + 附件多檔存磁碟 todos/）──────────
    _TODO_PRIORITIES = ("high", "normal", "low")

    def _todo_with_attachments(cur, tid: int) -> Dict[str, Any]:
        cur.execute("SELECT * FROM customer_todos WHERE id=%s", (tid,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "找不到這筆待辦（可能已被刪除）。")
        out = _row_json(row)
        cur.execute("SELECT id, filename, mime, bytes_size, created_at, created_by "
                    "FROM customer_todo_attachments WHERE todo_id=%s ORDER BY id", (tid,))
        out["attachments"] = [_row_json(a) for a in cur.fetchall()]
        return out

    @app.get("/api/customers/{cid}/todos")
    def list_todos(request: Request, cid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT * FROM customer_todos WHERE customer_id=%s "
                        "ORDER BY created_at DESC, id DESC", (cid,))
            todos = [_row_json(r) for r in cur.fetchall()]
            ids = [t["id"] for t in todos]
            atts: Dict[int, List[Dict[str, Any]]] = {}
            if ids:
                marks = ",".join(["%s"] * len(ids))
                cur.execute("SELECT id, todo_id, filename, mime, bytes_size, created_at, created_by "
                            f"FROM customer_todo_attachments WHERE todo_id IN ({marks}) ORDER BY id", ids)
                for a in cur.fetchall():
                    atts.setdefault(a["todo_id"], []).append(_row_json(a))
            for t in todos:
                t["attachments"] = atts.get(t["id"], [])
            return {"rows": todos}

    def _collect_todo(body: Dict[str, Any]) -> Dict[str, Any]:
        desc = _clean(body.get("description"))
        if not desc:
            raise HTTPException(422, "請填寫待辦說明。")
        cat = _clean(body.get("category"))
        pri = _clean(body.get("priority"))
        if pri and pri not in _TODO_PRIORITIES:
            raise HTTPException(422, f"優先度不正確：{pri}")
        return {"category": cat, "description": desc, "priority": pri,
                "due_at": _parse_dt(body.get("due_at"))}

    @app.post("/api/customers/{cid}/todos")
    def create_todo(request: Request, cid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "customers")
        data = _collect_todo(body)
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id FROM customers WHERE id=%s", (cid,))
            if cur.fetchone() is None:
                raise HTTPException(404, "找不到這筆客戶資料（可能已被移除）。")
            cur.execute(
                "INSERT INTO customer_todos "
                "(customer_id, category, description, priority, due_at, created_by, updated_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (cid, data["category"], data["description"], data["priority"],
                 data["due_at"], user["realname"], user["realname"]))
            return _todo_with_attachments(cur, cur.lastrowid)

    @app.put("/api/todos/{tid}")
    def update_todo(request: Request, tid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id FROM customer_todos WHERE id=%s", (tid,))
            if cur.fetchone() is None:
                raise HTTPException(404, "找不到這筆待辦（可能已被刪除）。")
            sets: List[str] = []
            args: List[Any] = []
            if "done" in body:   # 一鍵完成/取消完成
                if body.get("done"):
                    sets += ["done_at=IFNULL(done_at, NOW())", "done_by=%s"]
                    args += [user["realname"]]
                else:
                    sets += ["done_at=NULL", "done_by=NULL"]
            # 只更新有提供的欄位（部分更新，不強制 description）
            if "description" in body:
                desc = _clean(body.get("description"))
                if not desc:
                    raise HTTPException(422, "待辦說明不可空白。")
                sets.append("description=%s"); args.append(desc)
            if "category" in body:
                sets.append("category=%s"); args.append(_clean(body.get("category")))
            if "priority" in body:
                pri = _clean(body.get("priority"))
                if pri and pri not in _TODO_PRIORITIES:
                    raise HTTPException(422, f"優先度不正確：{pri}")
                sets.append("priority=%s"); args.append(pri)
            if "due_at" in body:
                sets.append("due_at=%s"); args.append(_parse_dt(body.get("due_at")))
            if not sets:
                raise HTTPException(422, "沒有可更新的欄位。")
            sets += ["updated_by=%s"]
            args += [user["realname"], tid]
            cur.execute(f"UPDATE customer_todos SET {', '.join(sets)} WHERE id=%s", args)
            return _todo_with_attachments(cur, tid)

    @app.delete("/api/todos/{tid}")
    def delete_todo(request: Request, tid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id FROM customer_todos WHERE id=%s", (tid,))
            if cur.fetchone() is None:
                raise HTTPException(404, "找不到這筆待辦（可能已被刪除）。")
            cur.execute("SELECT rel_path FROM customer_todo_attachments WHERE todo_id=%s", (tid,))
            rels = [r["rel_path"] for r in cur.fetchall()]
            cur.execute("DELETE FROM customer_todo_attachments WHERE todo_id=%s", (tid,))
            cur.execute("DELETE FROM customer_todos WHERE id=%s", (tid,))
        for rel in rels:
            config.delete_file(rel)
        return {"ok": True}

    @app.post("/api/todos/{tid}/attachments")
    async def upload_todo_attachment(request: Request, tid: int, filename: str = ""):
        user = _current_user(request)
        _require_menu(user, "customers")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        if not (mime.startswith("image/") or mime == "application/pdf"):
            raise HTTPException(422, "只接受圖片或 PDF 檔案。")
        data = await request.body()
        if not data:
            raise HTTPException(422, "沒有收到檔案內容。")
        if len(data) > _PHOTO_MAX:
            raise HTTPException(413, "檔案超過 10MB，請縮小後再上傳。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT customer_id FROM customer_todos WHERE id=%s", (tid,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "找不到這筆待辦（可能已被刪除）。")
            rel = config.save_file("todo_attachments", data, mime, row["customer_id"], tid)
            cur.execute("INSERT INTO customer_todo_attachments "
                        "(todo_id, rel_path, filename, mime, bytes_size, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (tid, rel, _clean(filename), mime, len(data), user["realname"]))
            return {"id": cur.lastrowid}

    @app.get("/api/todo-attachments/{aid}")
    def get_todo_attachment(request: Request, aid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT mime, rel_path FROM customer_todo_attachments WHERE id=%s", (aid,))
            row = cur.fetchone()
        if row is None or not row["rel_path"]:
            raise HTTPException(404, "找不到附件（可能已被刪除）。")
        path = config.abs_path(row["rel_path"])
        if not path.is_file():
            raise HTTPException(404, "附件檔案遺失。")
        return FileResponse(path, media_type=row["mime"],
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/todo-attachments/{aid}")
    def delete_todo_attachment(request: Request, aid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT rel_path FROM customer_todo_attachments WHERE id=%s", (aid,))
            row = cur.fetchone()
            cur.execute("DELETE FROM customer_todo_attachments WHERE id=%s", (aid,))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到附件（可能已被刪除）。")
        if row:
            config.delete_file(row["rel_path"])
        return {"ok": True}

    # ── 紙本表單 OCR（本機 ollama qwen3-vl；個資不出區網）───────
    _OLLAMA_URL = config.OCR["ollama_url"]
    # 2026-07-13：改用 Instruct 變體。實測 Thinking 版做 OCR 會生成 ~2800 思考 token（113s/張），
    #   Instruct 同圖只 ~250 token（13.5s/張，快 ~8.4×），JSON 準確度相當。think 參數關不掉
    #   thinking-parser 的生成量，必須換模型。速度已非瓶頸→前端影像調回 1600 拚準確度。
    _OCR_MODEL = config.OCR["model"]
    _OCR_PROMPT = """這是一張手寫的「客戶資料表」照片。請仔細辨識，只輸出一個 JSON 物件（不要其他文字）。
所有中文一律使用台灣正體（繁體）中文，禁止簡體字。
空白或無法辨識的欄位填 null；手寫潦草處用你最好的判斷。
凡是筆跡潦草、有多種可能讀法的欄位（特別是姓名、電話、編號、地址的字），務必把欄位名列入 uncertain 陣列——寧可多列不要漏列。欄位：
customer_code(客戶編號), registration_date(建檔日期，表上為民國年，輸出如 114/11/13),
name(姓名), gender("M"或"F"), national_id(身分證字號),
birth_date(出生年月日，表上為西元，輸出如 1935-06-02),
phone_home(通訊電話市話), phone_mobile(通訊電話個人手機), email(電子信箱),
contact_name(聯絡人姓名), contact_phone(聯絡人電話；若寫「同上」就輸出字串"同上"),
mailing_city(通訊地址縣市), mailing_district(鄉鎮市區), mailing_village(村里),
mailing_detail(路街段巷弄號樓), household_city(戶籍地址縣市), household_district,
household_village, household_detail,
subsidy_applied(申請補助：勾「有」輸出"y"、勾「沒有」輸出"n"、未勾 null),
uncertain(沒把握的欄位名陣列)"""

    # OCR 回傳中的日期欄位與其紀年：民國(roc) / 西元(ad)。輸出一律轉西元 ISO（系統慣例）
    _OCR_DATE_FIELDS = {"registration_date": "roc", "birth_date": "ad"}

    def _ocr_parse_date(v: Any, era: str) -> Optional[str]:
        m = re.match(r"^\s*(\d{1,4})[/\-.年]\s*(\d{1,2})[/\-.月]\s*(\d{1,2})日?\s*$", str(v or ""))
        if not m:
            return None
        y, mo, dd = int(m[1]), int(m[2]), int(m[3])
        if era == "roc" and y < 1000:
            y += 1911
        try:
            return date(y, mo, dd).isoformat()
        except ValueError:
            return None

    def _call_ocr(image_b64: str, mime: str) -> Dict[str, Any]:
        payload = json.dumps({
            "model": _OCR_MODEL, "prompt": _OCR_PROMPT, "images": [image_b64],
            "stream": False, "keep_alive": -1,
            "think": False,   # qwen3-vl 是 thinking 模型：OCR 只要 JSON，關掉 CoT 省下大量生成 token（實測延遲主因）
            "options": {"temperature": 0, "num_ctx": 8192},
        }).encode()
        req = urllib.request.Request(f"{_OLLAMA_URL}/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as res:
                r = json.loads(res.read())
        except urllib.error.URLError as e:
            raise HTTPException(503, f"本機辨識服務未回應（GPU 可能在休眠時段）：{e.reason}")
        text = (r.get("response") or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)   # 去掉 markdown 圍欄
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise HTTPException(502, "辨識結果無法解析，請重拍或改為手動輸入。")
        try:
            return json.loads(m.group(0))
        except ValueError:
            raise HTTPException(502, "辨識結果不是有效格式，請重拍或改為手動輸入。")

    @app.post("/api/customers/ocr")
    async def ocr_customer(request: Request):
        user = _current_user(request)
        _require_menu(user, "customers")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        if not mime.startswith("image/"):
            raise HTTPException(422, "只接受圖片檔案。")
        data = await request.body()
        if not data:
            raise HTTPException(422, "沒有收到檔案內容。")
        if len(data) > _PHOTO_MAX:
            raise HTTPException(413, "圖片超過 10MB，請縮小後再上傳。")

        # 阻塞的推論丟到執行緒，避免卡住整個服務（一張約 1 分鐘）
        raw = await asyncio.to_thread(_call_ocr, base64.b64encode(data).decode(), mime)

        uncertain = [str(x) for x in raw.get("uncertain") or [] if isinstance(x, str)]
        fields: Dict[str, Any] = {}
        for f in _TEXT_FIELDS:            # 只收白名單欄位；identity 等紙本沒有的自然略過
            v = raw.get(f)
            if v is None or f == "notes":
                continue
            v = str(v).strip()
            if not v or v.lower() == "null":
                continue
            fields[f] = v
        for f, era in _OCR_DATE_FIELDS.items():
            if raw.get(f):
                iso = _ocr_parse_date(raw.get(f), era)
                if iso:
                    fields[f] = iso
                elif f not in uncertain:
                    uncertain.append(f)      # 日期格式解析失敗 → 標記請人工確認
        g = str(raw.get("gender") or "").strip().upper()
        if g in ("M", "F"):
            fields["gender"] = g
        else:
            fields.pop("gender", None)      # 非 M/F 一律丟棄，避免污染 enum 欄位
        s = str(raw.get("subsidy_applied") or "").strip().lower()
        if s in ("y", "n"):
            fields["subsidy_applied"] = s
        # 「同上」→ 帶入市話或手機
        if str(fields.get("contact_phone") or "").strip() in ("同上", "同 上"):
            fields["contact_phone"] = fields.get("phone_mobile") or fields.get("phone_home") or ""
        if fields.get("national_id"):
            fields["national_id"] = fields["national_id"].upper()

        return {"fields": fields,
                "uncertain": [u for u in uncertain if u in fields or u in _OCR_DATE_FIELDS]}

    # ── OCR 模型載入狀態 ────────────────────────────────────────
    # 2026-07-12：移除舊「PCI bind/unbind 省電」機制（每日輪替 unbind 卡導致
    #   Tesla V100 Xid 79「掉出匯流排」故障）。GPU 現由 docker-compose 固定綁定
    #   單張健康卡常駐、模型 keep_alive=-1 常駐，warmup 只負責把模型拉進 VRAM，
    #   不再碰 PCI；sleep 端點保留但不卸載（避免頻繁載入/卸載）。
    _ocr_warm = {"state": "idle", "since": 0.0, "error": None}  # idle|loading|ready|error
    _ocr_lock = threading.Lock()

    def _model_loaded() -> bool:
        try:
            with urllib.request.urlopen(f"{_OLLAMA_URL}/api/ps", timeout=3) as res:
                ps = json.loads(res.read())
            return any(m.get("model", "").startswith(_OCR_MODEL.split(":")[0])
                       for m in ps.get("models") or [])
        except Exception:
            return False

    def _load_model() -> None:
        """呼叫 ollama 把 OCR 模型拉進 VRAM 常駐（keep_alive=-1），不動 PCI。"""
        try:
            body = json.dumps({"model": _OCR_MODEL, "keep_alive": -1}).encode()
            req = urllib.request.Request(
                f"{_OLLAMA_URL}/api/generate", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600):
                pass
            with _ocr_lock:
                _ocr_warm["state"] = "ready"
                _ocr_warm["error"] = None
        except Exception as e:  # noqa: BLE001
            with _ocr_lock:
                _ocr_warm["state"] = "error"
                _ocr_warm["error"] = str(e)

    @app.post("/api/ocr/warmup")
    def ocr_warmup(request: Request):
        user = _current_user(request)
        _require_menu(user, "customers")
        with _ocr_lock:
            if _ocr_warm["state"] == "loading":
                return {"state": "loading"}
            if _model_loaded():
                _ocr_warm["state"] = "ready"
                return {"state": "ready"}
            _ocr_warm.update(state="loading", since=time.time(), error=None)
        threading.Thread(target=_load_model, daemon=True).start()
        return {"state": "loading"}

    @app.get("/api/ocr/status")
    def ocr_status(request: Request):
        _current_user(request)
        with _ocr_lock:
            st = dict(_ocr_warm)
        # 以 ollama 實況為準：模型在就是 ready（可跨重啟/多分頁同步）
        if _model_loaded():
            if st["state"] != "ready":
                with _ocr_lock:
                    _ocr_warm["state"] = "ready"
            return {"state": "ready"}
        if st["state"] == "loading":
            return {"state": "loading", "elapsed": int(time.time() - st["since"])}
        if st["state"] == "error":
            return {"state": "error", "error": st["error"]}
        return {"state": "idle"}

    @app.post("/api/ocr/sleep")
    def ocr_sleep(request: Request):
        # 保留端點供前端相容；不再 unbind 卡或卸載模型（GPU 常駐）。
        user = _current_user(request)
        _require_menu(user, "customers")
        with _ocr_lock:
            _ocr_warm.update(state="idle", error=None)
        return {"state": "idle"}

    # ── 代碼表（下拉選單選項；登入即可讀）──────────────────────
    @app.get("/api/codes")
    def list_codes(request: Request):
        _current_user(request)
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT category, `key`, value FROM codes ORDER BY category, id")
            out: Dict[str, List[Dict[str, str]]] = {}
            for r in cur.fetchall():
                out.setdefault(r["category"], []).append({"key": r["key"], "value": r["value"]})
            return out

    # ── 商品管理（選單權限 products：admin/manager）────────────
    def _collect_product(body: Dict[str, Any]) -> Dict[str, Any]:
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT category, `key` FROM codes")
            valid: Dict[str, set] = {}
            for r in cur.fetchall():
                valid.setdefault(r["category"], set()).add(r["key"])

        data: Dict[str, Any] = {}
        for f in _PRODUCT_TEXT:
            if f not in body:
                continue
            v = _clean(body.get(f))
            if v and f in _PRODUCT_CODED and v not in valid.get(_PRODUCT_CODED[f], set()):
                raise HTTPException(422, f"「{f}」的值不在代碼表中：{v}")
            if f == "ha_fit_levels" and v:
                bad = [k for k in v.split(",") if k not in valid.get("identity", set())]
                if bad:
                    raise HTTPException(422, f"適配聽損程度代碼不正確：{','.join(bad)}")
            data[f] = v
        for f in _PRODUCT_INT:
            if f in body:
                v = _clean(str(body.get(f) or ""))
                if v is None:
                    data[f] = None
                else:
                    try:
                        data[f] = max(0, min(int(v), 999))
                    except ValueError:
                        raise HTTPException(422, f"「{f}」須為整數：{v}")
        for f in _PRODUCT_DEC:
            if f in body:
                v = _clean(str(body.get(f) or ""))
                if v is None:
                    data[f] = None
                else:
                    try:
                        data[f] = round(float(v), 2)
                        if data[f] < 0:
                            raise ValueError
                    except ValueError:
                        raise HTTPException(422, f"「{f}」須為不小於 0 的數字：{v}")
        for f in _PRODUCT_BOOL:
            if f in body:
                v = body.get(f)
                data[f] = None if v in ("", None) else (1 if v == "y" else 0)
        if "is_active" in body:
            data["is_active"] = 1 if str(body.get("is_active")) != "0" else 0
        return data

    @app.get("/api/products")
    def list_products(request: Request):
        user = _current_user(request)
        # 交易建檔需要挑商品，故 products 或 purchases 任一權限皆可讀清單（價格仍受遮蔽）
        menus = _menus_for(user["level"])
        if "products" not in menus and "purchases" not in menus:
            raise HTTPException(403, "您的帳號沒有使用此功能的權限。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT * FROM products "
                        "ORDER BY is_active DESC, brand, model_name")
            return {"rows": [_mask_row("products", _row_json(r), user) for r in cur.fetchall()]}

    @app.get("/api/products/brands")
    def list_product_brands(request: Request):
        """品牌／供應商建議清單（datalist 用）：取自現有商品的相異值，
        永遠反映真實資料。可輸入不受限，這裡只提供收斂用的建議。"""
        _require_menu(_current_user(request), "products")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT DISTINCT brand FROM products "
                        "WHERE brand IS NOT NULL AND brand<>'' ORDER BY brand")
            brands = [r["brand"] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT supplier FROM products "
                        "WHERE supplier IS NOT NULL AND supplier<>'' ORDER BY supplier")
            suppliers = [r["supplier"] for r in cur.fetchall()]
        return {"brands": brands, "suppliers": suppliers}

    @app.post("/api/products")
    def create_product(request: Request, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "products")
        data = _collect_product(body)
        if not data.get("brand") or not data.get("model_name") or not data.get("category"):
            raise HTTPException(422, "「分類」「品牌」「型號」為必填。")
        if not _can(user, "view_price"):          # 防越權：無價格權限者不得寫價格
            for f in _PRODUCT_DEC:
                data.pop(f, None)
        data["updated_by"] = user["realname"]
        cols = ", ".join(f"`{k}`" for k in data)
        marks = ", ".join(["%s"] * len(data))
        with _conn() as db, db.cursor() as cur:
            try:
                cur.execute(f"INSERT INTO products ({cols}) VALUES ({marks})", list(data.values()))
            except pymysql.err.IntegrityError as e:
                raise _save_error(e)
            return {"id": cur.lastrowid}

    @app.put("/api/products/{pid}")
    def update_product(request: Request, pid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "products")
        data = _collect_product(body)
        if "brand" in data and not data["brand"]:
            raise HTTPException(422, "品牌不可空白。")
        if "model_name" in data and not data["model_name"]:
            raise HTTPException(422, "型號不可空白。")
        if not _can(user, "view_price"):
            for f in _PRODUCT_DEC:
                data.pop(f, None)
        if not data:
            raise HTTPException(422, "沒有可更新的欄位。")
        data["updated_by"] = user["realname"]
        sets = ", ".join(f"`{k}`=%s" for k in data)
        with _conn() as db, db.cursor() as cur:
            try:
                cur.execute(f"UPDATE products SET {sets} WHERE id=%s", list(data.values()) + [pid])
            except pymysql.err.IntegrityError as e:
                raise _save_error(e)
            cur.execute("SELECT * FROM products WHERE id=%s", (pid,))
            row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "找不到這筆商品（可能已被刪除）。")
        return _mask_row("products", _row_json(row), user)

    @app.delete("/api/products/{pid}")
    def delete_product(request: Request, pid: int):
        user = _current_user(request)
        _require_menu(user, "products")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT rel_path FROM product_attachments WHERE product_id=%s", (pid,))
            rels = [r["rel_path"] for r in cur.fetchall()]
            try:
                cur.execute("DELETE FROM products WHERE id=%s", (pid,))
            except pymysql.err.IntegrityError:
                raise HTTPException(409, "此商品已有交易紀錄，不能刪除；請改為「停售」。")
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到這筆商品（可能已被刪除）。")
            cur.execute("DELETE FROM product_attachments WHERE product_id=%s", (pid,))
        for rel in rels:       # 商品刪除成功才連同附件檔一併清掉
            config.delete_file(rel)
        return {"ok": True}

    # ── 商品附件（product_attachments：多檔，任意類型；圖片可預覽）──
    #   說明書/合約/圖片等落磁碟 products/{pid}/；DB 只存相對路徑（不存 BLOB）。
    @app.get("/api/products/{pid}/attachments")
    def list_product_attachments(request: Request, pid: int):
        _require_menu(_current_user(request), "products")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id, filename, mime, bytes_size, created_at, created_by "
                        "FROM product_attachments WHERE product_id=%s ORDER BY id", (pid,))
            return {"rows": [_row_json(r) for r in cur.fetchall()]}

    @app.post("/api/products/{pid}/attachments")
    async def upload_product_attachment(request: Request, pid: int, filename: str = ""):
        user = _current_user(request)
        _require_menu(user, "products")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip() \
            or "application/octet-stream"
        data = await request.body()
        if not data:
            raise HTTPException(422, "沒有收到檔案內容。")
        if len(data) > _FILE_MAX:
            raise HTTPException(413, "檔案超過 25MB，請壓縮後再上傳。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id FROM products WHERE id=%s", (pid,))
            if cur.fetchone() is None:
                raise HTTPException(404, "找不到這筆商品（可能已被刪除）。")
            rel = config.save_file("product_attachments", data, mime, pid,
                                   orig_name=_clean(filename))
            cur.execute("INSERT INTO product_attachments "
                        "(product_id, rel_path, filename, mime, bytes_size, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (pid, rel, _clean(filename), mime, len(data), user["realname"]))
            return {"id": cur.lastrowid}

    @app.get("/api/product-attachments/{aid}")
    def get_product_attachment(request: Request, aid: int):
        _require_menu(_current_user(request), "products")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT mime, rel_path, filename FROM product_attachments WHERE id=%s", (aid,))
            row = cur.fetchone()
        if row is None or not row["rel_path"]:
            raise HTTPException(404, "找不到附件（可能已被刪除）。")
        path = config.abs_path(row["rel_path"])
        if not path.is_file():
            raise HTTPException(404, "附件檔案遺失。")
        # inline：圖片/PDF 直接預覽；其他類型瀏覽器會以原檔名下載
        return FileResponse(path, media_type=row["mime"] or "application/octet-stream",
                            filename=row["filename"] or path.name,
                            content_disposition_type="inline",
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/product-attachments/{aid}")
    def delete_product_attachment(request: Request, aid: int):
        _require_menu(_current_user(request), "products")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT rel_path FROM product_attachments WHERE id=%s", (aid,))
            row = cur.fetchone()
            cur.execute("DELETE FROM product_attachments WHERE id=%s", (aid,))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到附件（可能已被刪除）。")
        if row:
            config.delete_file(row["rel_path"])
        return {"ok": True}

    # ── 交易紀錄（purchases：選單權限 purchases＝admin/manager/sales）──────
    #   每筆綁一位客戶（必填）、可綁一項商品；unit_price 走價格權限遮蔽。
    #   兩種入口共用同一組 API：獨立頁列全部、客戶明細帶 ?customer_id= 只列該客戶。
    def _collect_purchase(body: Dict[str, Any]) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for f in _PURCHASE_TEXT:
            if f in body:
                data[f] = _clean(body.get(f))
        for f in _PURCHASE_DATE:
            if f in body:
                data[f] = _parse_date(body.get(f))
        for f, allowed in _PURCHASE_ENUMS.items():
            if f in body:
                v = _clean(body.get(f))
                if v is not None and v not in allowed:
                    raise HTTPException(422, f"「{f}」的值不正確：{v}")
                # transaction_type/payment_status 有 NOT NULL 預設，空值時交給 DB 預設
                if v is not None:
                    data[f] = v
        if "product_id" in body:
            v = _clean(str(body.get("product_id") or ""))
            data["product_id"] = int(v) if v else None
        if "quantity" in body:
            v = _clean(str(body.get("quantity") or ""))
            try:
                data["quantity"] = max(1, min(int(v), 9999)) if v else 1
            except ValueError:
                raise HTTPException(422, f"「數量」須為整數：{v}")
        if "unit_price" in body:
            v = _clean(str(body.get("unit_price") or ""))
            if v is None:
                data["unit_price"] = None
            else:
                try:
                    data["unit_price"] = round(float(v), 2)
                    if data["unit_price"] < 0:
                        raise ValueError
                except ValueError:
                    raise HTTPException(422, f"「單價」須為不小於 0 的數字：{v}")
        return data

    @app.get("/api/purchases")
    def list_purchases(request: Request, customer_id: Optional[int] = None):
        user = _current_user(request)
        _require_menu(user, "purchases")
        where, args = [], []
        if customer_id is not None:
            where.append("p.customer_id = %s")
            args.append(customer_id)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        with _conn() as db, db.cursor() as cur:
            cur.execute(
                "SELECT p.*, c.name AS customer_name, c.customer_code, "
                "       pr.brand, pr.model_name, pr.category "
                "FROM purchases p "
                "JOIN customers c ON c.id = p.customer_id "
                "LEFT JOIN products pr ON pr.id = p.product_id "
                f"{cond} ORDER BY p.transaction_date DESC, p.id DESC", args)
            rows = [_mask_row("purchases", _row_json(r), user) for r in cur.fetchall()]
        return {"rows": rows}

    @app.post("/api/purchases")
    def create_purchase(request: Request, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "purchases")
        cid = _clean(str(body.get("customer_id") or ""))
        if not cid:
            raise HTTPException(422, "請先選擇客戶。")
        if not _clean(body.get("transaction_date")):
            raise HTTPException(422, "「交易日期」為必填。")
        data = _collect_purchase(body)
        data["customer_id"] = int(cid)
        if not _can(user, "view_price"):          # 防越權：無價格權限者不得寫價格
            data.pop("unit_price", None)
        cols = ", ".join(f"`{k}`" for k in data)
        marks = ", ".join(["%s"] * len(data))
        with _conn() as db, db.cursor() as cur:
            try:
                cur.execute(f"INSERT INTO purchases ({cols}) VALUES ({marks})", list(data.values()))
            except pymysql.err.IntegrityError:
                raise HTTPException(422, "客戶或商品不存在（可能已被刪除）。")
            return {"id": cur.lastrowid}

    @app.put("/api/purchases/{pid}")
    def update_purchase(request: Request, pid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "purchases")
        data = _collect_purchase(body)
        if not _can(user, "view_price"):
            data.pop("unit_price", None)
        if not data:
            raise HTTPException(422, "沒有可更新的欄位。")
        sets = ", ".join(f"`{k}`=%s" for k in data)
        with _conn() as db, db.cursor() as cur:
            try:
                cur.execute(f"UPDATE purchases SET {sets} WHERE id=%s",
                            list(data.values()) + [pid])
            except pymysql.err.IntegrityError:
                raise HTTPException(422, "商品不存在（可能已被刪除）。")
            if cur.rowcount == 0:
                cur.execute("SELECT id FROM purchases WHERE id=%s", (pid,))
                if cur.fetchone() is None:
                    raise HTTPException(404, "找不到這筆交易（可能已被刪除）。")
        return {"ok": True}

    @app.delete("/api/purchases/{pid}")
    def delete_purchase(request: Request, pid: int):
        user = _current_user(request)
        _require_menu(user, "purchases")
        with _conn() as db, db.cursor() as cur:
            try:
                cur.execute("DELETE FROM purchases WHERE id=%s", (pid,))
            except pymysql.err.IntegrityError:
                raise HTTPException(409, "此交易已有關聯的提醒紀錄，無法刪除。")
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到這筆交易（可能已被刪除）。")
        return {"ok": True}

    # ── 店面狀態看板（免登入電視牆）────────────────────────────
    #   安全：URL 金鑰（config [board].key，空＝停用）＋全欄位個資遮罩＋唯讀。
    #   一次聚合回傳，前端每 30 秒輪詢；所有字串皆為顯示就緒（含民國日期）。
    _RTYPE = {"follow_up": "回訪", "maintenance": "保養",
              "battery_replacement": "電池", "tuning": "調機", "other": "其他"}

    @app.get("/api/board")
    def board_feed(k: str = ""):
        key = (config.BOARD.get("key") or "").strip()
        if not key or not secrets.compare_digest(k, key):
            raise HTTPException(403, "看板金鑰不正確。")
        today = date.today()
        tomorrow = today + timedelta(days=1)
        wd = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

        def roc(d: date) -> str:
            return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"

        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT `key`,`value` FROM codes WHERE category='todo_category'")
            tcat = {r["key"]: r["value"] for r in cur.fetchall()}

            # 今日行程：回訪提醒＋今日待辦＋門市事件＋LINE 預約（全天在前、再依時間）
            sched: List[Dict[str, Any]] = []
            cur.execute("SELECT r.reminder_type, r.status, c.name FROM follow_up_reminders r "
                        "JOIN customers c ON c.id=r.customer_id "
                        "WHERE r.scheduled_date=%s AND r.status IN ('pending','completed')", (today,))
            for r in cur.fetchall():
                sched.append({"time": "", "tag": _RTYPE.get(r["reminder_type"], "提醒"),
                              "text": _mask_name(r["name"]),
                              "done": r["status"] == "completed", "new": False})
            cur.execute("SELECT t.description, t.due_at, t.done_at, t.category, c.name "
                        "FROM customer_todos t JOIN customers c ON c.id=t.customer_id "
                        "WHERE t.due_at>=%s AND t.due_at<%s", (today, tomorrow))
            for r in cur.fetchall():
                sched.append({"time": r["due_at"].strftime("%H:%M"),
                              "tag": tcat.get(r["category"], "待辦"),
                              "text": f'{_mask_name(r["name"])}　{(r["description"] or "")[:20]}',
                              "done": r["done_at"] is not None, "new": False})
            cur.execute("SELECT title, start_at, all_day FROM calendar_events "
                        "WHERE scope='store' AND category<>'notice' "
                        "AND start_at>=%s AND start_at<%s", (today, tomorrow))
            for r in cur.fetchall():
                sched.append({"time": "" if r["all_day"] else r["start_at"].strftime("%H:%M"),
                              "tag": "門市", "text": (r["title"] or "")[:24],
                              "done": False, "new": False})
            cur.execute("SELECT contact_name, req_hour, status FROM booking_requests "
                        "WHERE req_date=%s AND status<>'cancelled'", (today,))
            for r in cur.fetchall():
                sched.append({"time": f'{r["req_hour"]:02d}:00', "tag": "預約",
                              "text": _mask_name(r["contact_name"]),
                              "done": r["status"] == "handled", "new": r["status"] == "new"})
            sched.sort(key=lambda x: (x["time"] != "", x["time"]))

            # LINE 新進預約（未處理；跨日期、新到舊）
            cur.execute("SELECT contact_name, phone, req_date, req_hour, note, created_at "
                        "FROM booking_requests WHERE status='new' "
                        "ORDER BY created_at DESC LIMIT 12")
            line_new = [{"name": _mask_name(r["contact_name"]), "phone": _mask_phone(r["phone"]),
                         "when": f'{roc(r["req_date"])}（{wd[r["req_date"].weekday()]}）'
                                 f'{r["req_hour"]:02d}:00',
                         "note": (r["note"] or "")[:30],
                         "created": r["created_at"].strftime("%m/%d %H:%M")}
                        for r in cur.fetchall()]

            # 壽星：今日起 7 天內（含跨月/跨年）
            mds = [((today + timedelta(days=i)).month, (today + timedelta(days=i)).day, i)
                   for i in range(7)]
            md_in = ", ".join(f"({m},{d})" for m, d, _ in mds)
            cur.execute("SELECT name, birth_date FROM customers WHERE birth_date IS NOT NULL "
                        f"AND (MONTH(birth_date), DAY(birth_date)) IN ({md_in})")
            off = {(m, d): i for m, d, i in mds}
            bs = []
            for r in cur.fetchall():
                i = off.get((r["birth_date"].month, r["birth_date"].day))
                if i is None:
                    continue
                d2 = today + timedelta(days=i)
                bs.append({"name": _mask_name(r["name"]), "_i": i,
                           "label": "今天" if i == 0 else
                                    ("明天" if i == 1 else f"{wd[d2.weekday()]} {d2.month}/{d2.day}")})
            bs.sort(key=lambda x: x.pop("_i"))
            birthdays = bs[:12]

            # 保固 30 天內到期
            cur.execute("SELECT c.name, pr.brand, pr.model_name, p.warranty_end_date "
                        "FROM purchases p JOIN customers c ON c.id=p.customer_id "
                        "LEFT JOIN products pr ON pr.id=p.product_id "
                        "WHERE p.warranty_end_date BETWEEN %s AND %s "
                        "ORDER BY p.warranty_end_date LIMIT 12",
                        (today, today + timedelta(days=30)))
            warranty = [{"name": _mask_name(r["name"]),
                         "product": f'{r["brand"] or ""} {r["model_name"] or ""}'.strip() or "—",
                         "end": roc(r["warranty_end_date"]),
                         "days": (r["warranty_end_date"] - today).days}
                        for r in cur.fetchall()]

            # 今日待辦＋逾期（未完成、到期時間已到今日之內）
            cur.execute("SELECT t.description, t.due_at, t.category, c.name "
                        "FROM customer_todos t JOIN customers c ON c.id=t.customer_id "
                        "WHERE t.done_at IS NULL AND t.due_at IS NOT NULL AND t.due_at<%s "
                        "ORDER BY t.due_at LIMIT 12", (tomorrow,))
            todos = [{"name": _mask_name(r["name"]), "cat": tcat.get(r["category"], "其他"),
                      "due": r["due_at"].strftime("%m/%d %H:%M"),
                      "overdue": r["due_at"].date() < today,
                      "text": (r["description"] or "")[:24]} for r in cur.fetchall()]

            # 公告跑馬燈：今日仍有效的「公告」門市事件
            cur.execute("SELECT title FROM calendar_events WHERE category='notice' AND scope='store' "
                        "AND DATE(start_at)<=%s AND (end_at IS NULL OR DATE(end_at)>=%s) "
                        "ORDER BY start_at", (today, today))
            announcements = [r["title"] for r in cur.fetchall() if r["title"]]

            # 當月密度（迷你月曆的每日筆數）
            first = today.replace(day=1)
            nxt = (first + timedelta(days=32)).replace(day=1)
            counts: Dict[str, int] = {}

            def bump(d):
                counts[d.isoformat()] = counts.get(d.isoformat(), 0) + 1
            cur.execute("SELECT scheduled_date d FROM follow_up_reminders "
                        "WHERE scheduled_date>=%s AND scheduled_date<%s AND status='pending'",
                        (first, nxt))
            for r in cur.fetchall():
                bump(r["d"])
            cur.execute("SELECT DATE(start_at) d FROM calendar_events WHERE scope='store' "
                        "AND category<>'notice' AND start_at>=%s AND start_at<%s", (first, nxt))
            for r in cur.fetchall():
                bump(r["d"])
            cur.execute("SELECT req_date d FROM booking_requests "
                        "WHERE req_date>=%s AND req_date<%s AND status<>'cancelled'", (first, nxt))
            for r in cur.fetchall():
                bump(r["d"])
            cur.execute("SELECT DATE(due_at) d FROM customer_todos WHERE done_at IS NULL "
                        "AND due_at>=%s AND due_at<%s", (first, nxt))
            for r in cur.fetchall():
                bump(r["d"])

        return {"today": {"iso": today.isoformat(), "roc": roc(today),
                          "weekday": wd[today.weekday()]},
                "announcements": announcements, "schedule": sched, "line_new": line_new,
                "birthdays": birthdays, "warranty": warranty, "todos": todos,
                "month": {"year": today.year, "month": today.month, "counts": counts}}

    return app


app = create_app()
