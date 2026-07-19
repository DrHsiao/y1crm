#!/usr/bin/env python3
"""
line_richmenu_setup.py — 建立/更新 LINE 圖文選單（Rich Menu）

- 以 PIL 生成 2500×1686 選單圖（3×2 六格，企業紅風格，Noto Sans CJK TC）
- 透過 Messaging API：清掉舊的同名選單 → 建立 → 上傳圖 → 設為全體預設
- 六顆按鈕全部走 postback（webhook 的 _handle_postback 已就緒），
  displayText 讓使用者看得到自己點了什麼。

重跑即重新佈署（改文案/版面後直接再執行一次）。
用法：venv/bin/python scripts/line_richmenu_setup.py
"""
import io
import json
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

TOKEN = config.LINE.get("channel_access_token") or ""
if not TOKEN:
    sys.exit("config [line].channel_access_token 未設定，無法佈署。")

W, H = 2500, 1686
COLS, ROWS = 3, 2
CW, CH = W // COLS, H // ROWS          # 833×843
BRAND = (200, 16, 46)                  # 企業紅 #c8102e
BRAND_DARK = (165, 13, 38)
TEXT = (32, 36, 43)
MUTED = (106, 114, 128)
LINE_C = (226, 229, 234)
MENU_NAME = "y1crm-main"

# (主標, 副標, action, displayText, 主格?)
CELLS = [
    ("我要預約", "線上預約來店服務", "booking",    "我要預約", True),
    ("查詢預約", "查看我的預約",     "mybookings", "查詢預約", False),
    ("常見問題", "電池・保養・保固", "faq",        "常見問題", False),
    ("最新活動", "優惠與公告",       "events",     "最新活動", False),
    ("門市資訊", "營業時間與位置",   "info",       "門市資訊", False),
    ("真人客服", "直接留言給門市",   "agent",      "真人客服", False),
]


def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    """從 Noto CJK ttc 挑出「Noto Sans CJK TC」（非 Mono）那個 face。"""
    path = f"/usr/share/fonts/opentype/noto/NotoSansCJK-{'Bold' if bold else 'Regular'}.ttc"
    for idx in range(12):
        try:
            f = ImageFont.truetype(path, size, index=idx)
        except OSError:
            break
        name = " ".join(f.getname())
        if "TC" in name and "Mono" not in name:
            return f
    return ImageFont.truetype(path, size)   # 後備：第一個 face


def build_image() -> bytes:
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f_big, f_sub = _font(120, True), _font(54, False)

    for i, (title, sub, _act, _dt, primary) in enumerate(CELLS):
        r, c = divmod(i, COLS)
        x0, y0 = c * CW, r * CH
        x1 = W if c == COLS - 1 else x0 + CW
        y1 = H if r == ROWS - 1 else y0 + CH
        if primary:
            d.rectangle([x0, y0, x1, y1], fill=BRAND)
            fg, sg = "white", (255, 220, 226)
        else:
            d.rectangle([x0, y0, x1, y1], fill="white", outline=LINE_C, width=3)
            fg, sg = TEXT, MUTED
            # 紅色小飾條（置中於主標上方）
            d.rounded_rectangle([x0 + CW // 2 - 70, y0 + 250, x0 + CW // 2 + 70, y0 + 272],
                                radius=11, fill=BRAND)
        tw = d.textlength(title, font=f_big)
        d.text((x0 + (CW - tw) / 2, y0 + 330), title, font=f_big, fill=fg)
        sw = d.textlength(sub, font=f_sub)
        d.text((x0 + (CW - sw) / 2, y0 + 515), sub, font=f_sub, fill=sg)
        if primary:   # 主格改白色飾條
            d.rounded_rectangle([x0 + CW // 2 - 70, y0 + 250, x0 + CW // 2 + 70, y0 + 272],
                                radius=11, fill="white")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    assert len(data) < 1_000_000, f"圖檔 {len(data)} bytes 超過 LINE 1MB 上限"
    return data


def api(method: str, url: str, body=None, ctype="application/json") -> dict:
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          **({"Content-Type": ctype} if data else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def main() -> None:
    # 1) 清掉舊的同名選單（可重複執行）
    for m in api("GET", "https://api.line.me/v2/bot/richmenu/list").get("richmenus", []):
        if m.get("name") == MENU_NAME:
            api("DELETE", f"https://api.line.me/v2/bot/richmenu/{m['richMenuId']}")
            print("已刪舊選單", m["richMenuId"])

    # 2) 建立選單（六格 postback）
    areas = []
    for i, (_t, _s, act, dtext, _p) in enumerate(CELLS):
        r, c = divmod(i, COLS)
        areas.append({
            "bounds": {"x": c * CW, "y": r * CH,
                       "width": W - c * CW if c == COLS - 1 else CW,
                       "height": H - r * CH if r == ROWS - 1 else CH},
            "action": {"type": "postback", "data": f"action={act}",
                       "displayText": dtext}})
    menu = {"size": {"width": W, "height": H}, "selected": True,
            "name": MENU_NAME, "chatBarText": "功能選單", "areas": areas}
    rid = api("POST", "https://api.line.me/v2/bot/richmenu", menu)["richMenuId"]
    print("已建立選單", rid)

    # 3) 上傳圖（注意：上傳走 api-data 網域）
    api("POST", f"https://api-data.line.me/v2/bot/richmenu/{rid}/content",
        build_image(), ctype="image/png")
    print("圖已上傳")

    # 4) 設為全體好友預設
    api("POST", f"https://api.line.me/v2/bot/user/all/richmenu/{rid}")
    print("已設為預設選單 ✅  richMenuId =", rid)


if __name__ == "__main__":
    main()
