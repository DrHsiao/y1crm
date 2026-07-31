"""
config.py — y1crm 集中組態（storage / db / ocr / server）

載入順序（後者覆寫前者）：內建預設 → config.toml → 環境變數。
- 移轉／換碟／換機：只改 config.toml 的 [storage].base_dir 一行。
- 推廣／容器化：用環境變數 Y1CRM_<區段>_<鍵> 覆寫，不必改檔（12-factor）。
- 上傳檔一律以「相對路徑」存 DB、實體落在 base_dir 之下 →
  換基地時 DB 資料零改動（見 storage_path / storage_rel）。

檔案路徑：預設讀專案根目錄的 config.toml；可用 Y1CRM_CONFIG 指定別處。
config.toml 內含密碼等機密，不進版控；config.example.toml 為樣板。
"""
from __future__ import annotations

import os
import tomllib
import uuid as _uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_CONFIG_PATH = Path(os.environ.get("Y1CRM_CONFIG", str(_ROOT / "config.toml")))

# 內建預設（config.toml 缺項時的後備；密碼預設空，務必於 config.toml 設定）
_DEFAULTS: dict = {
    "storage": {
        "base_dir": "/mnt/raid1/y1crm_uploads",
        "todo_attachments": "todos",
        "customer_photos": "photos",
        "form_images": "forms",
        "staff_photos": "staff",
        "product_attachments": "products",
        "line_media": "line",       # LINE 客戶傳來的圖片/檔案
    },
    "db": {
        "host": "localhost", "user": "root", "password": "",
        "database": "crm1", "charset": "utf8mb4",
    },
    "ocr": {
        "ollama_url": "http://127.0.0.1:11434",
        "model": "qwen3-vl:32b-instruct",
    },
    "line": {
        "channel_id": "",
        "channel_secret": "",
        "channel_access_token": "",   # 未設＝開發模式：訊息只記 log 不真發
        "liff_id": "",
        "public_base_url": "",
        # 預約提醒排程：前一天的 reminder_hour 點推播給客戶（附一顆「確認前往」按鈕）
        "reminder_enabled": True,
        "reminder_hour": 18,          # 0–23，整點；每日只跑一次
        "reminder_days_ahead": 1,     # 提前幾天提醒（1＝提醒明天的預約）
    },
    # LINE 客服 AI（本機推論；⚠ 只能送純文字，573 那張卡做影像推論會靜默崩潰）
    "ai": {
        "enabled": True,
        "url": "http://127.0.0.1:11435",   # ollama-573（純文字專用實例）
        "model": "qwen3:32b",
        "timeout_secs": 30,     # 逾時就回道歉文；LINE reply token 約 1 分鐘失效，不宜貼邊
        "auto_reply": False,    # False＝只產生草稿給店員審核（第一階段）；True＝直接回客戶
        "human_hold_hours": 4,  # 人工接手後多久自動交還 AI（另外隔日開店也會復原）
        "max_chars": 150,       # 回覆長度上限（提示詞用）
        # 客戶傳來的圖片：走「好卡」config.OCR（11434, qwen3-vl），不是上面的 573
        "vision_timeout_secs": 60,
    },
    "server": {"host": "0.0.0.0", "port": 8004},
    # 店面狀態看板（免登入電視牆）：key 未設＝看板停用；URL 帶 ?k=<key> 存取
    "board": {"key": ""},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_file() -> dict:
    if _CONFIG_PATH.is_file():
        with open(_CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {}


def _apply_env(cfg: dict) -> dict:
    """環境變數覆寫：Y1CRM_<SECTION>_<KEY>（全大寫），型別沿用預設。"""
    for section, keys in cfg.items():
        if not isinstance(keys, dict):
            continue
        for k, cur in keys.items():
            env = f"Y1CRM_{section.upper()}_{k.upper()}"
            if env in os.environ:
                val: object = os.environ[env]
                if isinstance(cur, int) and not isinstance(cur, bool):
                    try:
                        val = int(val)  # 例如 port
                    except ValueError:
                        pass
                keys[k] = val
    return cfg


_CFG = _apply_env(_deep_merge(_DEFAULTS, _load_file()))

# ── 對外存取 ──────────────────────────────────────────────
STORAGE: dict = _CFG["storage"]
OCR: dict = _CFG["ocr"]
SERVER: dict = _CFG["server"]
LINE: dict = _CFG["line"]
BOARD: dict = _CFG["board"]
AI: dict = _CFG["ai"]

_STORAGE_KINDS = ("todo_attachments", "customer_photos", "form_images", "staff_photos",
                  "product_attachments", "line_media")


def db_params() -> dict:
    """給 pymysql 的連線參數（純資料，cursorclass/autocommit 由呼叫端補）。"""
    return dict(_CFG["db"])


def storage_base(kind: str) -> Path:
    """某類檔案的實體根目錄：base_dir / <該類子目錄>。"""
    return Path(STORAGE["base_dir"]) / STORAGE.get(kind, kind)


def storage_path(kind: str, rel: str) -> Path:
    """由 DB 存的相對路徑還原成絕對路徑（換基地時只有 base_dir 變）。"""
    return storage_base(kind) / rel


def ensure_dirs() -> None:
    """啟動時建立各類儲存目錄（base_dir 需可寫；缺失自動建立）。"""
    for kind in _STORAGE_KINDS:
        storage_base(kind).mkdir(parents=True, exist_ok=True)


# ── 檔案存取（原則：DB 不存 BLOB，位元組落磁碟，DB 只存相對路徑）──────────
_MIME_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
             "image/webp": "webp", "image/gif": "gif", "image/heic": "heic",
             "application/pdf": "pdf"}


def ext_for_mime(mime: str) -> str:
    return _MIME_EXT.get((mime or "").split(";")[0].strip().lower(), "bin")


def save_file(kind: str, data: bytes, mime: str, *subparts: object,
              orig_name: str | None = None) -> str:
    """寫檔到 base_dir/<kind 子目錄>/<subparts…>/<uuid>.<ext>。
    回傳「相對 base_dir」的路徑字串（存 DB 用）；換基地時此值不變。
    任意檔案（mime 不在對照表）以原檔名的副檔名為準，保留 pdf/docx/… 等。"""
    sub = STORAGE.get(kind, kind)
    ext = ext_for_mime(mime)
    if ext == "bin" and orig_name:                       # 非圖片/PDF：沿用原副檔名
        suffix = Path(orig_name).suffix.lstrip(".").lower()
        if suffix and suffix.isalnum() and len(suffix) <= 8:
            ext = suffix
    name = f"{_uuid.uuid4().hex}.{ext}"
    rel = "/".join([sub, *[str(p) for p in subparts], name])
    full = Path(STORAGE["base_dir"]) / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(data)
    return rel


def abs_path(rel: str) -> Path:
    """DB 存的相對路徑 → 絕對路徑（base_dir/rel）。"""
    return Path(STORAGE["base_dir"]) / rel


def delete_file(rel: str) -> None:
    """刪實體檔（不存在則忽略）。"""
    if not rel:
        return
    try:
        (Path(STORAGE["base_dir"]) / rel).unlink()
    except FileNotFoundError:
        pass
