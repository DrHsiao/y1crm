#!/usr/bin/env python
"""
stock_import.py — 把 stock_library.json 裡的圖庫照片下載、裁成 3:2、調色，存到 y1web media/uploads，
檔名寫回清單的 file（可以重跑：已經匯入、檔案也在的跳過；--force 重新處理）。

    venv/bin/python -I scripts/illustrate/stock_import.py [--force] [--only px-8313438 ...]

只從清單裡這幾個圖庫網域下載（ALLOWED_HOSTS）；下載的內容只交給 PIL 處理，不存成檔案、不執行。
新增照片：在清單加一筆（來源頁、下載網址、授權、tags、裁切重心 focal 或 pad 背景色），再跑一次。
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import config  # noqa: E402
from api import stock_art, web_admin  # noqa: E402

ALLOWED_HOSTS = {"images.pexels.com", "images.unsplash.com", "commons.wikimedia.org", "upload.wikimedia.org",
                 "thumb.wikimedia.org"}
MAX_BYTES = 30 * 1024 * 1024
UA = "y1crm-stock-import/1.0 (hearing aid store website)"


def fetch(url: str) -> bytes:
    for _ in range(5):                          # 自己跟轉址，每一步都檢查網域
        host = urllib.parse.urlparse(url).hostname or ""
        if urllib.parse.urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
            raise ValueError(f"不在允許的圖庫網域：{host}")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=60) as r:
                data = r.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("檔案太大")
                return data
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise
    raise ValueError("轉址太多次")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", nargs="+")
    args = ap.parse_args()
    media = Path(config.WEB["media_dir"])
    items = json.loads(stock_art.LIBRARY.read_text(encoding="utf-8"))
    for x in items:
        if args.only and x["id"] not in args.only:
            continue
        if x.get("file") and (media / x["file"]).is_file() and not args.force:
            print(f"{x['id']}：已匯入 {x['file']}")
            continue
        try:
            data = fetch(x["download"])
            out = stock_art.process(data, tuple(x.get("focal") or (0.5, 0.5)), x.get("pad"))
            x["file"] = web_admin.save_image(media, out, thumb=True)
            print(f"{x['id']}：{x['file']}（{x['title']}）")
        except Exception as e:  # noqa: BLE001 — 一張失敗不影響其他
            print(f"{x['id']}：失敗 {e}")
    stock_art.LIBRARY.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
