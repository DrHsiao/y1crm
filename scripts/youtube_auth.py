#!/usr/bin/env python3
"""
youtube_auth.py — YouTube 頻道授權（只做一次）與檢查。config.toml [youtube] 的 client_id、client_secret 填好之後跑：

    sudo venv/bin/python scripts/youtube_auth.py login    # 印出 Google 登入網址 → 選門市頻道 → 把跳轉後的網址貼回來
    sudo venv/bin/python scripts/youtube_auth.py check    # 授權還有效嗎、是哪個頻道、頻道設定有沒有做好

授權或檢查成功時，順便把頻道網址寫給官網（頁尾、服務據點的 YouTube 圖示）。

要在自己的終端機跑（不要用 Claude 對話框的 !：要互動輸入，授權碼也不該留在對話紀錄）。
長期授權（refresh_token）只寫進 config.toml，不會印出來。
"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from api import review_art, web_admin, youtube  # noqa: E402


def show(ch: dict) -> int:
    """印出頻道資料和設定檢查，回傳要注意的項目數。"""
    print(f"頻道：{ch['title']}（{ch.get('handle') or '還沒有帳號代號'}）")
    print(f"網址：{ch['url']}")
    warn = 0
    if ch.get("long_uploads") == "allowed":
        print("✓ 已做電話驗證")
    elif ch.get("long_uploads"):
        warn += 1
        print("⚠ 還沒做電話驗證：YouTube 工作室 → 設定 → 頻道 → 功能使用資格 → 中級功能")
    if ch.get("made_for_kids"):
        warn += 1
        print("⚠ 頻道設成「為兒童打造」：設定 → 頻道 → 進階設定 → 觀眾，改成「否」")
    elif ch.get("made_for_kids") is False:
        print("✓ 觀眾設定：不是為兒童打造")
    return warn


def sync_site(channel_id: str) -> None:
    """官網頁尾、服務據點的 YouTube 圖示連到這個頻道（y1web.web_settings k='youtube'）。"""
    try:
        with review_art._conn() as db, db.cursor() as cur:
            print("官網的 YouTube 連結：", web_admin.set_youtube_channel(cur, channel_id, "youtube_auth"))
    except Exception as e:  # noqa: BLE001 — 授權本身已經完成，官網連結之後 check 會再補
        print("⚠ 官網的 YouTube 連結沒寫進去：", e)


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def login() -> int:
    if config.YOUTUBE.get("refresh_token") and ask("已經授權過了，要重新授權嗎？(y/N) ").lower() != "y":
        return 0
    verifier, challenge = youtube.pkce()
    state = secrets.token_urlsafe(16)
    print("\n1. 用電腦瀏覽器打開下面這個網址，登入擁有門市頻道的 Google 帳號：\n")
    print(youtube.auth_url(state, challenge))
    print(f"""
2. 選頻道時選「門市的品牌頻道」，不要選個人頻道。
3. 出現「Google 尚未驗證這個應用程式」→ 按「進階」→「前往…（不安全）」→ 權限全部勾選 → 繼續。
4. 瀏覽器會跳到 {youtube.REDIRECT_URI}?… 並顯示「無法連線」，這是正常的：
   把網址列的整串複製，貼到下面（10 分鐘內有效）。
""")
    pasted = ask("貼上網址：")
    if not pasted:
        print("沒有貼上網址，已取消")
        return 1
    try:
        tok = youtube.exchange(youtube.parse_redirect(pasted, state), verifier)
    except youtube.YouTubeError as e:
        print("授權失敗：", e)
        return 1
    rt = tok.get("refresh_token")
    missing = youtube.missing_scopes(tok.get("scope", ""))
    if missing or not rt:
        youtube.revoke(rt or tok.get("access_token", ""))
        print("⚠ 權限沒有全部勾選，請重跑 login，勾選全部權限" if missing
              else "⚠ Google 沒有給長期授權，請重跑 login")
        return 1
    try:
        ch = youtube.my_channel(tok["access_token"])
    except youtube.YouTubeError as e:
        youtube.revoke(rt)
        print("查不到頻道：", e)
        return 1
    if not ch:
        youtube.revoke(rt)
        print("⚠ 這個帳號（或選的身分）沒有 YouTube 頻道：請重跑 login，選門市的品牌頻道")
        return 1
    print()
    show(ch)
    prev = config.YOUTUBE.get("channel_id")
    if prev and ch["id"] != prev:
        # 11-08 事故：重新授權時選到個人頻道，又回答 y → 官網的 YouTube 圖示連到個人頻道、門市影片被判成「不是門市頻道的」
        try:
            old = youtube.get("channels", {"part": "snippet", "id": prev}, tok["access_token"]).get("items") or []
            old_name = old[0]["snippet"]["title"] if old else prev
        except youtube.YouTubeError:
            old_name = prev
        print(f"\n⚠⚠ 這不是原本的門市頻道！原本：{old_name}（{prev}）／這次選的：{ch['title']}（{ch['id']}）")
        print("   多半是選頻道那一步選到個人頻道了。真的要把門市頻道換成這個，請輸入「換頻道」；其他任何輸入都會取消。")
        if ask("> ") != "換頻道":
            youtube.revoke(rt)
            print("已撤銷這次授權，設定沒有改。請重跑 login，選頻道時選「" + str(old_name) + "」")
            return 1
    elif ask("\n這是門市的頻道嗎？(y/N) ").lower() != "y":
        youtube.revoke(rt)
        print("已撤銷這次授權。請重跑 login，在選頻道那一步選門市的品牌頻道")
        return 1
    config.save_keys("youtube", {"refresh_token": rt, "channel_id": ch["id"]})
    print("\n✓ 授權完成，已存進 config.toml（長期授權不會顯示）。")
    sync_site(ch["id"])
    print("提醒：YouTube API 稽核通過前，程式上傳的影片會被鎖成私人；在那之前影片請在 YouTube 工作室手動上傳。")
    return 0


def check() -> int:
    if not config.YOUTUBE.get("refresh_token"):
        print("還沒授權：sudo venv/bin/python scripts/youtube_auth.py login")
        return 1
    try:
        ch = youtube.my_channel()
    except youtube.YouTubeError as e:
        print("授權失效或連不上 Google：", e)
        print("invalid_grant＝授權被移除或半年沒用 → 重跑 login")
        return 1
    if not ch:
        print("⚠ 授權的帳號沒有 YouTube 頻道 → 重跑 login，選門市的品牌頻道")
        return 1
    show(ch)
    missing = youtube.missing_scopes(" ".join(youtube.granted()))
    if missing:
        print("⚠ 授權少了這些權限（程式加了新功能）：", "、".join(m.rsplit("/", 1)[-1] for m in missing))
        print("  → Google Cloud「資料存取權」加上這些範圍後，重跑 login")
        return 1
    if config.YOUTUBE.get("channel_id") and ch["id"] != config.YOUTUBE["channel_id"]:
        print(f"⚠ 和 config.toml 記的頻道（{config.YOUTUBE['channel_id']}）不一樣")
        return 1
    print("✓ 授權有效")
    sync_site(ch["id"])
    return 0


def main(cmd: str) -> int:
    if not youtube.configured():
        print("config.toml [youtube] 還沒填 client_id、client_secret")
        return 1
    return login() if cmd == "login" else check()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("login", "check"):
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
