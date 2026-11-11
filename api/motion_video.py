"""
motion_video.py — 官網文章 → 動態影片（John 2026-11-08「直接拿目前的所有算力來做動態影片，找一篇現有的文章，
做生動一點的動態影片（不要純投影片的）。然後各平台發，包含官網」）。

和 api/article_video.py（每句一張照片＋緩慢推近）不同：
- 每一段有自己的畫面（plan 的 kind）：title＝片頭（助聽器嗶嗶叫的動畫）、feedback／domes＝圖解動畫、
  photo＝照片運鏡（GPU 估深度做視差：近的動得多、遠的動得少）、end＝片尾（預約、LINE 按鈕）；
- 字幕照語音服務給的每個詞的時間跳出來；有程式合成的音效（嗶嗶、轉場、圖示出現）。
- 畫面在 scripts/video/motion_render.py 畫（要 torch，借 config.IMAGEGEN.python），只用好卡 0002（573 不做影像）；
  和 SDXL 插圖共用 review_art.GEN_LOCK 排隊。
一次做出 16:9（官網、YouTube）和 9:16（Reels、Shorts），記在文章的 extra.video（直式在 extra.video.tall）。

    venv/bin/python -m api.motion_video 107 --plan scripts/video/plans/107.json

plan＝人寫、人看過的分鏡（旁白、每段的畫面、照片、照片上的焦點），旁白一樣要過用語檢查。
⚠ 旁白文字會送到微軟的語音服務：只送我們自己寫的稿子，沒有客人資料。
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from api import article_video as av
from api import fb_drafts, fb_page, post_art, review_art, stock_art, web_admin

log = logging.getLogger("y1crm.motion")

RENDER = Path(__file__).resolve().parent.parent / "scripts" / "video" / "motion_render.py"
DEPTH_MODEL = "depth-anything/Depth-Anything-V2-Small-hf"     # Apache-2.0
GOOD_GPU_SERIAL = "1422019000002"                              # 只用這張做影像（573 不做影像，見 SYSTEM_NOTES）
KINDS = ("title", "photo", "points", "feedback", "domes", "end")
KIND_LABELS = {"title": "片頭（大字問句）", "photo": "照片運鏡", "points": "重點清單", "feedback": "圖解：回饋音（嗶嗶聲）",
               "domes": "圖解：耳塞尺寸", "end": "片尾（預約、LINE）"}
DEFAULT_BUTTONS = [["線上預約到門市", "/booking"], ["LINE 諮詢", "/line"]]
# 每種畫面在旁白前後留多少秒：圖解要讓動畫跑完，片頭先嗶兩聲，片尾讓按鈕停一下
LEAD = {"title": 1.25, "feedback": 0.4, "domes": 0.4, "photo": 0.35, "points": 0.4, "end": 0.4}
TAIL = {"title": 0.7, "feedback": 1.1, "domes": 1.3, "photo": 0.6, "points": 1.2, "end": 2.6}
MOVES = ("in", "out", "left", "right")


def tts_words(text: str, out: Path) -> Tuple[float, List[List[Any]]]:
    """一句旁白 → out（WAV、44.1kHz），回 (秒數, [[開始, 結束, 詞], …])，時間從 WAV 開頭算。
    前後的靜音照第一個詞、最後一個詞的時間裁掉（不用 silenceremove：裁多少才知道字幕要往前挪多少）。"""
    import edge_tts

    async def run():
        mp3, words = bytearray(), []
        comm = edge_tts.Communicate(text, av.VOICE, rate=av.RATE, boundary="WordBoundary")
        async for ch in comm.stream():
            if ch["type"] == "audio":
                mp3 += ch["data"]
            elif ch["type"] == "WordBoundary":
                t0 = ch["offset"] / 1e7
                words.append([t0, t0 + ch["duration"] / 1e7, ch["text"]])
        return bytes(mp3), words

    mp3, words = asyncio.run(run())
    if not mp3 or not words:
        raise RuntimeError("語音服務沒有回聲音或字詞時間")
    src = out.with_suffix(".mp3")
    src.write_bytes(mp3)
    start = max(0.0, words[0][0] - 0.03)
    end = words[-1][1] + 0.15
    s0, s1 = round(start * av.AUDIO_RATE), round(end * av.AUDIO_RATE)
    av._run(["-i", str(src), "-af", f"aresample={av.AUDIO_RATE},atrim=start_sample={s0}:end_sample={s1},asetpts=N/SR/TB",
             "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    sec = (out.stat().st_size - 44) / 2 / av.AUDIO_RATE
    return sec, [[round(a - start, 3), round(b - start, 3), w] for a, b, w in words]


def load_plan(path: Path) -> Dict[str, Any]:
    """分鏡檔 → 檢查過的 plan。錯誤（格式、用語、照片）丟 ValueError。"""
    return check_plan(json.loads(path.read_text(encoding="utf-8")))


def _short(v: Any, n: int) -> List[str]:
    rows = [str(x).strip() for x in (v or []) if str(x).strip()]
    if any(len(x) > n for x in rows):
        raise ValueError(f"「{next(x for x in rows if len(x) > n)}」太長（{n} 字以內）。")
    return rows


def check_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """分鏡（CRM 表格存的、或分鏡檔）→ 檢查過、整理好的 plan。錯誤丟 ValueError（訊息給店員看）。"""
    if not isinstance(plan, dict):
        raise ValueError("分鏡格式不對。")
    plan = {**plan, "title": _short(plan.get("title"), 14)[:2], "header": str(plan.get("header") or "").strip()[:20]}
    if not plan["title"]:
        raise ValueError("請填片頭的大字（吸引人的問句）。")
    lines = plan.get("lines") or []
    if not 4 <= len(lines) <= 12:
        raise ValueError("分鏡要 4～12 段。")
    if lines[0].get("kind") != "title" or lines[-1].get("kind") != "end":
        raise ValueError("第一段要是 title（片頭）、最後一段要是 end（片尾）。")
    lib = {e["id"]: e for e in stock_art.available(stock_art.load(), Path(config.WEB["media_dir"]))}
    for i, ln in enumerate(lines, 1):
        if ln.get("kind") not in KINDS:
            raise ValueError(f"第 {i} 段的畫面類型不認得：{ln.get('kind')}")
        if not (ln.get("say") or "").strip() or len(ln["say"]) > 70:
            raise ValueError(f"第 {i} 段的旁白是空的或太長（70 字以內）。")
        e = lib.get(ln.get("photo"))
        if not e:
            raise ValueError(f"第 {i} 段的照片不在圖庫：{ln.get('photo')}")
        if e.get("credit_required"):
            raise ValueError(f"第 {i} 段的照片要標作者，影片裡不好標，請換一張。")
        if ln.get("move", "in") not in MOVES:
            raise ValueError(f"第 {i} 段的運鏡不認得：{ln.get('move')}")
        if ln["kind"] == "points":
            ln["items"] = _short(ln.get("items"), 14)
            if not 2 <= len(ln["items"]) <= 4:
                raise ValueError(f"第 {i} 段的重點清單要 2～4 點。")
        if ln["kind"] == "end":
            ln["cta"] = _short(ln.get("cta"), 16)[:2] or ["想了解更多？", "歡迎線上預約到門市"]
            ln["buttons"] = ln.get("buttons") or DEFAULT_BUTTONS
        if len(str(ln.get("label") or "")) > 14:
            raise ValueError(f"第 {i} 段的重點標籤太長（14 字以內）。")
    text = "\n".join(f"{ln['say']}{ln.get('label', '')}" for ln in lines) + "".join(
        "".join(ln.get("cta") or []) + "".join(ln.get("items") or []) for ln in lines) + "".join(plan["title"])
    errors, warns = fb_drafts.check(text)
    if errors or warns:
        raise ValueError("旁白用語要改：" + "；".join(errors + warns))
    return plan


PLAN_PROMPT = """下面是助聽器門市官網的一篇文章。請規劃一支 40～60 秒的短影片分鏡，只回 JSON：
{"title": ["開頭大字第一行：讓人想看下去的問句（10 個字以內）", "第二行：這支影片會講什麼（12 個字以內）"],
 "lines": [
  {"kind": "title", "say": "開場旁白（15～30 個字）", "topic": "照片主題"},
  {"kind": "photo", "say": "旁白（15～30 個字）", "label": "畫面上的重點（10 個字以內）", "topic": "照片主題，2～4 個詞，例如：看電視、家人聊天、聽力檢查、助聽器配戴、門市諮詢"},
  {"kind": "points", "say": "旁白", "label": "重點標籤", "items": ["重點一（10 個字以內）", "重點二", "重點三"], "topic": "照片主題"},
  {"kind": "end", "say": "結尾旁白（請觀眾回門市或線上預約）", "cta": ["片尾大字（10 個字以內）", "片尾小字（14 個字以內）"], "topic": "照片主題"}
 ]}
規則：
- 5～8 段；第一段 kind 是 title、最後一段是 end；中間主要用 photo；文章有條列的話用 1 段 points 整理（2～4 點）；
- 旁白連起來要講完文章的重點；文章有條列的話每一點都要講到；有「要就醫」的提醒也要保留；
- 只講文章裡有的內容，不要加新的醫療說法；不要提價格、療效、「最」；助聽器不要叫耳機；
- 口語、溫和、平實，像門市人員對家屬說話；用台灣的說法；不要比喻、誇張的說法和表情符號。

標題：@TITLE@
內文：
@TEXT@"""


def _cut(v: Any, n: int) -> str:
    """AI 寫太長的：先在逗號斷，還是太長就截掉。"""
    s = re.sub(r"\s+", "", str(v or ""))
    if len(s) <= n:
        return s
    head = re.split(r"[，、：]", s)[0]
    return head if 0 < len(head) <= n else s[:n]


def header_of(title: str) -> str:
    """直式影片上方的標題：文章標題的問句那一段（「助聽器出現嗶嗶聲怎麼辦？常見原因…」→ 前半句）。"""
    m = re.match(r"^(.+?[？?])", title or "")
    return (m.group(1) if m else re.split(r"[：:]", title or "")[0])[:20]


def assign_photos(lines: List[Dict[str, Any]]) -> None:
    """沒指定照片的段落照「照片主題」從圖庫挑（不挑要標作者的，盡量不重複）。"""
    items = [e for e in stock_art.available(stock_art.load(), Path(config.WEB["media_dir"])) if not e.get("credit_required")]
    used: Dict[str, int] = {}
    for ln in lines:
        if ln.get("photo") in {e["id"] for e in items}:
            used[next(e["file"] for e in items if e["id"] == ln["photo"])] = 1
    for ln in lines:
        if ln.get("photo") in {e["id"] for e in items}:
            continue
        e = stock_art.pick(items, stock_art.text_of(ln.get("topic") or "", ln.get("say") or ""), used, allow_credit=False)
        if e:
            used[e["file"]] = used.get(e["file"], 0) + 1
            ln["photo"] = e["id"]


def fallback_plan(post: Dict[str, Any]) -> Dict[str, Any]:
    """AI 不能用時：文章標題、摘要、條列整理成分鏡（店員再改）。"""
    body = post.get("body_md") or ""
    items = [re.sub(r"\*+", "", m.group(1)).strip() for m in re.finditer(r"^\s*(?:\d+\.|[-*])\s+(.+)$", body, re.M)]
    first = re.split(r"(?<=[。！？])", (post.get("summary") or post["title"]).strip())[0]
    lines = [{"kind": "title", "say": first[:60]}]
    if len(items) >= 2:
        lines.append({"kind": "points", "say": "重點整理給您參考。", "label": "重點整理",
                      "items": [_cut(x, 14) for x in items[:4]]})
    lines += [{"kind": "photo", "say": x[:60], "label": _cut(x, 10)} for x in items[:4]]
    lines.append({"kind": "end", "say": "有問題歡迎回門市，我們一起幫您看看。", "cta": ["想了解更多？", "歡迎線上預約到門市"]})
    return {"title": [_cut(post["title"], 14)], "lines": lines[:9]}


def draft_plan(post: Dict[str, Any]) -> Dict[str, Any]:
    """本機 AI（573，純文字）擬分鏡 → 整理、配照片。AI 失敗或寫出不能用的，用 fallback_plan。
    回 {plan, source（ai／list）, problem（AI 版為什麼不能用）}。"""
    text = "\n\n".join(x for x in (post.get("summary") or "", post_art.plain(post.get("body_md") or "")) if x)
    problem = ""
    try:
        raw = fb_drafts._chat([{"role": "user", "content": PLAN_PROMPT.replace("@TITLE@", post["title"])
                                .replace("@TEXT@", text[:2500])}], 240, json_mode=True, temperature=0.4, num_predict=2000)
        d = json.loads(raw)
        lines = []
        for x in d.get("lines") or []:
            if not isinstance(x, dict) or x.get("kind") not in ("title", "photo", "points", "end"):
                continue
            ln = {"kind": x["kind"], "say": re.sub(r"\s+", "", str(x.get("say") or ""))[:70],
                  "label": _cut(x.get("label"), 14) if x["kind"] in ("photo", "points") else "",
                  "topic": str(x.get("topic") or "")[:30]}
            if x["kind"] == "points":
                ln["items"] = [_cut(i, 14) for i in (x.get("items") or []) if str(i).strip()][:4]
            if x["kind"] == "end":
                ln["cta"] = [_cut(c, 16) for c in (x.get("cta") or []) if str(c).strip()][:2]
            lines.append(ln)
        plan = {"title": [_cut(t, 14) for t in (d.get("title") or []) if str(t).strip()][:2], "lines": lines}
        plan["header"] = header_of(post["title"])
        assign_photos(plan["lines"])
        return {"plan": check_plan(plan), "source": "ai", "problem": ""}
    except Exception as e:  # noqa: BLE001 — 改用條列
        problem = str(e)[:200]
        log.warning("影片分鏡：本機 AI 的不能用：%s", problem)
    plan = fallback_plan(post)
    plan["header"] = header_of(post["title"])
    assign_photos(plan["lines"])
    return {"plan": plan, "source": "list", "problem": problem}


def fb_message(post: Dict[str, Any], plan: Dict[str, Any], site: str) -> str:
    """影片發粉專的預設貼文：第一行是片頭的問句（John 11-08：開頭要有足夠吸引人的字眼）、摘要、完整文章、官網宣傳、hashtag。"""
    x = fb_drafts._json(post.get("extra"))
    hook = "".join(plan.get("title") or []) or post["title"]
    tags = [k.replace(" ", "") for k in (x.get("keywords") or []) if "台北" not in k and "中正" not in k][:2]
    link = fb_page.post_url(site, post.get("kind") or "article", post.get("slug") or "")
    body = "\n\n".join(b for b in (
        f"{hook} 🎬", (post.get("summary") or "").replace("本文", "這支影片"),
        f"👉 完整文章：{link}" if link else "",
        " ".join([fb_drafts.STORE_TAG] + [f"#{t}" for t in tags])) if b)
    return fb_drafts.add_footer(body, site)


def good_gpu() -> str:
    """好卡 0002 的 UUID（照序號找；bus 編號冷開機會變）。"""
    r = subprocess.run(["nvidia-smi", "--query-gpu=serial,uuid", "--format=csv,noheader"],
                       capture_output=True, text=True, timeout=20)
    for line in r.stdout.splitlines():
        serial, uuid = [x.strip() for x in line.split(",")]
        if serial == GOOD_GPU_SERIAL:
            return uuid
    raise RuntimeError("找不到好卡 0002")


def scene_sfx(ln: Dict[str, Any], lead: float, words: List[List[Any]]) -> List[List[Any]]:
    """音效時間（從這段開頭算）：嗶嗶聲那篇的片頭嗶兩次；耳塞、重點一個一個彈出來時「啵」。
    重點出現的時間要和 motion_render.point_times 一樣。"""
    if ln["kind"] == "title" and ln.get("icon") == "beep":
        return [["beep2", 0.15], ["beep2", 0.7]]
    if ln["kind"] == "domes":
        return [["pop", 0.2 + 0.22 * i] for i in range(3)]
    if ln["kind"] == "points":
        items = ln.get("items") or []
        speech = words[-1][1] if words else 3.0
        return [["pop", round(lead + speech * 0.88 * i / max(1, len(items)), 3)] for i in range(len(items))]
    return []


def build(post: Dict[str, Any], plan: Dict[str, Any], work: Path,
          progress: Optional[Callable[[str, float], None]] = None) -> Dict[str, Any]:
    """分鏡 → work/wide.mp4、tall.mp4、wide.jpg、tall.jpg；回每段的秒數（給 extra.video.plan）。
    progress(說明, 0–1)：CRM 顯示進度用。"""
    media = Path(config.WEB["media_dir"])
    lib = {e["id"]: e for e in stock_art.available(stock_art.load(), media)}
    say = progress or (lambda msg, frac: None)
    scenes, out_plan = [], []
    n_lines = len(plan["lines"])
    for i, ln in enumerate(plan["lines"]):
        say(f"旁白 {i + 1}／{n_lines}", 0.1 * i / n_lines)
        voice = work / f"v{i}.wav"
        sec, words = tts_words(ln["say"], voice)
        lead, tail = LEAD[ln["kind"]], TAIL[ln["kind"]]
        dur = round(lead + sec + tail, 3)
        e = lib[ln["photo"]]
        scenes.append({"kind": ln["kind"], "say": ln["say"], "label": ln.get("label", ""), "voice": str(voice),
                       "words": words, "lead": lead, "dur": dur, "photo": str(media / e["file"]),
                       "focus": ln.get("focus") or list(e.get("focal") or (0.5, 0.45)), "move": ln.get("move", "in"),
                       "ring": ln.get("ring"), "arrow": ln.get("arrow"), "cta": ln.get("cta"),
                       "buttons": ln.get("buttons"), "sfx": scene_sfx(ln, lead, words), "poster_at": lead + 0.3,
                       "icon": ln.get("icon"), "items": ln.get("items")})
        out_plan.append({"say": ln["say"], "caption": ln.get("label", ""), "kind": ln["kind"], "photo": ln["photo"],
                         "sec": dur})
    spec = {"fps": av.FPS, "ffmpeg": av.ffmpeg(), "font": av.FONT, "font_index": av.FONT_INDEX, "brand": av.BRAND,
            "host": av.site_host(), "header": plan.get("header") or post["title"],
            "title": plan.get("title") or [post["title"]], "depth_model": DEPTH_MODEL, "out_dir": str(work),
            "formats": ["wide", "tall"], "scenes": scenes}
    (work / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    c = config.IMAGEGEN
    env = {**os.environ, "HF_HOME": str(c.get("hf_home") or ""), "HF_HUB_OFFLINE": "1", "PYTHONUNBUFFERED": "1",
           "CUDA_VISIBLE_DEVICES": good_gpu()}
    say("排隊等顯卡", 0.1)
    with open(review_art.GEN_LOCK, "a") as lock:          # 和 SDXL 插圖排隊（0002 的記憶體不夠兩個同時）
        fcntl.flock(lock, fcntl.LOCK_EX)
        say("估照片深度", 0.12)
        err = work / "render.stderr"
        with open(err, "w") as ef:
            proc = subprocess.Popen([str(c["python"]), "-I", str(RENDER), str(work / "spec.json")],
                                    stdout=subprocess.PIPE, stderr=ef, text=True, env=env,
                                    preexec_fn=lambda: os.nice(10))
            done, total = 0, 2 * len(scenes)
            for line in proc.stdout:                       # 畫面每做完一段印一行 JSON
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if "scene" in ev:
                    done += 1
                    say(f"{'橫式' if ev.get('fmt') == 'wide' else '直式'}畫面 {ev['scene'] + 1}／{len(scenes)}",
                        0.15 + 0.85 * done / total)
            rc = proc.wait(timeout=3600)
    if rc:
        raise RuntimeError(f"動態影片畫面失敗：{err.read_text(errors='replace')[-1500:]}")
    return {"plan": out_plan, "sec": round(sum(s["dur"] for s in scenes), 1)}


def get_post(post_id: int) -> Dict[str, Any]:
    with review_art._conn() as db, db.cursor() as cur:
        cur.execute(f"SELECT id, kind, slug, title, summary, body_md, category, status, extra FROM {web_admin.TABLE} "
                    "WHERE id=%s", (post_id,))
        post = cur.fetchone()
    if not post or post["kind"] != "article":
        raise ValueError("目前只有聽力專欄的文章可以做影片。")
    return post


def install(post_id: int, work: Path, res: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """build 做好的 work/wide.mp4、tall.mp4 放到官網、記到文章（官網馬上換成新影片），舊的影片檔刪掉。
    YouTube 連結跟著拿掉：YouTube 上的是舊版，要重傳。"""
    post = get_post(post_id)
    media = Path(config.WEB["media_dir"])
    wf, wp, wsize = av.save_files(media, work / "wide.mp4", work / "wide.jpg")
    tf, tp, tsize = av.save_files(media, work / "tall.mp4", work / "tall.jpg")
    now = review_art.now_str()
    info = {"file": wf, "poster": wp, "sec": res["sec"], "voice": av.VOICE, "script": "edited", "at": now,
            "style": "motion", "plan": res["plan"],
            "tall": {"file": tf, "poster": tp, "sec": res["sec"], "at": now, "size": [av.TW, av.TH]}}
    old = fb_drafts._json(post["extra"]).get("video") or {}
    with review_art._conn() as db, db.cursor() as cur:
        cur.execute(f"UPDATE {web_admin.TABLE} SET extra=JSON_SET(COALESCE(extra, JSON_OBJECT()), '$.video', "
                    "CAST(%s AS JSON)), updated_by=%s WHERE id=%s", (json.dumps(info, ensure_ascii=False), actor, post_id))
    old_tall = old.get("tall") if isinstance(old.get("tall"), dict) else {}
    keep = {wf, wp, tf, tp}
    for f in (old.get("file"), old.get("poster"), old_tall.get("file"), old_tall.get("poster")):
        if f and av.VIDEO_RE.match(f) and f not in keep:
            (media / f).unlink(missing_ok=True)
    log.info("動態影片 #%s：%s／%s（%.0f 秒）", post_id, wf, tf, res["sec"])
    return {**info, "bytes": [wsize, tsize]}


def make(post_id: int, plan_path: Path, actor: str = "影片") -> Dict[str, Any]:
    """做影片並直接放上官網。想先看過再放：build() 到自己的資料夾，看完再 install()。"""
    post = get_post(post_id)
    plan = load_plan(plan_path)
    work = Path(tempfile.mkdtemp(prefix="y1motion-"))
    try:
        t0 = time.time()
        res = build(post, plan, work)
        return {**install(post_id, work, res, actor), "took": round(time.time() - t0)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pid = int(sys.argv[1])
    plan_file = Path(sys.argv[sys.argv.index("--plan") + 1])
    if "--check" in sys.argv:
        print(json.dumps(load_plan(plan_file), ensure_ascii=False, indent=1)[:2000])
        sys.exit(0)
    print(json.dumps(make(pid, plan_file, "Claude（動態影片）"), ensure_ascii=False, indent=1))
