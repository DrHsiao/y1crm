"""
article_video.py — 官網文章 → 照片短影音（John 2026-11-08「聽力專欄找一則來做個影片看看，直接在網站上看效果」）。

流程（全部在這台主機、只用 CPU，一支 40 秒左右約一兩分鐘）：
1. 本機 AI（config.AI＝573，純文字）把文章濃縮成 6～9 句旁白，每句附字幕重點和照片主題（ai_script）；
   AI 沒回應或寫出不能用的字眼，就用文章裡的條列（list_script）。
2. edge-tts 台灣口音語音（VOICE）。⚠ 旁白文字會送到微軟的語音服務：只送我們自己寫的文章，不送客人的任何資料。
3. 每句從圖庫挑一張真實照片（stock_art.pick；不挑要標作者的，影片裡不好標），裁成 16:9、緩慢推近或平移；
   畫面下方是旁白字幕、左上角是這句的重點；第一句配片頭（標題），最後一句配片尾（線上聽力測驗、線上預約）。
4. ffmpeg（imageio-ffmpeg 內附的）每段各自轉好再接起來：H.264＋AAC、faststart（網頁可以邊下載邊播）。
輸出 y1web media/uploads/videos/<內容雜湊>.mp4 和同名 .jpg（封面），記在文章的 extra.video。

    venv/bin/python -m api.article_video 42            # 做影片並放到文章上
    venv/bin/python -m api.article_video 42 --dry-run  # 只印旁白腳本（AI 擬的）
    venv/bin/python -m api.article_video 42 --script 改好的腳本.json   # 用人修過的旁白做
    venv/bin/python -m api.article_video 42 --tall     # 直式 9:16 版（Reels、Shorts）：旁白、照片和橫式那支一樣
AI 擬的旁白會漏點、偶爾用怪比喻（11-08 試做 #42 就漏了兩點），所以要人看過、改過再做影片。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import config
from api import fb_drafts, post_art, review_art, stock_art, web_admin

log = logging.getLogger("y1crm.video")

VOICE = "zh-TW-HsiaoChenNeural"        # 台灣口音女聲；長輩聽：語速放慢一點
RATE = "-8%"
W, H, FPS = 1920, 1080, 30
# 直式 9:16（John 11-08「做直式 9:16 版給 Reels」）。照片是橫的（3:2），裁成 9:16 會切掉六成，
# 所以照片放在中間 6:5 的框裡、同一張照片模糊當底；上面是標題，字幕放在 Reels 下方帳號／說明文字蓋住的範圍（約最下面 20%）之上
TW, TH = 1080, 1920
T_PHOTO_Y, T_PHOTO_H = 350, 900        # 照片框的上緣、高度（寬度滿版）
T_SUB_TOP, T_SUB_BOTTOM = 1290, 1540   # 字幕區
T_SUB_W = 780                          # 字幕寬度：置中、不到右邊（Reels 右側中下方有讚、留言、分享按鈕）
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_INDEX = 3                        # 這個 .ttc 裡第 3 個是 Noto Sans CJK TC（繁中字形）
BRAND = "韻譯助聽器・睿聲中正門市"
LEAD, TAIL = 0.35, 0.65               # 每段旁白前後留白（秒；語音本身前後的靜音已經裁掉）
AUDIO_RATE = 44100
MIN_LINES, MAX_LINES, MAX_SAY = 4, 10, 60
VIDEO_DIR = "videos"                  # y1web media/uploads 底下（config.WEB.media_dir），網址 /media/uploads/videos/…
VIDEO_RE = re.compile(r"^videos/[0-9a-f]{16}\.(mp4|jpg)$")

SCRIPT_PROMPT = """下面是助聽器門市官網的一篇文章。請改寫成 30～45 秒的短影片旁白，只回 JSON：
{"lines": [{"say": "旁白一句（口語、15～30 個字）", "caption": "畫面左上角的重點（12 個字以內）", "topic": "這句要配的照片主題，2～4 個詞，例如：看電視、家人聊天、餐廳聚會、講電話、聽力檢查"}]}
規則：
- 6～10 句；第一句點出主題；最後一句請觀眾「先在官網做線上聽力測驗，或線上預約到門市」；
- 文章有條列的話，每一點都要講到（相近的可以併成一句）；文章有「要就醫」的提醒，也要保留；
- 只講文章裡有的內容，不要加新的醫療說法；不要提價格、療效、「最」；助聽器不要叫耳機；
- 口語、溫和、平實，像門市人員對家屬說話；用台灣的說法；不要比喻、誇張的說法和表情符號。

標題：@TITLE@
內文：
@TEXT@"""


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: List[str]) -> None:
    r = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg 失敗：{r.stderr[-800:]}")


# ── 旁白腳本 ─────────────────────────────────────────────────

def clean_script(d: Any) -> List[Dict[str, str]]:
    """AI 回的 JSON → [{say, caption, topic}]；格式不對、句數不對、有不能用的字眼回 []。"""
    lines = d.get("lines") if isinstance(d, dict) else None
    if not isinstance(lines, list):
        return []
    out = []
    for x in lines:
        if not isinstance(x, dict):
            continue
        say = re.sub(r"\s+", "", str(x.get("say") or ""))[:MAX_SAY]
        if not say:
            continue
        out.append({"say": say, "caption": re.sub(r"\s+", " ", str(x.get("caption") or "")).strip()[:14],
                    "topic": str(x.get("topic") or "")[:30]})
    if not MIN_LINES <= len(out) <= MAX_LINES:
        return []
    errors, _ = fb_drafts.check("\n".join(x["say"] + x["caption"] for x in out))
    return [] if errors else out


def list_script(title: str, body: str) -> List[Dict[str, str]]:
    """AI 不能用時：標題＋文章的條列（最多 7 點）＋固定的結尾。"""
    items = [re.sub(r"\*+", "", m.group(1)).strip() for m in re.finditer(r"^\s*(?:\d+\.|[-*])\s+(.+)$", body or "", re.M)]
    lines = [{"say": title.replace("：", "，"), "caption": "", "topic": title}]
    lines += [{"say": x[:MAX_SAY], "caption": f"重點 {i}", "topic": x} for i, x in enumerate(items[:7], 1)]
    lines.append({"say": "想知道自己的聽力狀況，可以先在官網做線上聽力測驗，或線上預約到門市。",
                  "caption": "", "topic": "聽力檢查 預約"})
    return lines


def ai_script(title: str, body: str, timeout: float = 180) -> List[Dict[str, str]]:
    prompt = SCRIPT_PROMPT.replace("@TITLE@", title).replace("@TEXT@", post_art.plain(body)[:2500])
    try:
        raw = fb_drafts._chat([{"role": "user", "content": prompt}], timeout, json_mode=True, temperature=0.4,
                              num_predict=1500)
        return clean_script(json.loads(raw))
    except Exception as e:  # noqa: BLE001 — 改用條列
        log.warning("影片旁白：本機 AI 失敗：%s", e)
        return []


# ── 語音 ────────────────────────────────────────────────────

def tts(text: str, out: Path) -> float:
    """一句旁白 → out（WAV、AUDIO_RATE、前後的靜音裁掉，留白由 LEAD／TAIL 控制），回秒數。"""
    import edge_tts
    mp3 = out.with_suffix(".mp3")
    asyncio.run(edge_tts.Communicate(text, VOICE, rate=RATE).save(str(mp3)))
    trim = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05"
    _run(["-i", str(mp3), "-af", f"aresample={AUDIO_RATE},{trim},areverse,{trim},areverse", "-ac", "1",
          "-c:a", "pcm_s16le", str(out)])
    return (out.stat().st_size - 44) / 2 / AUDIO_RATE


# ── 畫面 ────────────────────────────────────────────────────

def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, size, index=FONT_INDEX)


def wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> List[str]:
    """中文逐字換行；標點不放行首。"""
    lines, cur = [], ""
    for ch in text:
        if f.getlength(cur + ch) > width and cur:
            if ch in "，。、！？；：」）":
                cur, ch = cur[:-1], cur[-1] + ch
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    return lines + ([cur] if cur else [])


def still(src: Path, dst: Path, focal: Tuple[float, float] = (0.5, 0.45), size: Tuple[int, int] = (W, H)) -> None:
    """照片 → 畫面比例、兩倍大（放大給 zoompan 取樣，推近時才不會抖）。"""
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    ImageOps.fit(im, (size[0] * 2, size[1] * 2), Image.LANCZOS, centering=focal).save(dst, quality=92)


def backdrop(src: Path, dst: Path) -> None:
    """直式的底：同一張照片填滿 9:16、模糊、變暗（照片框上下露出來的部分）。"""
    im = ImageOps.fit(ImageOps.exif_transpose(Image.open(src)).convert("RGB"), (TW, TH), Image.LANCZOS)
    im = Image.blend(im.filter(ImageFilter.GaussianBlur(36)), Image.new("RGB", (TW, TH), (20, 28, 40)), 0.55)
    im.save(dst, quality=90)


def _box(draw: ImageDraw.ImageDraw, xy, radius: int, alpha: int) -> None:
    draw.rounded_rectangle(xy, radius, fill=(0, 0, 0, alpha))


def overlay(say: str, label: str, dst: Path) -> None:
    """透明圖層：下方旁白字幕（最多兩行）、左上角重點標籤。"""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    f = font(54)
    rows = wrap(say, f, 1500)[:2]
    lh = 76
    top = H - 70 - lh * len(rows) - 28
    _box(d, (W / 2 - 800, top, W / 2 + 800, H - 60), 22, 150)
    for i, row in enumerate(rows):
        d.text((W / 2, top + 20 + i * lh), row, font=f, fill="white", anchor="ma")
    if label:
        fl = font(44)
        tw = fl.getlength(label)
        _box(d, (60, 56, 60 + tw + 56, 56 + 80), 18, 165)
        d.text((88, 70), label, font=fl, fill=(255, 214, 102))
    im.save(dst)


def card(bg: Path, big: List[str], small: List[str], dst: Path, size: Tuple[int, int] = (W, H)) -> None:
    """片頭、片尾：照片模糊變暗當底，中間大字、下面小字（兩倍大，和 still 一樣給 zoompan）。
    直式的字放在偏上（約 42%）：下面會被 Reels 的帳號和說明文字蓋住。"""
    w, h = size
    im = ImageOps.fit(Image.open(bg).convert("RGB"), (w * 2, h * 2), Image.LANCZOS)
    im = im.filter(ImageFilter.GaussianBlur(28))
    im = Image.blend(im, Image.new("RGB", im.size, (20, 28, 40)), 0.62)
    d = ImageDraw.Draw(im)
    fb, fs = font(150), font(84 if w > h else 80)
    big = [r for row in big for r in wrap(row, fb, w * 2 - 240)]
    total = len(big) * 200 + (60 + len(small) * 120 if small else 0)
    y = (h * 2 - total) / 2 if w > h else h * 2 * 0.42 - total / 2
    for row in big:
        d.text((w, y), row, font=fb, fill="white", anchor="ma")
        y += 200
    y += 60
    for row in small:
        d.text((w, y), row, font=fs, fill=(255, 214, 102), anchor="ma")
        y += 120
    im.save(dst, quality=92)


def overlay_tall(say: str, label: str, header: Optional[str], dst: Path) -> None:
    """直式的透明圖層：上方標題＋店名、照片框左上角的重點、照片下方的旁白字幕（最多 4 行，超出字幕區就往上長）。"""
    im = Image.new("RGBA", (TW, TH), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if header:
        d.text((TW / 2, 215), header, font=font(58), fill="white", anchor="ma")
        d.text((TW / 2, 292), BRAND, font=font(34), fill=(255, 214, 102), anchor="ma")
    if label:
        fl = font(42)
        tw = fl.getlength(label)
        _box(d, (40, T_PHOTO_Y + 30, 40 + tw + 52, T_PHOTO_Y + 30 + 76), 18, 165)
        d.text((66, T_PHOTO_Y + 43), label, font=fl, fill=(255, 214, 102))
    f, lh = font(54), 76
    rows = wrap(say, f, T_SUB_W)[:4]
    need = 44 + lh * len(rows)
    top = T_SUB_TOP if T_SUB_TOP + need <= T_SUB_BOTTOM else T_SUB_BOTTOM - need
    _box(d, (TW / 2 - T_SUB_W / 2 - 40, top, TW / 2 + T_SUB_W / 2 + 40, top + need), 22, 160)
    for i, row in enumerate(rows):
        d.text((TW / 2, top + 22 + i * lh), row, font=f, fill="white", anchor="ma")
    im.save(dst)


MOTIONS = (                           # zoompan：推近、拉遠、往右平移、往左平移（@N@＝這段的總影格數）
    ("1+0.10*on/@N@", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("1.10-0.10*on/@N@", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    ("1.08", "(iw-iw/zoom)*on/@N@", "ih/2-(ih/zoom/2)"),
    ("1.08", "(iw-iw/zoom)*(1-on/@N@)", "ih/2-(ih/zoom/2)"),
)


def segment(img: Path, layer: Optional[Path], n: int, motion: int, out: Path, size: Tuple[int, int] = (W, H),
            backdrop_img: Optional[Path] = None, at_y: int = 0, fade_in: bool = True) -> None:
    """一段的畫面（n 格、沒有聲音），先存成高畫質的暫存檔，最後整支一起編碼。
    size＝img 推近／平移後的大小；直式的照片框另外給 backdrop_img（整個畫面的底），照片疊在 at_y。
    fade_in＝False：第一段不從黑畫面淡入。Facebook Reels 拿第一格當封面，11-08 #42 在粉專就是一片黑。"""
    dur = n / FPS
    z, x, y = (e.replace("@N@", str(n)) for e in MOTIONS[motion % len(MOTIONS)])
    vf = f"[0:v]zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={size[0]}x{size[1]}:fps={FPS}[bg]"
    inputs, k, last = ["-i", str(img)], 1, "bg"         # k＝下一個輸入的編號
    if backdrop_img:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(backdrop_img)]
        vf += f";[{k}:v][bg]overlay=0:{at_y}:shortest=1[pic]"
        k, last = k + 1, "pic"
    if layer:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(layer)]
        vf += f";[{last}][{k}:v]overlay=0:0:shortest=1[ov]"
        last = "ov"
    vf += (f";[{last}]{'fade=t=in:st=0:d=0.3,' if fade_in else ''}fade=t=out:st={dur - 0.3:.3f}:d=0.3,"
           f"scale=out_range=tv,format=yuv420p[v]")
    _run([*inputs, "-filter_complex", vf, "-map", "[v]", "-frames:v", str(n), "-r", str(FPS),
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", str(out)])


def segment_audio(voice: Path, n: int, out: Path) -> None:
    """一段的聲音：前面留 LEAD 秒、補靜音到和畫面一樣長（精確到取樣點），存無壓縮 WAV，接起來才不會越差越多。"""
    samples = round(n / FPS * AUDIO_RATE)
    # 一定要先轉成 AUDIO_RATE 再照取樣點裁：edge-tts 的 mp3 是 24kHz，直接裁會照 24kHz 算，聲音變 1.84 倍長（11-08 踩過）
    _run(["-i", str(voice), "-af", f"aresample={AUDIO_RATE},adelay={int(LEAD * 1000)},apad,atrim=end_sample={samples},"
          f"afade=t=out:st={n / FPS - 0.15:.3f}:d=0.15", "-ar", str(AUDIO_RATE), "-ac", "1",
          "-c:a", "pcm_s16le", str(out)])


def encode(videos: List[Path], audios: List[Path], out: Path, work: Path) -> None:
    """整支一次編碼成網頁用的標準 MP4（2026-11-08 教訓：各段分別編碼再 -c copy 接起來，
    有些瀏覽器的硬體解碼會前段黑屏、聲音畫面對不上；顏色也要轉成 tv 範圍的 yuv420p）。"""
    vl, al = work / "v.txt", work / "a.txt"
    vl.write_text("".join(f"file '{p}'\n" for p in videos))
    al.write_text("".join(f"file '{p}'\n" for p in audios))
    _run(["-f", "concat", "-safe", "0", "-i", str(vl), "-f", "concat", "-safe", "0", "-i", str(al),
          "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
          "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
          "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
          "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE), "-ac", "1",
          "-movflags", "+faststart", "-shortest", str(out)])


# ── 組起來 ──────────────────────────────────────────────────

def site_host() -> str:
    with review_art._conn() as db, db.cursor() as cur:
        return re.sub(r"^https?://", "", fb_drafts.site_url(cur)).rstrip("/")


def photos(items: List[Dict[str, Any]], lines: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """每句一張照片：先照「照片主題」挑（主題完全對不到才看旁白，免得旁白裡順口的字配錯圖），
    中間各句先挑，片頭、片尾（照片會模糊當底）再從還沒用到的裡挑，不和中間重複。"""
    used: Dict[str, int] = {}
    out: List[Optional[Dict[str, Any]]] = [None] * len(lines)

    def one(i: int, avoid=()) -> None:
        ln = lines[i]
        topic = ln["topic"] or ln["say"]
        text = topic if any(stock_art.score(x, topic) for x in items) else stock_art.text_of(topic, ln["say"])
        e = stock_art.pick(items, text, used, avoid=avoid, allow_credit=False)
        used[e["file"]] = used.get(e["file"], 0) + 1
        out[i] = e

    for i in range(1, len(lines) - 1):
        one(i)
    for i in (0, len(lines) - 1):
        one(i, avoid=[x["file"] for x in out if x])
    return out


def build(title: str, lines: List[Dict[str, str]], work: Path, tall: bool = False,
          pics: Optional[List[Dict[str, Any]]] = None,
          hook: Optional[Tuple[List[str], List[str]]] = None) -> Tuple[Path, Path, float, List[Dict[str, Any]]]:
    """旁白腳本 → mp4、封面 jpg、秒數、每段用了哪張照片。tall＝直式 9:16；pics＝指定每句的照片（直式沿用橫式的）；
    hook＝(大字, 小字)：片頭不用文章標題，改用吸引人滑到就停下來的問句（John 11-08：開頭要有足夠吸引人的字眼）。"""
    media = Path(config.WEB["media_dir"])
    pics = pics or photos(stock_art.available(stock_art.load(), media), lines)
    host = site_host()
    size = (TW, TH) if tall else (W, H)
    heads = [x for x in re.split(r"[：:]", title) if x]
    parts, plan = [], []
    for i, ln in enumerate(lines):
        e = pics[i]
        voice = work / f"s{i}_voice.wav"
        n = max(FPS, round((tts(ln["say"], voice) + LEAD + TAIL) * FPS))   # 這段幾格（聲音照這個長度補齊）
        img, layer, back = work / f"s{i}.jpg", work / f"s{i}.png", None
        mid = 0 < i < len(lines) - 1
        if i == 0:
            card(media / e["file"], hook[0] if hook else heads[:2], (hook[1] if hook else []) + [BRAND], img, size)
        elif i == len(lines) - 1:
            card(media / e["file"], ["先做線上聽力測驗", "或線上預約到門市"],
                 [f"{host}/hearing-test", f"{host}/booking", BRAND], img, size)
        elif tall:
            still(media / e["file"], img, tuple(e.get("focal") or (0.5, 0.45)), (TW, T_PHOTO_H))
            back = work / f"s{i}_back.jpg"
            backdrop(media / e["file"], back)
        else:
            still(media / e["file"], img, tuple(e.get("focal") or (0.5, 0.45)))
        if i == len(lines) - 1:
            layer = None                                        # 片尾只有字卡
        elif tall:
            overlay_tall(ln["say"], ln["caption"] if i else "", heads[0] if mid else None, layer)
        else:
            overlay(ln["say"], ln["caption"] if i else "", layer)
        if back:
            segment(img, layer, n, i, work / f"s{i}.mp4", (TW, T_PHOTO_H), back, T_PHOTO_Y)
        else:
            segment(img, layer, n, i, work / f"s{i}.mp4", size, fade_in=i > 0)
        segment_audio(voice, n, work / f"s{i}.wav")
        parts.append((work / f"s{i}.mp4", work / f"s{i}.wav"))
        plan.append({"say": ln["say"], "caption": ln["caption"], "photo": e["id"], "sec": round(n / FPS, 2)})
    mp4 = work / "out.mp4"
    encode([v for v, _ in parts], [a for _, a in parts], mp4, work)
    poster = work / "poster.jpg"
    Image.open(work / "s0.jpg").resize(size, Image.LANCZOS).save(poster, quality=88)
    return mp4, poster, sum(x["sec"] for x in plan), plan


def make(post_id: int, actor: str = "影片", dry_run: bool = False,
         lines: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """做影片並記到文章的 extra.video（舊的影片檔沒人用就刪）。lines＝人修過的旁白（沒給就請 AI 擬）。"""
    with review_art._conn() as db, db.cursor() as cur:
        cur.execute(f"SELECT id, kind, title, summary, body_md, extra FROM {web_admin.TABLE} WHERE id=%s", (post_id,))
        r = cur.fetchone()
    if not r or r["kind"] != "article":     # 使用小教室的 extra.video 是表單上的「影片連結」（字串），官網也還沒有播放器
        raise ValueError("目前只有聽力專欄的文章可以做影片。")
    source = "edited"
    if lines is not None:
        lines = clean_script({"lines": lines})
        if not lines:
            raise ValueError("旁白腳本不能用（句數要 4～10 句，或有不能用的字眼）。")
    else:
        lines = ai_script(r["title"], "\n\n".join(x for x in (r["summary"] or "", r["body_md"] or "") if x))
        source = "ai"
    if not lines:
        lines, source = list_script(r["title"], r["body_md"] or ""), "list"
    if dry_run:
        return {"source": source, "lines": lines}
    media = Path(config.WEB["media_dir"])
    work = Path(tempfile.mkdtemp(prefix="y1video-"))
    try:
        t0 = time.time()
        mp4, poster, sec, plan = build(r["title"], lines, work)
        data = mp4.read_bytes()
        name = hashlib.sha256(data).hexdigest()[:16]
        (media / VIDEO_DIR).mkdir(exist_ok=True)
        (media / VIDEO_DIR / f"{name}.mp4").write_bytes(data)
        shutil.copy(poster, media / VIDEO_DIR / f"{name}.jpg")
        info = {"file": f"{VIDEO_DIR}/{name}.mp4", "poster": f"{VIDEO_DIR}/{name}.jpg", "sec": round(sec, 1),
                "voice": VOICE, "script": source, "at": review_art.now_str(), "plan": plan}
        old = (fb_drafts._json(r["extra"]).get("video") or {})
        with review_art._conn() as db, db.cursor() as cur:
            cur.execute(f"UPDATE {web_admin.TABLE} SET extra=JSON_SET(COALESCE(extra, JSON_OBJECT()), '$.video', "
                        "CAST(%s AS JSON)), updated_by=%s WHERE id=%s", (json.dumps(info, ensure_ascii=False), actor, post_id))
        tall = old.get("tall") if isinstance(old.get("tall"), dict) else {}    # 直式版是舊旁白做的，跟著作廢
        for f in (old.get("file"), old.get("poster"), tall.get("file"), tall.get("poster")):
            if f and VIDEO_RE.match(f) and f not in (info["file"], info["poster"]):
                (media / f).unlink(missing_ok=True)
        log.info("影片 #%s：%s（%.0f 秒，做了 %.0f 秒）", post_id, info["file"], sec, time.time() - t0)
        return {**info, "size": len(data), "took": round(time.time() - t0)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def save_files(media: Path, mp4: Path, poster: Path) -> Tuple[str, str, int]:
    data = mp4.read_bytes()
    name = hashlib.sha256(data).hexdigest()[:16]
    (media / VIDEO_DIR).mkdir(exist_ok=True)
    (media / VIDEO_DIR / f"{name}.mp4").write_bytes(data)
    shutil.copy(poster, media / VIDEO_DIR / f"{name}.jpg")
    return f"{VIDEO_DIR}/{name}.mp4", f"{VIDEO_DIR}/{name}.jpg", len(data)


def make_tall(post_id: int, actor: str = "影片", hook: Optional[Tuple[List[str], List[str]]] = None) -> Dict[str, Any]:
    """直式 9:16 版（Reels、Shorts）：旁白和每句的照片照橫式那支（extra.video.plan），記在 extra.video.tall。
    照片從圖庫拿掉了就重新挑。橫式重做時直式會被刪掉（make），要再做一次。hook：片頭的問句（見 build）。"""
    if hook:
        errors, warns = fb_drafts.check("\n".join(hook[0] + hook[1]))
        if errors or warns:
            raise ValueError("片頭字卡的用語要改：" + "；".join(errors + warns))
    with review_art._conn() as db, db.cursor() as cur:
        cur.execute(f"SELECT id, kind, title, extra FROM {web_admin.TABLE} WHERE id=%s", (post_id,))
        r = cur.fetchone()
    v = fb_drafts._json(r["extra"]).get("video") if r else None
    if not isinstance(v, dict) or not v.get("plan"):
        raise ValueError("這篇還沒有影片：先做橫式的。")
    media = Path(config.WEB["media_dir"])
    lines = [{"say": x["say"], "caption": x.get("caption") or "", "topic": ""} for x in v["plan"]]
    lib = {e["id"]: e for e in stock_art.available(stock_art.load(), media)}
    pics = [lib.get(x.get("photo")) for x in v["plan"]]
    work = Path(tempfile.mkdtemp(prefix="y1video-"))
    try:
        t0 = time.time()
        mp4, poster, sec, _ = build(r["title"], lines, work, tall=True, pics=pics if all(pics) else None, hook=hook)
        f, pf, size = save_files(media, mp4, poster)
        info = {"file": f, "poster": pf, "sec": round(sec, 1), "at": review_art.now_str(), "size": [TW, TH]}
        if hook:
            info["hook"] = hook[0] + hook[1]
        with review_art._conn() as db, db.cursor() as cur:       # 做的這段時間橫式被重做了就不記（舊旁白）
            cur.execute(f"UPDATE {web_admin.TABLE} SET extra=JSON_SET(extra, '$.video.tall', CAST(%s AS JSON)) "
                        "WHERE id=%s AND JSON_UNQUOTE(JSON_EXTRACT(extra, '$.video.file'))=%s",
                        (json.dumps(info, ensure_ascii=False), post_id, v["file"]))
            saved = cur.rowcount == 1
        old = v.get("tall") if isinstance(v.get("tall"), dict) else {}
        for k in ("file", "poster"):
            g = old.get(k) if saved else info[k]
            if g and VIDEO_RE.match(g) and g != (info[k] if saved else None):
                (media / g).unlink(missing_ok=True)
        if not saved:
            raise ValueError("做的時候橫式影片被重做了，請再做一次直式。")
        log.info("直式影片 #%s：%s（%.0f 秒，做了 %.0f 秒）", post_id, f, sec, time.time() - t0)
        return {**info, "bytes": size, "took": round(time.time() - t0)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pid = int(sys.argv[1])
    script = None
    if "--script" in sys.argv:
        script = json.loads(Path(sys.argv[sys.argv.index("--script") + 1]).read_text(encoding="utf-8"))
    hook = None
    if "--hook" in sys.argv:            # --hook "大字1|大字2" [--hook-sub "小字"]
        big = sys.argv[sys.argv.index("--hook") + 1].split("|")
        small = sys.argv[sys.argv.index("--hook-sub") + 1].split("|") if "--hook-sub" in sys.argv else []
        hook = (big, small)
    res = make_tall(pid, "Claude（影片試做）", hook) if "--tall" in sys.argv else \
        make(pid, "Claude（影片試做）", dry_run="--dry-run" in sys.argv, lines=script)
    print(json.dumps(res, ensure_ascii=False, indent=1))
