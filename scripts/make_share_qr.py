"""產生門市的 Google 地圖、Facebook 粉專 QR（SVG），給 CRM 會員集點「門市聽檢 +10」按完跳出來的畫面、櫃檯海報用。
11-20 john：門市聽檢集點店員說很累 → 改一鍵加點，「並且同時產生 google 和 facebook 的打卡按讚，2 個 QR」。
連結取自官網管理「好評分享」設定（y1web.web_settings k='share_links'），Facebook 拿掉評論分頁（&sk=reviews）＝粉專本身。
連結改了重跑一次即可。要用 y1web 的 venv（有 qrcode）：
  cd /home/john/y1web && venv/bin/python /home/john/y1crm/scripts/make_share_qr.py"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/home/john/y1web")
from app import db, qr  # noqa: E402

OUT = Path("/home/john/y1crm/webui/static/img")
NAMES = {"google": "Google 地圖", "facebook": "Facebook 粉專"}


def main():
    v = json.loads(db.rows("SELECT v FROM web_settings WHERE k='share_links'")[0]["v"])
    OUT.mkdir(parents=True, exist_ok=True)
    for key, label in NAMES.items():
        url = re.sub(r"[?&]sk=reviews$|/reviews/?$", "", str(v.get(key) or ""))
        if not url.startswith("https://"):
            print("沒有設定", key)
            continue
        svg = str(qr.svg(url, f"{label} QR Code")).replace('class="qr" ', "")
        (OUT / f"share_qr_{key}.svg").write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n'
                                                 + svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1))
        print("ok", key, url)


if __name__ == "__main__":
    main()
