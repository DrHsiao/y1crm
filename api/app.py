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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import bcrypt
import pymysql
import pymysql.cursors
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from api import (article_import, care_reminders, dbpool, fb_drafts, fb_page, hearing, line_bot, motion_video, noah,
                 post_art, post_card, review_art, stock_art, video_jobs, video_youtube, web_admin, youtube)
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
    return dbpool.connect(**_DB)   # 連線池（2026-09-26）；close() 是歸還連線池


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
    app.include_router(fb_page.router)   # Facebook 粉專 webhook（api/fb_page.py；同上）
    line_bot.start_reminder_scheduler()   # 預約提醒背景排程（每日固定時間推播）
    fb_drafts.start_scheduler()           # 粉專貼文草稿（每天開店前由本機 AI 寫，管理者勾選才發）
    video_youtube.start_scheduler()       # 官網嵌的 YouTube 影片每 6 小時重查（下架／改私人就改播自己的 MP4）
    video_jobs.cleanup()                  # 「做影片」：上次沒做完的標成失敗、太舊的暫存刪掉

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

    @app.get("/care-reminders")
    def page_care_reminders(request: Request):
        return _page(request, "care_reminders.html", "care-reminders")

    @app.get("/purchases")
    def page_purchases(request: Request):
        return _page(request, "purchases.html", "purchases")

    @app.get("/line-chat")
    def page_line_chat(request: Request):
        return _page(request, "line_chat.html", "line-chat")

    @app.get("/fb-chat")
    def page_fb_chat(request: Request):
        return _page(request, "fb_chat.html", "fb-chat")

    @app.get("/fb-posts")
    def page_fb_posts(request: Request):
        return _page(request, "fb_posts.html", "fb-posts")

    @app.get("/hearing")
    def page_hearing(request: Request):
        return _page(request, "hearing.html", "hearing")

    @app.get("/web-admin")
    def page_web_admin(request: Request):
        return _page(request, "web_admin.html", "web")

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
    # ── 保養提醒（規則在 api/care_reminders.py）──────────────────────
    #   John 2026-10-22：先只列名單、預覽 LINE 訊息，不自動傳給客人；店員可按「加到行事曆」建 follow_up_reminders。
    def _store_contact(cur) -> Dict[str, str]:
        """訊息裡的店名、電話：用官網的門市設定（y1web.web_settings），讀不到就用 LINE 機器人的寫法。"""
        try:
            cur.execute("SELECT v FROM y1web.web_settings WHERE k='store'")
            r = cur.fetchone()
            v = json.loads(r["v"]) if r and isinstance(r["v"], (str, bytes)) else (r["v"] if r else {})
            if v.get("name") and v.get("phone"):
                return {"name": v["name"], "phone": v["phone"]}
        except pymysql.MySQLError:
            pass
        return {"name": "睿聲助聽器-中正門市", "phone": ""}

    @app.get("/api/care-reminders")
    def care_reminder_list(request: Request, ahead: int = care_reminders.DEFAULT_AHEAD):
        _require_menu(_current_user(request), "customers")
        ahead = max(7, min(ahead, 120))
        today = date.today()
        with _conn() as db, db.cursor() as cur:
            # 助聽器的購買／租借（沒選商品的交易也算：店裡的交易大多是助聽器，名單上會標「未選商品」）；
            # 之後同一機型退貨的不算
            cur.execute(
                "SELECT p.id, p.customer_id, p.transaction_date, p.warranty_end_date, pr.brand, pr.model_name "
                "FROM purchases p LEFT JOIN products pr ON pr.id = p.product_id "
                "WHERE p.transaction_type IN ('purchase','rental') AND (pr.id IS NULL OR pr.category = 'hearing_aid') "
                "  AND NOT EXISTS (SELECT 1 FROM purchases r WHERE r.customer_id = p.customer_id "
                "    AND r.product_id = p.product_id AND r.transaction_type = 'return' "
                "    AND r.transaction_date >= p.transaction_date)")
            buys = {b["id"]: b for b in cur.fetchall()}
            cur.execute("SELECT customer_id, reminder_type, scheduled_date, status FROM follow_up_reminders "
                        "WHERE scheduled_date >= %s", (today - timedelta(days=care_reminders.DEDUP_DAYS + 200),))
            items = care_reminders.due_items(buys.values(), cur.fetchall(), today, ahead)
            cids = tuple({i["customer_id"] for i in items}) or (0,)
            cur.execute("SELECT id, name, gender, customer_code, phone_mobile, line_user_id FROM customers "
                        "WHERE id IN %s", (cids,))
            custs = {c["id"]: c for c in cur.fetchall()}
            cur.execute("SELECT customer_id, MIN(req_date) AS d FROM booking_requests WHERE customer_id IN %s "
                        "AND status <> 'cancelled' AND req_date >= %s GROUP BY customer_id", (cids, today))
            booked = {r["customer_id"]: r["d"] for r in cur.fetchall()}
            store = _store_contact(cur)
            care_url = web_admin.get_site_url(cur, config.WEB["site_url"]) + "/care"
        rows = []
        for it in items:
            c, b = custs.get(it["customer_id"]), buys[it["purchase_id"]]
            if not c:
                continue
            model = " ".join(x for x in (b["brand"], b["model_name"]) if x)          # 沒選商品＝空字串
            rows.append({"customer_id": c["id"], "customer_name": c["name"], "customer_code": c["customer_code"],
                         "phone": c["phone_mobile"], "line": bool(c["line_user_id"]),
                         "purchase_id": it["purchase_id"], "model": model, "type": it["type"], "label": it["label"],
                         "due": it["due"].isoformat(), "overdue": it["overdue"],
                         "transaction_date": b["transaction_date"].isoformat(),
                         "booked": booked[c["id"]].isoformat() if c["id"] in booked else None,
                         "message": care_reminders.message(it, care_reminders.salutation(c["name"], c["gender"]),
                                                           model, store["name"], store["phone"], care_url)})
        return {"rows": rows, "today": today.isoformat(), "ahead": ahead, "send_enabled": False}

    @app.post("/api/care-reminders")
    def care_reminder_schedule(request: Request, body: Dict[str, Any] = Body(...)):
        """「加到行事曆」：建一筆回訪提醒（follow_up_reminders），行事曆和看板會顯示。"""
        user = _current_user(request)
        _require_menu(user, "customers")
        rtype = body.get("type")
        if rtype not in care_reminders.TYPE_LABEL:
            raise HTTPException(422, "提醒類型不正確。")
        due_iso = _parse_date(str(body.get("due") or ""))
        if not due_iso:
            raise HTTPException(422, "日期不正確。")
        due = date.fromisoformat(due_iso)
        try:
            cid, pid = int(body.get("customer_id")), int(body.get("purchase_id"))
        except (TypeError, ValueError):
            raise HTTPException(422, "缺少客戶或交易。")
        label = str(body.get("label") or care_reminders.TYPE_LABEL[rtype])[:20]
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id FROM purchases WHERE id=%s AND customer_id=%s", (pid, cid))
            if not cur.fetchone():
                raise HTTPException(404, "找不到這筆交易。")
            cur.execute("INSERT INTO follow_up_reminders (customer_id, purchase_id, reminder_type, scheduled_date, "
                        "status, notes) VALUES (%s,%s,%s,%s,'pending',%s)",
                        (cid, pid, rtype, max(due, date.today()), f"{label}（保養提醒名單，{user['username']} 建立）"))
            rid = cur.lastrowid
        return {"ok": True, "id": rid}

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
            rows = [_row_json(r) for r in cur.fetchall()]
            # 官網預約沒有身分驗證：電話剛好對到一位客戶時只「提示」，由店員按「掛上」確認
            phones = {re.sub(r"\D", "", r["phone"] or "") for r in rows if not r["customer_id"]} - {""}
            match: Dict[str, List[Dict[str, Any]]] = {}
            if phones:
                cur.execute("SELECT id, name, customer_code, "
                            "       REPLACE(REPLACE(phone_mobile,'-',''),' ','') AS ph FROM customers "
                            "WHERE REPLACE(REPLACE(phone_mobile,'-',''),' ','') IN %s", (tuple(phones),))
                for c in cur.fetchall():
                    match.setdefault(c["ph"], []).append(c)
            for r in rows:
                cands = match.get(re.sub(r"\D", "", r["phone"] or ""), []) if not r["customer_id"] else []
                r["phone_match"] = ({"id": cands[0]["id"], "name": cands[0]["name"],
                                     "customer_code": cands[0]["customer_code"]}
                                    if len(cands) == 1 else None)
            return {"rows": rows}

    @app.put("/api/bookings/{bid}/customer")
    def link_booking_customer(request: Request, bid: int, body: Dict[str, Any] = Body(...)):
        """店員確認後把預約掛到客戶（官網預約只憑填寫的電話，不自動掛）。"""
        _require_menu(_current_user(request), "customers")
        try:
            cid = int(body.get("customer_id"))
        except (TypeError, ValueError):
            raise HTTPException(422, "customer_id 不正確。")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id FROM customers WHERE id=%s", (cid,))
            if cur.fetchone() is None:
                raise HTTPException(404, "找不到這位客戶。")
            cur.execute("UPDATE booking_requests SET customer_id=%s "
                        "WHERE id=%s AND customer_id IS NULL", (cid, bid))
            if cur.rowcount != 1:
                raise HTTPException(422, "這筆預約不存在，或已經掛了客戶。")
        return {"ok": True}

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

    # ── Facebook 粉專訊息（Messenger 私訊＋貼文留言；規則在 api/fb_page.py）─────────
    #   和 LINE 不同：粉專的回覆直接從這裡送（不收費、不佔額度），所以這頁可以回覆。
    _flog = logging.getLogger("y1crm.fb")

    def _fb_thread(cur, tid: int) -> Dict[str, Any]:
        t = fb_page.get_thread(cur, tid)
        if t is None:
            raise HTTPException(404, "找不到這段對話（可能已被刪除）。")
        return t

    @app.get("/api/fb/threads")
    def fb_threads(request: Request):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.kind, t.person_name, t.post_id, t.last_in_at, t.last_msg_at, "
                "       CASE WHEN t.mode='human' AND (t.human_until IS NULL OR t.human_until > NOW()) "
                "            THEN 'human' ELSE 'bot' END AS mode, "
                "       t.human_until, t.assigned_to, t.handoff_reason, "
                "       (t.last_in_at IS NOT NULL AND (t.last_out_at IS NULL OR t.last_out_at < t.last_in_at) "
                "        AND (t.handled_at IS NULL OR t.handled_at < t.last_in_at)) AS pending, "
                "       (t.kind='comment' OR t.last_in_at > NOW() - INTERVAL %s HOUR) AS can_reply, "
                "       (SELECT text FROM fb_messages m WHERE m.thread_id=t.id AND m.source<>'ai_draft' "
                "          ORDER BY m.id DESC LIMIT 1) AS last_text, "
                "       (SELECT COUNT(*) FROM fb_messages m WHERE m.thread_id=t.id AND m.source='ai_draft' "
                "          AND m.status='new') AS drafts, "
                "       COALESCE((SELECT w.title FROM fb_posts p JOIN y1web.web_posts w ON w.id=p.web_post_id "
                "          WHERE p.fb_post_id=t.post_id ORDER BY p.id DESC LIMIT 1), "
                "        (SELECT d.topic FROM fb_post_drafts d WHERE d.fb_post_id=t.post_id LIMIT 1)) AS post_title "
                "FROM fb_threads t ORDER BY t.last_msg_at DESC, t.id DESC LIMIT 100",
                (fb_page.WINDOW_HOURS,))
            rows = [_row_json(r) for r in cur.fetchall()]
        for r in rows:
            r["pending"], r["can_reply"] = bool(r["pending"]), bool(r["can_reply"])
        return {"rows": rows, "dev_mode": fb_page.dev_mode(), "ai_reply": fb_page.ai_reply_mode(),
                "ai_enabled": bool(config.AI.get("enabled")), "window_hours": fb_page.WINDOW_HOURS}

    @app.get("/api/fb/threads/{tid}/messages")
    def fb_thread_messages(request: Request, tid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            _fb_thread(cur, tid)
            cur.execute("SELECT id, direction, source, text, status, meta, created_by, created_at "
                        "FROM fb_messages WHERE thread_id=%s ORDER BY id DESC LIMIT 100", (tid,))
            rows = [_row_json(r) for r in reversed(cur.fetchall())]
        return {"rows": rows}

    @app.post("/api/fb/threads/{tid}/reply")
    def fb_thread_reply(request: Request, tid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "customers")
        draft = body.get("draft_id")
        try:
            r = fb_page.staff_reply(tid, str(body.get("text") or ""), user["realname"],
                                    int(draft) if draft else None)
        except LookupError:
            raise HTTPException(404, "找不到這段對話（可能已被刪除）。")
        except fb_page.GraphError as e:
            raise HTTPException(422, str(e))
        _flog.info("粉專回覆 thread=%s 由 %s %s", tid, user["realname"], "已送出" if r["sent"] else "（開發模式）")
        return {"ok": True, **r}

    @app.put("/api/fb/threads/{tid}/mode")
    def fb_thread_mode(request: Request, tid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "customers")
        mode = _clean(body.get("mode"))
        if mode not in ("bot", "human"):
            raise HTTPException(422, "mode 須為 bot 或 human。")
        with _conn() as db, db.cursor() as cur:
            _fb_thread(cur, tid)
            if mode == "human":
                fb_page.set_human(cur, tid, "staff", user["realname"])
            else:
                fb_page.set_bot(cur, tid)
        return {"ok": True, "mode": mode}

    @app.put("/api/fb/threads/{tid}/handled")
    def fb_thread_handled(request: Request, tid: int):
        """「不用回」：例如只留了一個讚或謝謝，清掉待回標記。"""
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            _fb_thread(cur, tid)
            cur.execute("UPDATE fb_threads SET handled_at=NOW() WHERE id=%s", (tid,))
        return {"ok": True}

    @app.delete("/api/fb/threads/{tid}")
    def fb_thread_delete(request: Request, tid: int):
        """客人要求刪除資料時用：只刪我們這邊的紀錄，Facebook 上的留言／私訊不受影響。"""
        user = _current_user(request)
        _require_menu(user, "customers")
        with _conn() as db, db.cursor() as cur:
            t = _fb_thread(cur, tid)
            cur.execute("DELETE FROM fb_threads WHERE id=%s", (tid,))   # 訊息跟著 CASCADE 刪掉
        _flog.info("粉專對話 #%s（%s）已由 %s 刪除", tid, t["kind"], user["realname"])
        return {"ok": True}

    @app.put("/api/fb/drafts/{mid}")
    def fb_draft_discard(request: Request, mid: int):
        _require_menu(_current_user(request), "customers")
        with _conn() as db, db.cursor() as cur:
            cur.execute("UPDATE fb_messages SET status='discarded' WHERE id=%s AND source='ai_draft' "
                        "AND status='new'", (mid,))
            if cur.rowcount == 0:
                raise HTTPException(422, "這則 AI 建議已經處理過了。")
        return {"ok": True}

    # ── 粉專貼文草稿（本機 AI 每天寫，管理者勾選才發；規則在 api/fb_drafts.py）──────────
    #   權限用 web（admin、manager）：發文是門市對外的聲音，和官網管理同一群人。
    _LINK_RE = re.compile(r"^https?://\S{3,490}$")

    def _fbd_json(r: Dict[str, Any], site: str = "") -> Dict[str, Any]:
        d = _row_json(r)
        d["category_label"] = fb_drafts.CATEGORIES.get(r["category"], r["category"])
        d["seo"] = fb_drafts._json(r.get("seo"))
        d["picked"], d["web_sync"] = bool(r.get("picked")), bool(r.get("web_sync"))
        d["image_meta"] = fb_drafts._json(r.get("image_meta"))
        d["image_url"] = f"/api/web/media/{r['image']}" if r.get("image") else None
        d["card_meta"] = fb_drafts._json(r.get("card_meta"))           # 圖卡（照片＋大字標題）：有就發這張
        d["card_url"] = f"/api/web/media/{r['card']}" if r.get("card") else None
        if r["status"] == "new":
            e, d["check_warnings"] = fb_drafts.check(r["message"])
            d["check_errors"] = e + (fb_drafts.column_errors(r) if r.get("web_sync") else [])
            d["auto"], d["auto_note"] = fb_drafts.auto_status(r)
        if r.get("web_slug") and site:
            d["web_url"] = f"{site}/column/{r['web_slug']}"
        return d

    def _fbd_get(cur, did: int, editable: bool = True) -> Dict[str, Any]:
        cur.execute("SELECT * FROM fb_post_drafts WHERE id=%s", (did,))
        r = cur.fetchone()
        if r is None:
            raise HTTPException(404, "找不到這則草稿（可能已被刪除）。")
        if editable and r["status"] != "new":
            raise HTTPException(409, "這則已經發佈或刪掉了，請重新整理頁面。")
        return r

    def _fbd_link(v: Any) -> Optional[str]:
        link = str(v or "").strip()
        if link and not _LINK_RE.match(link):
            raise HTTPException(422, "連結要是 http:// 或 https:// 開頭的完整網址。")
        return link or None

    @app.get("/api/fb/post-drafts")
    def fb_post_drafts(request: Request):
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT * FROM fb_post_drafts WHERE status='new' AND day >= %s ORDER BY day DESC, id",
                        (date.today() - timedelta(days=fb_drafts.PENDING_DAYS),))
            pending = [_fbd_json(r) for r in cur.fetchall()]
            site = web_admin.get_site_url(cur, config.WEB["site_url"])
            cur.execute("SELECT d.*, w.slug AS web_slug FROM fb_post_drafts d "
                        "LEFT JOIN y1web.web_posts w ON w.id=d.web_article_id "
                        "WHERE d.status IN ('published','dev') ORDER BY d.published_at DESC, d.id DESC LIMIT 20")
            recent = [_fbd_json(r, site) for r in cur.fetchall()]
        cfg = getattr(config, "SOCIAL", {})
        dl = fb_drafts.auto_deadline(date.today())
        return {"pending": pending, "recent": recent, "dev_mode": fb_page.dev_mode(),
                "categories": fb_drafts.CATEGORIES, "column_categories": web_admin.COLUMN_CATEGORIES,
                "auto": {"enabled": bool(cfg.get("draft_enabled", True)), "hour": int(cfg.get("draft_hour", 6)),
                         "count": int(cfg.get("draft_count", 3)), "news": bool(cfg.get("news", True)),
                         "publish_at": f"{dl:%H:%M}" if dl else "", "review_min": fb_drafts.AUTO_REVIEW_MIN}}

    @app.post("/api/fb/post-drafts")
    def fb_post_draft_new(request: Request, body: Dict[str, Any] = Body(...)):
        """「多寫一則」：本機 AI 當場寫（約 10～40 秒）。有題目就照題目，沒有就從選的類別挑。"""
        user = _current_user(request)
        _require_menu(user, "web")
        cat = _clean(body.get("category")) or "own"
        if cat not in fb_drafts.CATEGORIES:
            raise HTTPException(422, "類別不對。")
        brief = str(body.get("brief") or "").strip()[:500]
        if cat == "own" and not brief:
            raise HTTPException(422, "請寫下想寫的題目或重點。")
        link = _fbd_link(body.get("link"))
        try:
            did = fb_drafts.write_one(cat, brief, user["username"], link)
        except LookupError as e:
            raise HTTPException(422, str(e.args[0]))
        except Exception:  # noqa: BLE001
            logging.getLogger("y1crm.fbdraft").exception("多寫一則失敗")
            raise HTTPException(503, "本機 AI 沒有回應，請稍後再試。")
        with _conn() as db, db.cursor() as cur:
            return _fbd_json(_fbd_get(cur, did, False))

    @app.put("/api/fb/post-drafts/{did}")
    def fb_post_draft_save(request: Request, did: int, body: Dict[str, Any] = Body(...)):
        """管理者改字、改連結、勾選（要不要發）、同步到聽力專欄和官網版資料（發佈前隨時存）。"""
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            _fbd_get(cur, did)
            if "message" in body:
                msg = str(body.get("message") or "").strip()
                if not msg:
                    raise HTTPException(422, "貼文內容不能是空的。")
                if len(msg) > fb_page.MAX_POST:
                    raise HTTPException(422, f"貼文太長，請控制在 {fb_page.MAX_POST} 字以內。")
                cur.execute("UPDATE fb_post_drafts SET message=%s WHERE id=%s", (msg, did))
            if "link" in body:
                cur.execute("UPDATE fb_post_drafts SET link=%s WHERE id=%s", (_fbd_link(body.get("link")), did))
            if "picked" in body:          # 取消勾選＝不要自動發
                cur.execute("UPDATE fb_post_drafts SET picked=%s WHERE id=%s", (int(bool(body["picked"])), did))
            if "web_sync" in body:
                cur.execute("UPDATE fb_post_drafts SET web_sync=%s WHERE id=%s", (int(bool(body["web_sync"])), did))
            if "seo" in body:
                try:
                    seo = fb_drafts.clean_seo_input(body["seo"])
                except web_admin.Invalid as e:
                    raise HTTPException(422, str(e))
                cur.execute("UPDATE fb_post_drafts SET seo=%s WHERE id=%s", (json.dumps(seo, ensure_ascii=False), did))
            return _fbd_json(_fbd_get(cur, did, False))

    @app.post("/api/fb/post-drafts/{did}/regenerate")
    def fb_post_draft_regen(request: Request, did: int):
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            _fbd_get(cur, did)
        try:
            fb_drafts.regenerate(did)
        except (LookupError, ValueError):
            raise HTTPException(409, "這則已經發佈或刪掉了，請重新整理頁面。")
        except Exception:  # noqa: BLE001
            logging.getLogger("y1crm.fbdraft").exception("重寫草稿 #%s 失敗", did)
            raise HTTPException(503, "本機 AI 沒有回應，請稍後再試。")
        with _conn() as db, db.cursor() as cur:
            return _fbd_json(_fbd_get(cur, did, False))

    @app.post("/api/fb/post-drafts/{did}/art")
    def fb_post_draft_art(request: Request, did: int, body: Dict[str, Any] = Body(...)):
        """配圖：從圖庫挑一張，force＝換一張（已發佈的也可以：粉專上的不會變，同步的專欄封面會換）。回更新後的草稿。"""
        user = _current_user(request)
        _require_menu(user, "web")
        with _conn() as db, db.cursor() as cur:
            r = _fbd_get(cur, did, False)
            if r["status"] == "discarded":
                raise HTTPException(409, "這則已經不要了。")
        res = post_art.illustrate([did], [], user["username"], bool(body.get("force")))
        if res["errors"]:
            raise HTTPException(422, res["errors"][0])
        with _conn() as db, db.cursor() as cur:
            return _fbd_json(_fbd_get(cur, did, False))

    @app.put("/api/fb/post-drafts/{did}/card")
    def fb_post_draft_card(request: Request, did: int, body: Dict[str, Any] = Body(...)):
        """圖卡：店員改大字標題（ai＝true：請本機 AI 重擬）→ 重做圖卡。回更新後的草稿。"""
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            r = _fbd_get(cur, did)
            if not r["image"]:
                raise HTTPException(422, "這則沒有配圖：請先按「挑一張」。")
            try:
                if body.get("ai"):
                    post_card.build(cur, did, retitle=True)
                else:
                    title, sub = post_card.check_title(body.get("title"), body.get("sub"))
                    post_card.build(cur, did, title, sub)
            except ValueError as e:
                raise HTTPException(422, str(e))
            except FileNotFoundError:
                raise HTTPException(422, "配圖的檔案不見了：請按「換一張」。")
            return _fbd_json(_fbd_get(cur, did, False))

    @app.delete("/api/fb/post-drafts/{did}/image")
    def fb_post_draft_image_remove(request: Request, did: int):
        """拿掉配圖（發成純文字或連結貼文）。"""
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            _fbd_get(cur, did)
            post_art.set_draft_image(cur, _WEB_MEDIA, did, None)
            return _fbd_json(_fbd_get(cur, did, False))

    @app.delete("/api/fb/post-drafts/{did}")
    def fb_post_draft_discard(request: Request, did: int):
        """「不要這則」：留著紀錄（題目 60 天內不再出），只是不再列出。"""
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            _fbd_get(cur, did)
            cur.execute("UPDATE fb_post_drafts SET status='discarded' WHERE id=%s AND status='new'", (did,))
        return {"ok": True}

    @app.post("/api/fb/post-drafts/publish")
    def fb_post_draft_publish(request: Request, body: Dict[str, Any] = Body(...)):
        """發佈勾選的。有錯誤（價格、待填）全部不發；療效／品牌提醒要帶 force=true 再送一次。"""
        user = _current_user(request)
        _require_menu(user, "web")
        try:
            ids = [int(i) for i in body.get("ids") or []]
        except (TypeError, ValueError):
            raise HTTPException(422, "請先勾選要發佈的貼文。")
        try:
            return fb_drafts.publish(ids, bool(body.get("force")), user["username"])
        except ValueError as e:
            raise HTTPException(422, str(e))
        except LookupError as e:
            raise HTTPException(409, str(e.args[0]))

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

    # ── 紙本表單 OCR（本機 ollama qwen3.8；個資不出區網）───────
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
name(姓名；台灣常見複姓有 張簡、張廖、簡廖、范姜、歐陽、司馬、諸葛、上官、司徒，四個字的姓名前兩字常是複姓或冠夫姓（夫姓＋本姓，例如 陳林、李廖），第二個字通常也是姓氏，請對照字形辨識，
     不確定就照字形填並把 name 列入 uncertain), gender("M"或"F"), national_id(身分證字號),
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

    def _call_ocr(image_b64: str, mime: str, prompt: Optional[str] = None, think: Any = False) -> Dict[str, Any]:
        # think：預設關（照實抄寫最快）；客戶表單可由 config [ocr] form_think 暫時打開（2026-11-13 測試用）
        payload = json.dumps({
            "model": _OCR_MODEL, "prompt": prompt or _OCR_PROMPT, "images": [image_b64],
            "stream": False, "keep_alive": -1,
            "think": think,   # qwen3.8 預設會思考：OCR 只要 JSON，關掉 CoT 省下大量生成 token（實測延遲主因）
            "options": {"temperature": 0, "num_ctx": 8192},
        }).encode()
        req = urllib.request.Request(f"{_OLLAMA_URL}/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as res:
                r = json.loads(res.read())
        except urllib.error.URLError as e:
            raise HTTPException(503, f"本機辨識服務未回應（GPU 可能在休眠時段）：{e.reason}")
        logging.getLogger("y1crm.ocr").info("OCR 模型：思考 %d 字、生成 %s token（think=%s）",
                                            len(r.get("thinking") or ""), r.get("eval_count"), think)   # 只記長度
        text = (r.get("response") or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)   # 去掉 markdown 圍欄
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise HTTPException(502, "辨識結果無法解析，請重拍或改為手動輸入。")
        try:
            return json.loads(m.group(0))
        except ValueError:
            raise HTTPException(502, "辨識結果不是有效格式，請重拍或改為手動輸入。")

    def _chat_ocr(messages: List[Dict[str, Any]], think: Any = False) -> Tuple[Dict[str, Any], str]:
        """對話式 OCR（2026-11-13）：回傳（解析後的 JSON, 原始文字）。接續同一段對話追問時，
        llama.cpp 會沿用前面讀過的圖（提示詞快取），重讀姓名實測 1.2 秒；重送整張圖要約 11 秒。"""
        payload = json.dumps({"model": _OCR_MODEL, "messages": messages, "stream": False, "keep_alive": -1,
                              "think": think, "options": {"temperature": 0, "num_ctx": 8192}}).encode()
        req = urllib.request.Request(f"{_OLLAMA_URL}/api/chat", data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as res:
                r = json.loads(res.read())
        except urllib.error.URLError as e:
            raise HTTPException(503, f"本機辨識服務未回應（GPU 可能在休眠時段）：{e.reason}")
        msg = r.get("message") or {}
        logging.getLogger("y1crm.ocr").info("OCR 模型：思考 %d 字、生成 %s token（think=%s）",
                                            len(msg.get("thinking") or ""), r.get("eval_count"), think)   # 只記長度
        raw_text = (msg.get("content") or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text)
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise HTTPException(502, "辨識結果無法解析，請重拍或改為手動輸入。")
        try:
            return json.loads(m.group(0)), raw_text
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
        _t0 = time.time()
        _think = config.OCR.get("form_think") or False
        _msgs = [{"role": "user", "content": _OCR_PROMPT, "images": [base64.b64encode(data).decode()]}]
        raw, _raw_text = await asyncio.to_thread(_chat_ocr, _msgs, _think)
        logging.getLogger("y1crm.ocr").info("客戶表單 OCR 完成：%.1f 秒（思考=%s）", time.time() - _t0, _think)   # 只記時間，不記內容（個資）

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

        # 合理性檢查（2026-11-13）：只標黃、不改值（姓氏／複姓、身分證檢查碼、性別對身分證、年齡、日期、電話、Email）
        from api import ocr_check
        reasons = ocr_check.check(fields)
        # 姓名不合理 → 針對姓名欄再讀一次（仍關思考，約多 7～10 秒）。第二個字給「常見姓氏」當參考：
        #   2026-11-13 冠夫姓「李廖」被讀成「李聲」——四字姓名的第二字幾乎都是姓（複姓或冠夫姓）。
        #   重讀結果合理才採用，但一律保留標黃並寫明原讀值，由店員對照紙本確認。
        if "name" in reasons and fields.get("name"):
            old = fields["name"]
            rq = (f"上面姓名欄讀成「{old}」，但這個姓不太合理。"
                  "台灣四個字的姓名幾乎都是複姓（張簡、張廖、簡廖、范姜、歐陽…）或冠夫姓（夫姓＋本姓，例如 陳林、李廖、王陳），"
                  "所以第二個字通常也是姓氏，常見的有：廖 陳 林 黃 張 李 王 吳 劉 蔡 楊 許 鄭 謝 郭 洪 曾 邱 賴 周 徐 蘇 葉 莊 呂 江 何 "
                  "蕭 羅 高 潘 簡 朱 鍾 彭 游 詹 胡 施 沈 余 趙 盧 梁 顏 柯 孫 魏 翁 戴 范 宋 方 鄧 杜 傅 侯 曹 溫 薛 丁 馬 蔣 唐。"
                  "請再仔細看一次姓名欄每個字的字形，用台灣正體中文只輸出 JSON：{\"name\": \"姓名\"}")
            try:
                r2, _ = await asyncio.to_thread(_chat_ocr, _msgs + [{"role": "assistant", "content": _raw_text},
                                                                     {"role": "user", "content": rq}], False)
                new = re.sub(r"\s", "", str(r2.get("name") or ""))
            except Exception:
                new = ""
            if new and new != old and "name" not in ocr_check.check({"name": new}):
                fields["name"] = new
                reasons["name"] = f"第一次讀成「{old}」，針對姓名重讀後改為「{new}」，請對照紙本確認"
            logging.getLogger("y1crm.ocr").info("OCR 姓名重讀：%s", "已改用重讀結果" if fields["name"] != old else "重讀沒有更合理的結果")
        if reasons:
            logging.getLogger("y1crm.ocr").info("OCR 合理性檢查標記：%s", ",".join(reasons))   # 只記欄位名，不記內容
        for k in reasons:
            if k not in uncertain:
                uncertain.append(k)
        return {"fields": fields,
                "uncertain": [u for u in uncertain if u in fields or u in _OCR_DATE_FIELDS],
                "reasons": {k: v for k, v in reasons.items() if k in fields}}

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

    # ── 聽檢輔助（MA42 螢幕照片／PDF → 本機 qwen3.8 讀值 → 講義規則判讀）───
    #   規則全在 api/hearing.py（純函式、有單元測試），這裡只管 I/O 與權限。
    #   讀值與表單 OCR 共用同一個常駐模型（同 model 名，不會互相擠出 VRAM），
    #   模型載入狀態也沿用 /api/ocr/warmup、/api/ocr/status。
    _NHAX_MIME = "application/x-nhax"       # Noah 匯出檔沒有標準 MIME，前端上傳時指定這個
    _NHAX_MAX = 30 * 1024 * 1024             # 一位客戶實測 1MB；多年紀錄也遠小於此

    def _hearing_upload_mime(request: Request, allow_nhax: bool = False) -> str:
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        if mime.startswith("image/") or mime == "application/pdf" or (allow_nhax and mime == _NHAX_MIME):
            return mime
        raise HTTPException(422, "只接受圖片、PDF 或 Noah 匯出檔（.nhax）。" if allow_nhax else "只接受圖片或 PDF 檔案。")

    async def _hearing_body(request: Request, limit: int = _PHOTO_MAX) -> bytes:
        data = await request.body()
        if not data:
            raise HTTPException(422, "沒有收到檔案內容。")
        if len(data) > limit:
            raise HTTPException(413, f"檔案超過 {limit // 1024 // 1024}MB，無法上傳。")
        return data

    def _parse_nhax(data: bytes) -> Dict[str, Any]:
        try:
            return noah.parse_nhax(data)
        except noah.NoahError as e:
            raise HTTPException(422, str(e))

    def _pdf_first_page(data: bytes) -> tuple:
        """PDF 第 1 頁轉 PNG（長邊 1600px，同 OCR 的準確度設定）＋內嵌文字當輔助。"""
        import fitz   # PyMuPDF；只有這裡用到，延遲載入
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception:
            raise HTTPException(422, "PDF 檔案無法開啟，請確認檔案沒有損壞。")
        with doc:
            if doc.page_count == 0:
                raise HTTPException(422, "PDF 沒有任何頁面。")
            page = doc[0]
            zoom = 1600 / max(page.rect.width, page.rect.height)
            png = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
            text = page.get_text().strip()
        return base64.b64encode(png).decode(), text[:3000]

    @app.post("/api/hearing/read")
    async def hearing_read(request: Request):
        _require_menu(_current_user(request), "hearing")
        mime = _hearing_upload_mime(request)
        data = await _hearing_body(request)
        prompt = hearing.READ_PROMPT
        if mime == "application/pdf":
            b64, text = await asyncio.to_thread(_pdf_first_page, data)
            if text:
                prompt += ("\n\n這份是 PDF 報告，以下是檔案內嵌的文字（可能含數值表，可交叉比對）：\n"
                           + text)
        else:
            b64 = base64.b64encode(data).decode()
        raw = await asyncio.to_thread(_call_ocr, b64, mime, prompt)
        return hearing.from_vlm(raw)

    # Noah 匯出檔：只解析不存檔（原始檔跟著聽檢紀錄儲存時才上傳）
    @app.post("/api/hearing/noah")
    async def hearing_noah(request: Request):
        _require_menu(_current_user(request), "hearing")
        data = await _hearing_body(request, _NHAX_MAX)
        return await asyncio.to_thread(_parse_nhax, data)

    @app.post("/api/hearing/assess")
    def hearing_assess(request: Request, body: Dict[str, Any] = Body(...)):
        _require_menu(_current_user(request), "hearing")
        return hearing.assess(hearing.clean_data(body.get("data")), **hearing.options(body))

    @app.get("/api/hearing/customers")
    def hearing_customers(request: Request, q: str = ""):
        _require_menu(_current_user(request), "hearing")
        q = q.strip()
        if not q:
            return {"rows": []}
        like = f"%{q}%"
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id, customer_code, name, gender, birth_date FROM customers "
                        "WHERE name LIKE %s OR customer_code LIKE %s OR phone_mobile LIKE %s "
                        "ORDER BY updated_at DESC LIMIT 20", (like, like, like))
            return {"rows": [_row_json(r) for r in cur.fetchall()]}

    def _collect_hearing(body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            cid = int(body.get("customer_id"))
        except (TypeError, ValueError):
            raise HTTPException(422, "請先選擇客戶。")
        test_date = _parse_date(body.get("test_date"))
        if not test_date:
            raise HTTPException(422, "請填寫檢查日期。")
        opts = hearing.options(body)
        data = hearing.clean_data(body.get("data"))
        summary = hearing.assess(data, **opts)["summary"]   # 存檔當下的判讀，列表顯示用
        return {"customer_id": cid, "test_date": test_date, **opts,
                "data": json.dumps(data, ensure_ascii=False),
                "summary": summary[:200] or None, "rules_version": hearing.RULES_VERSION,
                "notes": (_clean(body.get("notes")) or "")[:1000] or None}

    def _hearing_customer_exists(cur, cid: int) -> None:
        cur.execute("SELECT id FROM customers WHERE id=%s", (cid,))
        if cur.fetchone() is None:
            raise HTTPException(404, "找不到這位客戶（可能已被移除）。")

    @app.get("/api/hearing/tests")
    def list_hearing_tests(request: Request, customer_id: int):
        _require_menu(_current_user(request), "hearing")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT t.id, t.test_date, t.summary, t.created_by, t.updated_at, "
                        "(SELECT COUNT(*) FROM hearing_test_files f WHERE f.test_id=t.id) AS files "
                        "FROM hearing_tests t WHERE t.customer_id=%s "
                        "ORDER BY t.test_date DESC, t.id DESC", (customer_id,))
            return {"rows": [_row_json(r) for r in cur.fetchall()]}

    @app.get("/api/hearing/tests/{tid}")
    def get_hearing_test(request: Request, tid: int):
        _require_menu(_current_user(request), "hearing")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT t.*, c.name AS customer_name, c.customer_code "
                        "FROM hearing_tests t JOIN customers c ON c.id=t.customer_id "
                        "WHERE t.id=%s", (tid,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(404, "找不到這筆聽檢紀錄（可能已被刪除）。")
            cur.execute("SELECT id, filename, mime, bytes_size, created_by, created_at "
                        "FROM hearing_test_files WHERE test_id=%s ORDER BY id", (tid,))
            files = [_row_json(r) for r in cur.fetchall()]
        out = _row_json(row)
        out["data"] = hearing.clean_data(json.loads(row["data"]))
        out["files"] = files
        return out

    @app.post("/api/hearing/tests")
    def create_hearing_test(request: Request, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "hearing")
        row = _collect_hearing(body)
        cols = list(row) + ["created_by", "updated_by"]
        with _conn() as db, db.cursor() as cur:
            _hearing_customer_exists(cur, row["customer_id"])
            cur.execute(f"INSERT INTO hearing_tests ({', '.join(f'`{c}`' for c in cols)}) "
                        f"VALUES ({', '.join(['%s'] * len(cols))})",
                        list(row.values()) + [user["realname"]] * 2)
            return {"id": cur.lastrowid}

    @app.put("/api/hearing/tests/{tid}")
    def update_hearing_test(request: Request, tid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "hearing")
        row = _collect_hearing(body)
        with _conn() as db, db.cursor() as cur:
            _hearing_customer_exists(cur, row["customer_id"])
            sets = ", ".join(f"`{c}`=%s" for c in row)
            cur.execute(f"UPDATE hearing_tests SET {sets}, updated_by=%s WHERE id=%s",
                        list(row.values()) + [user["realname"], tid])
            cur.execute("SELECT id FROM hearing_tests WHERE id=%s", (tid,))
            if cur.fetchone() is None:   # rowcount 在「內容沒變」時也是 0，改用查詢確認存在
                raise HTTPException(404, "找不到這筆聽檢紀錄（可能已被刪除）。")
        return {"ok": True}

    @app.delete("/api/hearing/tests/{tid}")
    def delete_hearing_test(request: Request, tid: int):
        _require_menu(_current_user(request), "hearing")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT rel_path FROM hearing_test_files WHERE test_id=%s", (tid,))
            paths = [r["rel_path"] for r in cur.fetchall()]
            cur.execute("DELETE FROM hearing_tests WHERE id=%s", (tid,))   # 檔案列 FK CASCADE
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到這筆聽檢紀錄（可能已被刪除）。")
        for rel in paths:
            config.delete_file(rel)
        return {"ok": True}

    # 原始檔（MA42 螢幕照片／PDF 報告）：存磁碟 hearing/{tid}/，DB 只存相對路徑
    @app.post("/api/hearing/tests/{tid}/files")
    async def upload_hearing_file(request: Request, tid: int, filename: str = ""):
        user = _current_user(request)
        _require_menu(user, "hearing")
        mime = _hearing_upload_mime(request, allow_nhax=True)
        data = await _hearing_body(request, _NHAX_MAX if mime == _NHAX_MIME else _PHOTO_MAX)
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT id FROM hearing_tests WHERE id=%s", (tid,))
            if cur.fetchone() is None:
                raise HTTPException(404, "找不到這筆聽檢紀錄（可能已被刪除）。")
            rel = config.save_file("hearing_files", data, mime, tid, orig_name=filename or None)
            cur.execute("INSERT INTO hearing_test_files "
                        "(test_id, filename, rel_path, mime, bytes_size, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (tid, _clean(filename), rel, mime, len(data), user["realname"]))
            return {"id": cur.lastrowid}

    def _hearing_file(fid: int) -> tuple:
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT mime, rel_path, filename FROM hearing_test_files WHERE id=%s", (fid,))
            row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "找不到檔案（可能已被刪除）。")
        path = config.abs_path(row["rel_path"])
        if not path.is_file():
            raise HTTPException(404, "檔案遺失。")
        return row, path

    @app.get("/api/hearing/files/{fid}")
    def get_hearing_file(request: Request, fid: int):
        _require_menu(_current_user(request), "hearing")
        row, path = _hearing_file(fid)
        if row["mime"] == _NHAX_MIME:        # 瀏覽器開不了，給下載（可再匯入 Noah）
            return FileResponse(path, media_type="application/octet-stream",
                                filename=row["filename"] or "noah.nhax")
        return FileResponse(path, media_type=row["mime"],
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/hearing/files/{fid}/noah")
    async def get_hearing_file_noah(request: Request, fid: int):
        """開舊紀錄時重新解析存檔的 Noah 檔（選配設定不另存，原始檔就是唯一來源）。"""
        _require_menu(_current_user(request), "hearing")
        row, path = _hearing_file(fid)
        if row["mime"] != _NHAX_MIME:
            raise HTTPException(422, "這個原始檔不是 Noah 匯出檔。")
        return await asyncio.to_thread(lambda: _parse_nhax(path.read_bytes()))

    @app.delete("/api/hearing/files/{fid}")
    def delete_hearing_file(request: Request, fid: int):
        _require_menu(_current_user(request), "hearing")
        with _conn() as db, db.cursor() as cur:
            cur.execute("SELECT rel_path FROM hearing_test_files WHERE id=%s", (fid,))
            row = cur.fetchone()
            cur.execute("DELETE FROM hearing_test_files WHERE id=%s", (fid,))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到檔案（可能已被刪除）。")
        if row:
            config.delete_file(row["rel_path"])
        return {"ok": True}

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

    # ── 官網管理（y1web 的關於我們／聽力專欄／好評分享／使用小教室／補助小幫手）─────────
    #   規則全在 api/web_admin.py（欄位檢查、圖片處理，有單元測試），這裡只管 I/O 與權限。
    #   資料寫進 y1web.web_posts；圖片存到官網的 media/uploads（config.WEB.media_dir）。
    _WEB_MEDIA = Path(config.WEB["media_dir"])
    _WEB_IMG_MAX = 15 * 1024 * 1024
    _wlog = logging.getLogger("y1crm.web")

    def _web_kind(kind: str) -> str:
        if kind not in web_admin.KINDS:
            raise HTTPException(422, "不支援的內容類型。")
        return kind

    async def _web_image_body(request: Request) -> bytes:
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        if not mime.startswith("image/"):
            raise HTTPException(422, "只接受圖片檔案。")
        if int(request.headers.get("content-length") or 0) > _WEB_IMG_MAX:
            raise HTTPException(413, "圖片超過 15MB，請縮小後再上傳。")
        data = await request.body()
        if len(data) > _WEB_IMG_MAX:
            raise HTTPException(413, "圖片超過 15MB，請縮小後再上傳。")
        return data

    @app.get("/api/web/meta")
    def web_meta(request: Request):
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            site = web_admin.get_site_url(cur, config.WEB["site_url"])
            links = web_admin.get_share_links(cur)
        platforms = {k: {"label": label, "stars": stars, "link": "none" if domains == () else "yes",
                         "all": bool(domains)}
                     for k, (label, domains, stars) in web_admin.SHARE_PLATFORMS.items()}
        return {"kinds": web_admin.KINDS, "categories": web_admin.COLUMN_CATEGORIES,
                "care_types": web_admin.CARE_TYPES, "subsidy_conditions": web_admin.SUBSIDY_CONDITIONS,
                "statuses": web_admin.STATUSES, "site_url": site, "share_formats": web_admin.SHARE_FORMATS,
                "share_platforms": platforms, "share_links": links, "art": review_art.enabled()}

    @app.put("/api/web/site")
    def web_site_update(request: Request, body: Dict[str, Any] = Body(...)):
        """官網網址（換網域時改這裡一次就好）。"""
        user = _current_user(request)
        _require_menu(user, "web")
        try:
            with _conn() as db, db.cursor() as cur:
                url = web_admin.set_site_url(cur, body.get("base_url"), user["username"])
        except web_admin.Invalid as e:
            raise HTTPException(422, str(e))
        _wlog.info("官網管理 %s 官網網址改為 %s", user["username"], url)
        return {"ok": True, "site_url": url}

    @app.put("/api/web/share-links")
    def web_share_links_update(request: Request, body: Dict[str, Any] = Body(...)):
        """好評分享頁的「到 Google 地圖看全部評論」這類連結。"""
        user = _current_user(request)
        _require_menu(user, "web")
        try:
            with _conn() as db, db.cursor() as cur:
                links = web_admin.set_share_links(cur, body, user["username"])
        except web_admin.Invalid as e:
            raise HTTPException(422, str(e))
        _wlog.info("官網管理 %s 好評分享連結改為 %s", user["username"], links)
        return {"ok": True, "share_links": links}

    @app.post("/api/web/share/draft")
    def web_share_draft(request: Request, body: Dict[str, Any] = Body(...)):
        """評論轉貼：拆解整段複製的評論（text），或只檢查原文（original）；回建議的節錄。不存檔。"""
        _require_menu(_current_user(request), "web")
        try:
            return web_admin.review_draft(str(body.get("text") or ""), str(body.get("original") or ""))
        except web_admin.Invalid as e:
            raise HTTPException(422, str(e))

    # 一次貼很多則 Google 評論 → 草稿（發佈仍要勾同意）；沒圖片的可以畫示意插圖（api/review_art.py）
    @app.post("/api/web/share/batch")
    def web_share_batch(request: Request, body: Dict[str, Any] = Body(...)):
        """拆解整片複製的評論區（不存檔）：回一則一則的帳號、日期、原文、建議節錄、是不是轉貼過。"""
        _require_menu(_current_user(request), "web")
        try:
            with _conn() as db, db.cursor() as cur:
                items = web_admin.batch_reviews(cur, str(body.get("text") or ""))
                url = web_admin.get_share_links(cur).get("google", "")
        except web_admin.Invalid as e:
            raise HTTPException(422, str(e))
        return {"items": items, "url": url, "art": review_art.enabled()}

    @app.post("/api/web/share/batch/create")
    def web_share_batch_create(request: Request, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "web")
        items = body.get("items") if isinstance(body.get("items"), list) else []
        if not items:
            raise HTTPException(422, "請勾選要轉貼的評論。")
        with _conn() as db, db.cursor() as cur:
            url = web_admin.get_share_links(cur).get("google", "")
            r = web_admin.create_share_drafts(cur, [x for x in items if isinstance(x, dict)], user["username"], url)
        _wlog.info("官網管理 %s 批次轉貼評論：建 %s，跳過 %d", user["username"], r["created"], len(r["skipped"]))
        draw = [i for i in r["created"] if i not in r["with_photos"]]      # 附了照片的等店員上傳照片
        r["art_started"] = bool(draw and body.get("illustrate") and review_art.enabled()
                                and review_art.start(draw, user["username"]))
        return r

    @app.post("/api/web/share/import-fb")
    def web_share_import_fb(request: Request, body: Dict[str, Any] = Body(...)):
        """粉專的推薦（正面、有文字）→ 好評轉貼草稿；轉貼過的（原文相同）自動跳過。"""
        user = _current_user(request)
        _require_menu(user, "web")
        try:
            recs = fb_page.recommendations()
        except fb_page.GraphError as e:
            raise HTTPException(502, str(e))
        items = [{**x, "shown": web_admin.suggest_excerpt(x["original"])
                  if any(f["bad"] for f in web_admin.flag_sentences(x["original"])) else x["original"]} for x in recs]
        with _conn() as db, db.cursor() as cur:
            fb_url = web_admin.get_share_links(cur).get("facebook")   # 有設「看全部評論」就連到那裡（店家從瀏覽器複製的，最準）
            if fb_url:
                items = [{**x, "url": fb_url} for x in items]
            r = web_admin.create_share_drafts(cur, items, user["username"], platform="facebook")
        _wlog.info("官網管理 %s 匯入粉專推薦：%d 則，建 %s", user["username"], len(recs), r["created"])
        r["found"] = len(recs)
        r["art_started"] = bool(r["created"] and body.get("illustrate") and review_art.enabled()
                                and review_art.start(r["created"], user["username"]))
        return r

    @app.get("/api/web/share/illustrate")
    def web_share_art_status(request: Request):
        _require_menu(_current_user(request), "web")
        return {**review_art.status, "enabled": review_art.enabled()}

    @app.post("/api/web/share/illustrate")
    def web_share_art(request: Request, body: Dict[str, Any] = Body(...)):
        """畫示意插圖：ids 沒給＝所有沒圖片的評論轉貼；force＝已經是示意插圖的重畫。背景執行，用 GET 看進度。"""
        user = _current_user(request)
        _require_menu(user, "web")
        if not review_art.enabled():
            raise HTTPException(503, "這台主機沒有設定畫圖的環境（config [imagegen]）。")
        ids = body.get("ids")
        try:
            ids = [int(i) for i in ids] if isinstance(ids, list) else None
        except (TypeError, ValueError):
            raise HTTPException(422, "ids 格式不對。")
        if not review_art.start(ids, user["username"], bool(body.get("force"))):
            raise HTTPException(409, "已經有一批在畫了，請等它畫完。")
        return {"ok": True}

    @app.post("/api/web/illustrate")
    def web_post_art(request: Request, body: Dict[str, Any] = Body(...)):
        """聽力專欄、使用小教室的封面：從圖庫挑真實照片。ids 沒給＝這個類別（kind）所有沒封面的；force＝換一張。
        店員自己放的封面（原圖）不會被蓋掉。回 {total, done, errors}。"""
        user = _current_user(request)
        _require_menu(user, "web")
        kind = body.get("kind")
        if kind not in post_art.ART_KINDS:
            raise HTTPException(422, "只有聽力專欄和使用小教室從圖庫挑封面。")
        ids = body.get("ids")
        try:
            ids = [int(i) for i in ids] if isinstance(ids, list) else None
        except (TypeError, ValueError):
            raise HTTPException(422, "ids 格式不對。")
        return post_art.illustrate([], ids, user["username"], bool(body.get("force")), (kind,))

    # 聽力專欄「從網址翻譯文章」（api/article_import.py）：本機 AI 翻譯、圖片帶回並標出處；背景執行，GET 看進度；做好是草稿
    @app.post("/api/web/translate")
    def web_translate(request: Request, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "web")
        cat = body.get("category") or web_admin.COLUMN_CATEGORIES[0]
        if cat not in web_admin.COLUMN_CATEGORIES:
            raise HTTPException(422, "請選擇專欄分類。")
        try:
            url = article_import.check_url(str(body.get("url") or ""))
            site = web_admin._text({"source_site": body.get("site")}, "source_site", False, "出處名稱") or ""
        except web_admin.Invalid as e:
            raise HTTPException(422, str(e))
        html_text = body.get("html")                 # 網站擋程式讀取時：店員從瀏覽器複製貼上的原文
        html_text = str(html_text) if isinstance(html_text, str) and html_text.strip() else None
        if html_text and len(html_text) > article_import.PASTE_MAX:
            raise HTTPException(413, "貼上的內容太長了，請只複製文章那一段。")
        if not article_import.start(url, html_text, site, cat, body.get("images") is not False, user["username"]):
            raise HTTPException(409, "已經有一篇在翻譯了，請等它做完。")
        _wlog.info("官網管理 %s 翻譯文章 %s%s", user["username"], url, "（貼上原文）" if html_text else "")
        return {"ok": True}

    @app.get("/api/web/translate")
    def web_translate_status(request: Request):
        _require_menu(_current_user(request), "web")
        return dict(article_import.status)

    @app.get("/api/web/posts")
    def web_posts(request: Request, kind: str):
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            return {"rows": web_admin.list_posts(cur, _web_kind(kind))}

    def _web_save(request: Request, body: Dict[str, Any], pid: Optional[int]):
        user = _current_user(request)
        _require_menu(user, "web")
        try:
            with _conn() as db, db.cursor() as cur:
                if pid is not None:
                    old = web_admin.get_post(cur, pid)
                    if old is None:
                        raise HTTPException(404, "找不到這筆內容（可能已被刪除）。")
                    kind = old["kind"]
                else:
                    kind = _web_kind(body.get("kind") or "")
                clean = web_admin.clean_post(kind, body)
                if pid is not None:     # 表單上沒有的欄位（AI 插圖標記、評論代號、照片數）存檔不能弄丟
                    old_x = json.loads(old["extra"] or "{}") if isinstance(old["extra"], (str, bytes)) else (old["extra"] or {})
                    keep = web_admin.carried_extra(old_x)
                    if keep:
                        clean["extra"] = {**(clean.get("extra") or {}), **keep}
                    if old_x.get("posted_prec") and (clean.get("extra") or {}).get("posted") == old_x.get("posted"):
                        clean["extra"]["posted_prec"] = old_x["posted_prec"]     # 日期沒改：還是只知道年
                # 評論轉貼有療效／誇大用語：先回 warnings，店員確認後帶 force=true 再存一次
                if clean.get("warnings") and not body.get("force"):
                    return {"ok": False, "warnings": clean["warnings"]}
                new_id = web_admin.save_post(cur, clean, user["username"], pid)
        except web_admin.Invalid as e:
            raise HTTPException(422, str(e))
        _wlog.info("官網管理 %s %s #%s「%s」%s", user["username"], "修改" if pid else "新增",
                 new_id, clean["title"], clean["status"])
        return {"ok": True, "id": new_id, "slug": clean["slug"]}

    @app.post("/api/web/posts")
    def web_post_create(request: Request, body: Dict[str, Any] = Body(...)):
        return _web_save(request, body, None)

    @app.put("/api/web/posts/{pid}")
    def web_post_update(request: Request, pid: int, body: Dict[str, Any] = Body(...)):
        return _web_save(request, body, pid)

    @app.delete("/api/web/posts/{pid}")
    def web_post_delete(request: Request, pid: int):
        user = _current_user(request)
        _require_menu(user, "web")
        with _conn() as db, db.cursor() as cur:
            old = web_admin.get_post(cur, pid)
            if old is None:
                raise HTTPException(404, "找不到這筆內容（可能已被刪除）。")
            cur.execute(f"DELETE FROM {web_admin.TABLE} WHERE id=%s", (pid,))
            web_admin.remove_if_unused(cur, _WEB_MEDIA, old["cover"])
        _wlog.info("官網管理 %s 刪除 #%s「%s」", user["username"], pid, old["title"])
        return {"ok": True}

    # 分享到粉專：只有聽力專欄、使用小教室（有自己的網頁）而且已經在官網看得到的才能分享。
    def _web_share_target(cur, pid: int) -> Dict[str, Any]:
        cur.execute(f"SELECT id, kind, slug, title, summary, status, publish_at FROM {web_admin.TABLE} "
                    f"WHERE id=%s", (pid,))
        post = cur.fetchone()
        if post is None:
            raise HTTPException(404, "找不到這筆內容（可能已被刪除）。")
        site = web_admin.get_site_url(cur, config.WEB["site_url"])
        link = fb_page.post_url(site, post["kind"], post["slug"])
        reason = None
        if not link:
            reason = "只有「聽力專欄」和「使用小教室」的文章可以分享到粉專。"
        elif post["status"] != "published":
            reason = "請先把這篇改成「發佈」並儲存。"
        elif post["publish_at"] and post["publish_at"] > datetime.now():
            reason = "這篇的發佈日期還沒到，官網上還看不到。"
        return {**post, "link": link, "reason": reason}

    @app.get("/api/web/posts/{pid}/facebook")
    def web_post_fb_info(request: Request, pid: int):
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            t = _web_share_target(cur, pid)
            cur.execute("SELECT status, fb_post_id, error, created_by, created_at FROM fb_posts "
                        "WHERE web_post_id=%s ORDER BY id DESC LIMIT 10", (pid,))
            hist = [_row_json(r) for r in cur.fetchall()]
        return {"link": t["link"], "reason": t["reason"], "dev_mode": fb_page.dev_mode(),
                "text": fb_page.default_post_text(t["title"], t["summary"] or ""), "history": hist}

    @app.post("/api/web/posts/{pid}/facebook")
    def web_post_fb_share(request: Request, pid: int, body: Dict[str, Any] = Body(...)):
        """發到粉專。價格字眼一律擋；療效／誇大用語回 warnings，店員確認後帶 force=true 再送一次。"""
        user = _current_user(request)
        _require_menu(user, "web")
        message = str(body.get("message") or "").strip()
        errors, warns = fb_page.check_post_text(message)
        if errors:
            raise HTTPException(422, " ".join(errors))
        if warns and not body.get("force"):
            return {"ok": False, "warnings": warns}
        with _conn() as db, db.cursor() as cur:
            t = _web_share_target(cur, pid)
            if t["reason"]:
                raise HTTPException(422, t["reason"])
            try:
                fid = fb_page.publish_link(message, t["link"])
                status, err = ("sent" if fid else "dev"), None
            except fb_page.GraphError as e:
                fid, status, err = None, "failed", str(e)
            cur.execute("INSERT INTO fb_posts (web_post_id, fb_post_id, message, link, status, error, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (pid, fid, message, t["link"], status, err and err[:255], user["username"]))
        _wlog.info("官網管理 %s 分享 #%s 到粉專：%s %s", user["username"], pid, status, fid or err or "")
        if status == "failed":
            raise HTTPException(502, err)
        return {"ok": True, "status": status, "fb_post_id": fid}

    # 影片送 YouTube（api/video_youtube.py）：API 稽核通過前店員在 YouTube 工作室手動上傳，
    #   這裡給要貼的標題／說明欄／標籤、下載影片檔，再收回 YouTube 網址（查過才讓官網嵌）。
    def _web_video_post(cur, pid: int) -> Dict[str, Any]:
        post = video_youtube.post_row(cur, pid)
        if post is None:
            raise HTTPException(404, "找不到這筆內容（可能已被刪除）。")
        if post["video"] is None:
            raise HTTPException(422, "這篇還沒有影片。")
        return post

    @app.get("/api/web/posts/{pid}/youtube")
    def web_post_youtube(request: Request, pid: int):
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            post = _web_video_post(cur, pid)
            store, site = fb_drafts.store_info(cur), fb_drafts.site_url(cur)
        v, ch = post["video"], config.YOUTUBE.get("channel_id")
        return {"pack": video_youtube.pack(post, store, site), "template": video_youtube.channel_template(store, site),
                "video": {"sec": v.get("sec"), "at": v.get("at"),
                          "tall": {"sec": v["tall"].get("sec"), "at": v["tall"].get("at"), "link": v["tall"].get("youtube")}
                          if isinstance(v.get("tall"), dict) and v["tall"].get("file") else None},
                "link": v.get("youtube"),
                "authorized": youtube.authorized(), "published": post["status"] == "published",
                "channel": f"https://www.youtube.com/channel/{ch}" if ch else None}

    @app.put("/api/web/posts/{pid}/youtube")
    def web_post_youtube_link(request: Request, pid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "web")
        try:
            with _conn() as db, db.cursor() as cur:
                info = video_youtube.link(cur, pid, str(body.get("url") or ""), user["username"],
                                          "tall" if body.get("which") == "tall" else "main")
        except video_youtube.Invalid as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "link": info}

    @app.post("/api/web/posts/{pid}/youtube/check")
    def web_post_youtube_check(request: Request, pid: int):
        _require_menu(_current_user(request), "web")
        video_youtube.recheck(pid)
        with _conn() as db, db.cursor() as cur:
            return {"link": _web_video_post(cur, pid)["video"].get("youtube")}

    @app.post("/api/web/posts/{pid}/youtube/sync")
    def web_post_youtube_sync(request: Request, pid: int, body: Dict[str, Any] = Body(default={})):
        """視窗裡的說明、標籤（店員可以先改）寫到 YouTube 上那支影片；標題、類別不動。"""
        user = _current_user(request)
        _require_menu(user, "web")
        try:
            with _conn() as db, db.cursor() as cur:
                return video_youtube.sync_meta(cur, pid, user["username"], body.get("description"), body.get("tags"))
        except video_youtube.Invalid as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/web/posts/{pid}/youtube")
    def web_post_youtube_unlink(request: Request, pid: int, which: str = "main"):
        user = _current_user(request)
        _require_menu(user, "web")
        try:
            with _conn() as db, db.cursor() as cur:
                video_youtube.unlink(cur, pid, "tall" if which == "tall" else "main")
        except video_youtube.Invalid as e:
            raise HTTPException(422, str(e))
        _wlog.info("官網管理 %s 拿掉 #%s 的 YouTube 連結", user["username"], pid)
        return {"ok": True}

    # 做影片（api/motion_video.py、api/video_jobs.py）：AI 擬分鏡 → 店員改 → 背景產生 → 預覽 → 放上官網
    def _video_article(cur, pid: int) -> Dict[str, Any]:
        cur.execute(f"SELECT id, kind, slug, title, summary, body_md, category, status, extra FROM {web_admin.TABLE} "
                    f"WHERE id=%s", (pid,))
        post = cur.fetchone()
        if post is None:
            raise HTTPException(404, "找不到這筆內容（可能已被刪除）。")
        if post["kind"] != "article":
            raise HTTPException(422, "目前只有聽力專欄的文章可以做影片。")
        return post

    @app.get("/api/web/posts/{pid}/video-plan")
    def web_video_plan(request: Request, pid: int):
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            post = _video_article(cur, pid)
        x = fb_drafts._json(post["extra"])
        photos = [{"id": e["id"], "title": e["title"], "file": e["file"]}
                  for e in stock_art.available(stock_art.load(), _WEB_MEDIA) if not e.get("credit_required")]
        return {"plan": x.get("video_draft"), "photos": photos, "kinds": motion_video.KIND_LABELS,
                "header": motion_video.header_of(post["title"]), "job": video_jobs.latest(pid),
                "has_video": isinstance(x.get("video"), dict) and bool(x["video"].get("file"))}

    @app.post("/api/web/posts/{pid}/video-plan/ai")
    def web_video_plan_ai(request: Request, pid: int):
        """本機 AI 擬分鏡（30 秒～2 分鐘）。不存檔：店員看過、改過按「儲存分鏡」或「產生影片」才存。"""
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            post = _video_article(cur, pid)
        return motion_video.draft_plan(post)

    def _save_video_plan(cur, pid: int, plan: Dict[str, Any], user: str) -> Dict[str, Any]:
        try:
            plan = motion_video.check_plan(plan)
        except ValueError as e:
            raise HTTPException(422, str(e))
        cur.execute(f"UPDATE {web_admin.TABLE} SET extra=JSON_SET(COALESCE(extra, JSON_OBJECT()), '$.video_draft', "
                    f"CAST(%s AS JSON)), updated_by=%s WHERE id=%s", (json.dumps(plan, ensure_ascii=False), user, pid))
        return plan

    @app.put("/api/web/posts/{pid}/video-plan")
    def web_video_plan_save(request: Request, pid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "web")
        with _conn() as db, db.cursor() as cur:
            _video_article(cur, pid)
            return {"ok": True, "plan": _save_video_plan(cur, pid, body.get("plan") or {}, user["username"])}

    @app.post("/api/web/posts/{pid}/video-job")
    def web_video_job_start(request: Request, pid: int, body: Dict[str, Any] = Body(...)):
        """存分鏡、開始產生影片（背景做，約 5 分鐘）。"""
        user = _current_user(request)
        _require_menu(user, "web")
        with _conn() as db, db.cursor() as cur:
            _video_article(cur, pid)
            plan = _save_video_plan(cur, pid, body.get("plan") or {}, user["username"])
        try:
            return video_jobs.start(pid, plan, user["username"])
        except video_jobs.Busy as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    def _job(job_id: str) -> Dict[str, Any]:
        try:
            return video_jobs.status(job_id)
        except (KeyError, ValueError):
            raise HTTPException(404, "找不到這個影片工作（可能已經放上官網或捨棄了）。")

    @app.get("/api/web/video-jobs/{job_id}")
    def web_video_job(request: Request, job_id: str):
        _require_menu(_current_user(request), "web")
        return _job(job_id)

    @app.get("/api/web/video-jobs/{job_id}/{name}")
    def web_video_job_file(request: Request, job_id: str, name: str):
        _require_menu(_current_user(request), "web")
        try:
            path = video_jobs.file_path(job_id, name)
        except KeyError:
            raise HTTPException(404, "找不到檔案。")
        return FileResponse(path, media_type="video/mp4" if name.endswith(".mp4") else "image/jpeg",
                            headers={"Cache-Control": "private, no-store"})

    @app.post("/api/web/video-jobs/{job_id}/install")
    def web_video_job_install(request: Request, job_id: str):
        user = _current_user(request)
        _require_menu(user, "web")
        _job(job_id)
        try:
            info = video_jobs.install(job_id, user["username"])
        except ValueError as e:
            raise HTTPException(422, str(e))
        _wlog.info("官網管理 %s 放上影片 %s", user["username"], info["file"])
        return {"ok": True, "video": {k: info[k] for k in ("file", "poster", "sec", "at")}}

    @app.delete("/api/web/video-jobs/{job_id}")
    def web_video_job_discard(request: Request, job_id: str):
        _require_menu(_current_user(request), "web")
        _job(job_id)
        try:
            video_jobs.discard(job_id)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True}

    # 影片發粉專（fb_page.publish_video）：直式＝Reels（建議）、橫式＝一般影片。價格字眼一律擋，療效用語先提醒
    @app.get("/api/web/posts/{pid}/video-fb")
    def web_video_fb_info(request: Request, pid: int):
        _require_menu(_current_user(request), "web")
        with _conn() as db, db.cursor() as cur:
            post = _video_article(cur, pid)
            site = fb_drafts.site_url(cur)
            cur.execute("SELECT fb_post_id, status, error, created_by, created_at FROM fb_posts WHERE web_post_id=%s "
                        "ORDER BY id DESC LIMIT 10", (pid,))
            hist = [_row_json(r) for r in cur.fetchall()]
        x = fb_drafts._json(post["extra"])
        v = x.get("video") if isinstance(x.get("video"), dict) else None
        if not v or not v.get("file"):
            raise HTTPException(422, "這篇還沒有影片。")
        plan = x.get("video_draft") or {}
        return {"message": motion_video.fb_message(post, plan, site), "tall": bool((v.get("tall") or {}).get("file")),
                "dev_mode": fb_page.dev_mode(), "history": hist, "published": post["status"] == "published"}

    @app.post("/api/web/posts/{pid}/video-fb")
    def web_video_fb_publish(request: Request, pid: int, body: Dict[str, Any] = Body(...)):
        user = _current_user(request)
        _require_menu(user, "web")
        message = str(body.get("message") or "").strip()
        errors, warns = fb_page.check_post_text(message)
        e2, w2 = fb_drafts.check(message)
        if errors or e2:
            raise HTTPException(422, " ".join(errors + e2))
        if (warns or w2) and not body.get("force"):
            return {"ok": False, "warnings": warns + w2}
        with _conn() as db, db.cursor() as cur:
            post = _video_article(cur, pid)
            site = fb_drafts.site_url(cur)
        if post["status"] != "published":
            raise HTTPException(422, "這篇還是草稿：請先把文章改成「發佈」，貼文裡的連結才看得到。")
        v = fb_drafts._json(post["extra"]).get("video") or {}
        src = v.get("tall") if body.get("which", "tall") == "tall" else v
        if not isinstance(src, dict) or not src.get("file") or not video_youtube.FILE_RE.match(str(src["file"])):
            raise HTTPException(422, "這篇沒有這個版本的影片。")
        link = fb_page.post_url(site, post["kind"], post["slug"]) or ""
        try:
            vid = fb_page.publish_video(message, post["title"], (_WEB_MEDIA / src["file"]).read_bytes(),
                                        (_WEB_MEDIA / src["poster"]).read_bytes() if src.get("poster") else None)
            status, err = ("sent" if vid else "dev"), None
        except fb_page.GraphError as e:
            vid, status, err = None, "failed", str(e)
        with _conn() as db, db.cursor() as cur:
            cur.execute("INSERT INTO fb_posts (web_post_id, fb_post_id, message, link, status, error, created_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)", (pid, vid, message, link, status, err and err[:255], user["username"]))
        _wlog.info("官網管理 %s 影片發粉專 #%s：%s %s", user["username"], pid, status, vid or err or "")
        if status == "failed":
            raise HTTPException(502, err)
        return {"ok": True, "status": status, "id": vid,
                "url": f"https://www.facebook.com/reel/{vid}/" if vid and body.get("which", "tall") == "tall"
                else (f"https://www.facebook.com/{vid}" if vid else None)}

    @app.get("/api/web/posts/{pid}/video.{ext}")
    def web_post_video_file(request: Request, pid: int, ext: str, dl: int = 0, v: str = ""):
        """後台預覽／下載文章的影片和封面（要上傳到 YouTube、粉專的檔案）。v=tall：直式 9:16 版。"""
        _require_menu(_current_user(request), "web")
        if ext not in ("mp4", "jpg") or v not in ("", "tall"):
            raise HTTPException(404, "找不到檔案。")
        with _conn() as db, db.cursor() as cur:
            post = _web_video_post(cur, pid)
        src = post["video"].get("tall") if v else post["video"]
        name = str((src if isinstance(src, dict) else {}).get("file" if ext == "mp4" else "poster") or "")
        path = _WEB_MEDIA / name
        if not video_youtube.FILE_RE.match(name) or not name.endswith(ext) or not path.is_file():
            raise HTTPException(404, "找不到檔案。")
        return FileResponse(path, media_type="video/mp4" if ext == "mp4" else "image/jpeg",
                            filename=f"{post['slug'] or pid}{'-9x16' if v else ''}.{ext}" if dl else None,
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.put("/api/web/posts/{pid}/cover")
    async def web_post_cover(request: Request, pid: int):
        user = _current_user(request)
        _require_menu(user, "web")
        data = await _web_image_body(request)
        try:
            name = web_admin.save_image(_WEB_MEDIA, data, thumb=True)
        except web_admin.Invalid as e:
            raise HTTPException(422, str(e))
        with _conn() as db, db.cursor() as cur:
            old = web_admin.get_post(cur, pid)
            if old is None:
                raise HTTPException(404, "找不到這筆內容（可能已被刪除）。")
            cur.execute(f"UPDATE {web_admin.TABLE} SET cover=%s, updated_by=%s, extra={web_admin.UPLOAD_COVER_KEYS} "
                        f"WHERE id=%s", (name, user["username"], datetime.now().strftime("%Y-%m-%d %H:%M"), pid))
            if old["cover"] != name:
                web_admin.remove_if_unused(cur, _WEB_MEDIA, old["cover"])
        return {"ok": True, "cover": name}

    @app.delete("/api/web/posts/{pid}/cover")
    def web_post_cover_delete(request: Request, pid: int):
        user = _current_user(request)
        _require_menu(user, "web")
        with _conn() as db, db.cursor() as cur:
            old = web_admin.get_post(cur, pid)
            if old is None:
                raise HTTPException(404, "找不到這筆內容（可能已被刪除）。")
            cur.execute(f"UPDATE {web_admin.TABLE} SET cover=NULL, updated_by=%s, extra={web_admin.CLEAR_COVER_KEYS} "
                        f"WHERE id=%s", (user["username"], pid))
            web_admin.remove_if_unused(cur, _WEB_MEDIA, old["cover"])
        return {"ok": True}

    @app.post("/api/web/images")
    async def web_image_upload(request: Request):
        """內文插圖：存檔後回傳官網上的網址，前端把 ![說明](網址) 插進內文。"""
        _require_menu(_current_user(request), "web")
        data = await _web_image_body(request)
        try:
            name = web_admin.save_image(_WEB_MEDIA, data)
        except web_admin.Invalid as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "name": name, "url": f"/media/uploads/{name}"}

    @app.get("/api/web/media/{name}")
    def web_media(request: Request, name: str):
        """後台預覽用（官網在另一個 port，直接從這裡讀同一個檔案）。"""
        _require_menu(_current_user(request), "web")
        path = web_admin.image_path(_WEB_MEDIA, name)
        if path is None or not path.is_file():
            raise HTTPException(404, "找不到圖片。")
        return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, max-age=86400"})

    return app


app = create_app()
