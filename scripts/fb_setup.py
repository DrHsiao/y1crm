#!/usr/bin/env python3
"""
fb_setup.py — Facebook 粉專串接的檢查與訂閱（權杖填進 config.toml [facebook] 之後跑）。

    ./venv/bin/python scripts/fb_setup.py check       # 權杖是不是這個粉專的、目前訂閱了哪些通知
    ./venv/bin/python scripts/fb_setup.py subscribe   # 讓粉專把留言、私訊送到我們的 webhook

權杖只從 config.toml 讀，不會印出來。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from api import fb_page  # noqa: E402

FIELDS = "feed,messages,message_echoes"   # 留言／私訊／店員在 Meta Business Suite 回的訊息


def main(cmd: str) -> int:
    missing = [k for k in ("page_id", "app_id", "app_secret", "page_token", "verify_token")
               if not config.FACEBOOK.get(k)]
    if missing:
        print("config.toml [facebook] 還沒填：", "、".join(missing))
        return 1
    page = config.FACEBOOK["page_id"]
    try:
        me = fb_page._graph("me", {"fields": "id,name"}, method="GET") or {}
        print(f"權杖屬於：{me.get('name')}（id {me.get('id')}）")
        if str(me.get("id")) != str(page):
            print(f"⚠ 和 config 的 page_id {page} 不一樣：請確認產生權杖時選的是這個粉專")
            return 1
        if cmd == "subscribe":
            r = fb_page._graph(f"{page}/subscribed_apps", {"subscribed_fields": FIELDS}) or {}
            print("訂閱：", "成功" if r.get("success") else r)
        subs = fb_page._graph(f"{page}/subscribed_apps", {}, method="GET") or {}
        for app in subs.get("data") or []:
            mark = "← 本系統" if str(app.get("id")) == str(config.FACEBOOK["app_id"]) else ""
            print(f"已訂閱的 app：{app.get('name')}（{app.get('id')}）{', '.join(app.get('subscribed_fields') or [])} {mark}")
        if not subs.get("data"):
            print("粉專還沒有訂閱任何 app：請執行 subscribe")
    except fb_page.GraphError as e:
        print("失敗：", e)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("check", "subscribe"):
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
