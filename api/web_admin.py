"""
web_admin.py — 「官網管理」：編輯官網（y1web，port 8006）的關於韻譯、聽力專欄、好評分享。

資料在 y1web 資料庫的 web_posts。y1crm 用自己的資料庫連線寫入；官網那邊的資料庫帳號只能讀。
官網有 60 秒快取，存檔後最多一分鐘就會出現在官網上。
圖片一律轉成 WebP、縮到 1600px，存到官網的 media/uploads（官網唯讀），檔名＝內容雜湊。

本檔是純規則（欄位檢查、網址代稱、圖片處理）＋資料存取，有單元測試；app.py 只管 I/O 與權限。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps

KINDS = {"about": "關於韻譯", "article": "聽力專欄", "share": "好評分享"}
# 和官網 y1web app/content.py 的 COLUMN_CATEGORIES 相同，要一起改
COLUMN_CATEGORIES = ["助聽器知識", "聽力保健", "聽能復健", "補助申請", "生活樂活"]
STATUSES = {"draft": "草稿", "published": "發佈"}
LIMITS = {"title": 120, "summary": 300, "body_md": 50000, "slug": 80, "en": 60,
          "person": 40, "model": 60, "hearing": 40}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{16}\.webp$")
IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "MPO", "BMP"}
IMAGE_MAX_EDGE = 1600
IMAGE_MAX_PIXELS = 50_000_000          # 超過就拒收（防解壓縮炸彈）
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


def auto_slug(kind: str, now: Optional[datetime] = None) -> str:
    """沒填網址代稱時自動產生，例如 article → a261021153045。"""
    now = now or datetime.now()
    return f"{kind[0]}{now:%y%m%d%H%M%S}"


def clean_post(kind: str, body: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """店員送來的欄位 → 可以寫進 web_posts 的值。不合格丟 Invalid。"""
    if kind not in KINDS:
        raise Invalid("不支援的內容類型。")
    now = now or datetime.now()
    out: Dict[str, Any] = {
        "kind": kind,
        "title": _text(body, "title", True, "標題"),
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
    else:
        extra = {"person": _text(body, "person", True, "分享人稱呼"),
                 "model": _text(body, "model", False, "機種") or "",
                 "hearing": _text(body, "hearing", False, "聽力狀況") or "",
                 "consent": bool(body.get("consent"))}
        # 好評分享用的是真實客人的經驗與照片：沒有本人同意就不能公開
        if status == "published" and not extra["consent"]:
            raise Invalid("好評分享要先取得當事人同意（勾選「已取得當事人同意」）才能發佈。")
    out["extra"] = extra
    return out


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


def save_image(media_dir: Path, data: bytes) -> str:
    """處理後存到官網的 media/uploads；同一張圖只存一次。回傳檔名。"""
    out, name = process_image(data)
    media_dir.mkdir(parents=True, exist_ok=True)
    path = media_dir / name
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(out)
        tmp.chmod(0o644)
        tmp.replace(path)
    return name


def image_path(media_dir: Path, name: str) -> Optional[Path]:
    """檔名合格才組路徑（擋 ../ 之類）。"""
    return media_dir / name if IMAGE_NAME_RE.match(name or "") else None


# ── 資料存取（cur＝y1crm 的 DictCursor）────────────────────────

def _row(r: Dict[str, Any]) -> Dict[str, Any]:
    extra = r.get("extra")
    extra = json.loads(extra) if isinstance(extra, (str, bytes)) else (extra or {})
    pub = r.get("publish_at")
    return {**r, "extra": extra, "publish_date": pub.date().isoformat() if pub else "",
            "updated_at": r["updated_at"].strftime("%Y-%m-%d %H:%M") if r.get("updated_at") else ""}


def list_posts(cur, kind: str) -> List[Dict[str, Any]]:
    # 關於韻譯照排序（就是官網上由上而下的順序）；專欄與好評新的在前
    order = "sort, id" if kind == "about" else "(publish_at IS NULL) DESC, publish_at DESC, id DESC"
    cur.execute(f"SELECT id, kind, slug, title, summary, body_md, category, status, publish_at, sort, "
                f"cover, extra, updated_by, updated_at FROM {TABLE} WHERE kind=%s ORDER BY {order}", (kind,))
    return [_row(r) for r in cur.fetchall()]


def get_post(cur, pid: int) -> Optional[Dict[str, Any]]:
    cur.execute(f"SELECT id, kind, slug, title, cover, body_md FROM {TABLE} "
                f"WHERE id=%s AND kind IN ('about','article','share')", (pid,))
    return cur.fetchone()


def _slug_taken(cur, kind: str, slug: str, pid: Optional[int] = None) -> bool:
    cur.execute(f"SELECT id FROM {TABLE} WHERE kind=%s AND slug=%s", (kind, slug))
    r = cur.fetchone()
    return bool(r) and r["id"] != pid


def save_post(cur, clean: Dict[str, Any], actor: str, pid: Optional[int] = None) -> int:
    if _slug_taken(cur, clean["kind"], clean["slug"], pid):
        raise Invalid(f"網址代稱「{clean['slug']}」已經有人用了，請換一個。")
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
    path = image_path(media_dir, name)
    if path and path.exists():
        path.unlink()
