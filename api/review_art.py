"""
review_art.py — 好評分享「評論轉貼」沒有圖片的，依評論文意畫一張示意插圖（John 2026-11-08）。

John：「沒有圖片的做一些吉卜力的圖，面容儘量在貼文中找，找不到用文意分析。」做法和界線：
- **只用文意**：本機 AI（config.AI＝573，純文字）讀評論，想一個溫馨的生活畫面（誰、在哪、做什麼），
  寫成英文的畫面描述；人物一律是虛構的。**不拿評論者的大頭照或評論照片來畫臉**——畫成卡通還是認得出來，
  等於未經同意使用別人的肖像（y1web docs/好評轉貼.md 早就定了「不抄大頭照」）。
- **風格**：溫暖手繪動畫、水彩背景（scripts/illustrate/gen_art.py 的 STYLE）。不寫任何動畫公司或導演的名字，
  不用拿特定作品畫面訓練的模型（主機上有一個 Ghibli-Diffusion 的快取，不用它）。
- **標示**：extra.cover_ai＝True（cover_src＝"ai"）。原本官網卡片和彈窗標「示意插圖」，John 2026-11-08 改成畫面上不標，
  「我們可以識別就好」：只留在後台、官網的 data-ai 和圖片替代文字。
- 畫圖跑在 0002（gen_art.py 以序號認卡，cpu_offload 約 5GB，可以和常駐的 OCR 模型共存），一張約 15–20 秒，
  一批只載入一次模型；同一時間只跑一批（_lock）。
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import pymysql
import pymysql.cursors

import config
from api import dbpool, web_admin

log = logging.getLogger("y1crm.art")

GEN_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "illustrate" / "gen_art.py"
# 畫圖的模型和畫風版本：記在每張 AI 圖上（extra.cover_model、fb_post_drafts.image_meta.model）。
# 換模型、或改了 gen_art.py 的 STYLE／NEG，就換這個版號，之後可以挑舊版的批次重畫（image_sources.py）。
ART_MODEL = "sdxl-base-1.0/watercolor-v1"
GEN_LOCK = Path(tempfile.gettempdir()) / "y1crm-gen-art.lock"
MAX_IDS = 30
MAX_WORDS = 45          # SDXL 只讀前 77 個 token，風格描述已經用掉約 25 個
FALLBACK = ("a smiling elderly East Asian woman sitting and chatting with a young woman staff in her 20s with long "
            "black hair in a light blue uniform across a white counter in a bright modern shop")
DEFAULT_PEOPLE = "一位長輩客人和一位親切的門市人員"
# 評論太短、看不出畫面裡有誰（模型回 DEFAULT_PEOPLE）時，依文章 id 輪流用這幾個場景，免得每張都長一樣
DEFAULT_SCENES = (
    ("七十多歲的阿嬤和年輕的女店員", FALLBACK),
    ("一對長輩夫妻和年輕的女店員",
     "an elderly East Asian couple in their 70s sitting side by side and smiling, a young woman staff in her 20s in a "
     "light blue uniform explaining kindly at a white counter in a bright modern shop"),
    ("七十多歲的父親和四十多歲的女兒",
     "an elderly East Asian man in his 70s and his daughter in her 40s smiling together while talking with a young "
     "woman staff at a white counter in a bright modern shop"),
    ("阿嬤在家和孫子聊天",
     "an elderly East Asian grandmother laughing while chatting with her young grandson on a sofa in a cozy bright "
     "living room, afternoon sunlight"),
    ("阿公和家人一起吃飯",
     "an elderly East Asian man in his 70s happily talking with his family around a dinner table at home, warm "
     "evening light"),
    ("長輩夫妻在公園散步聊天",
     "an elderly East Asian couple walking and chatting happily in a sunny city park with trees"),
)

SCENE_PROMPT = """下面是一則客人在 Google 地圖上給助聽器門市的好評。請想像一張溫馨的「示意插圖」，只回 JSON：
{"people": "中文，畫面裡有誰：依評論文意推測年齡、性別、關係（例如：七十多歲的父親和四十多歲的女兒、一位門市人員）；看不出來就寫「一位長輩客人和一位親切的門市人員」",
 "prompt": "英文的畫面描述，25～40 個英文字，越短越好：每個人的年齡（例如 a woman in her 40s、an elderly man in his 70s）、East Asian、表情和動作，加上地點。門市請寫 a bright modern shop with a white counter（不要植物、貨架、商品）；門市人員寫 a young woman staff in a light blue uniform；年輕的人要寫 young，免得都畫成長輩。也可以是家裡客廳、餐桌、公園"}
規則：
- 人物都是虛構的，不要寫任何名字；
- 畫面裡不要有文字、招牌、品牌，不要特寫耳朵或助聽器（可以畫成在門市聊天、全家吃飯、一起散步）；
- 正面、溫暖的日常生活；不要醫院、病床、哭泣或悲傷的畫面。

評論：
@TEXT@"""

_lock = threading.Lock()
status: Dict[str, Any] = {"running": False, "total": 0, "done": 0, "errors": [], "finished_at": None}


def _conn():
    return dbpool.connect(**config.db_params(), cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def enabled() -> bool:
    c = getattr(config, "IMAGEGEN", {}) or {}
    return bool(c.get("enabled")) and Path(str(c.get("python") or "")).is_file() and GEN_SCRIPT.is_file()


def clean_prompt(v: Any) -> str:
    """模型寫的英文描述：只留英文、數字和一般標點，截長度（會原樣進 SDXL）。"""
    t = re.sub(r"[^A-Za-z0-9 ,.;:'()\-]", " ", str(v or ""))
    words = re.sub(r"\s+", " ", t).strip(" ,.").split(" ")
    return " ".join(words[:MAX_WORDS]).strip(" ,.") if len(words) >= 6 else ""


def ask_json(prompt: str, timeout: float = 120) -> Dict[str, Any]:
    """請本機 AI 想畫面，回 JSON（dict）；失敗回 {}。⚠ 純文字送 config.AI，永不帶 images。"""
    payload = {"model": config.AI.get("model"), "stream": False, "think": False, "keep_alive": -1, "format": "json",
               "options": {"temperature": 0.4, "num_predict": 400, "num_ctx": 8192},   # 跟常駐模型同一個 num_ctx，不同會逼 ollama 重載整顆模型
               "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(config.AI.get("url", "").rstrip("/") + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads((json.loads(r.read()).get("message") or {}).get("content") or "{}")
    except Exception as e:  # noqa: BLE001 — 想不出畫面就用通用場景
        log.warning("插圖：畫面描述失敗：%s", e)
        d = {}
    return d if isinstance(d, dict) else {}


def scene(original: str, timeout: float = 120, variant: int = 0) -> Dict[str, str]:
    """評論 → {people（給店員看）, prompt（英文，給畫圖）}。
    看不出人物時用 DEFAULT_SCENES[variant]（呼叫端傳文章 id）。"""
    d = ask_json(SCENE_PROMPT.replace("@TEXT@", (original or "")[:1500]), timeout)
    people, prompt = str(d.get("people") or "").strip()[:100], clean_prompt(d.get("prompt"))
    if not prompt or not people or people == DEFAULT_PEOPLE:
        people, prompt = DEFAULT_SCENES[variant % len(DEFAULT_SCENES)]
    return {"people": people, "prompt": prompt}


def run_gen(jobs: List[Dict[str, Any]], tmp: Path) -> Dict[str, Path]:
    """呼叫 gen_art.py（另一個 Python 環境），回 {key: 圖檔}（放在 tmp 裡，用完由呼叫端刪）。整批只載入一次模型。"""
    c = config.IMAGEGEN
    (tmp / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "HF_HOME": str(c.get("hf_home") or ""), "HF_HUB_OFFLINE": "1", "PYTHONUNBUFFERED": "1"}
    with open(GEN_LOCK, "a") as lock:             # 跨行程排隊：CRM 和批次腳本同時畫，0002 的記憶體會不夠
        fcntl.flock(lock, fcntl.LOCK_EX)
        r = subprocess.run([str(c["python"]), "-I", str(GEN_SCRIPT), str(tmp / "jobs.json"), str(tmp / "out")],
                           capture_output=True, text=True, env=env, timeout=180 + 90 * len(jobs),
                           preexec_fn=lambda: os.nice(10))
    out: Dict[str, Path] = {}
    for line in r.stdout.splitlines():
        if line.startswith("{"):
            try:
                d = json.loads(line)
                out[str(d["key"])] = Path(d["file"])
            except (ValueError, KeyError):
                pass
    if r.returncode != 0:
        log.warning("示意插圖：gen_art.py 結束碼 %s：%s", r.returncode, r.stderr[-500:])
    return out


def _targets(cur, ids: Optional[Sequence[int]], force: bool) -> List[Dict[str, Any]]:
    cur.execute(f"SELECT id, cover, extra FROM {web_admin.TABLE} WHERE kind='share' ORDER BY id")
    out = []
    for r in cur.fetchall():
        x = json.loads(r["extra"] or "{}") if isinstance(r["extra"], (str, bytes)) else (r["extra"] or {})
        if x.get("format") != "quote" or not x.get("original"):
            continue
        if ids is not None and r["id"] not in ids:
            continue
        if r["cover"] and not (force and x.get("cover_ai")):     # 店員自己放的圖（評論照片等）一律不動
            continue
        if ids is None and x.get("review_photos"):              # 評論附了照片：等店員問過後上傳照片
            continue
        out.append({"id": r["id"], "original": x["original"]})
    return out[:MAX_IDS]


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def set_cover(cur, media: Path, post_id: int, name: str, prompt: str, people: str, actor: str,
              model: str = "", at: str = "") -> None:
    """官網內容（web_posts）的圖片換成 AI 畫的 name，標成 AI 生成、記下模型版本、時間和畫面描述；
    換掉的舊圖沒人用就刪檔。model／at 沿用別處畫好的（粉專草稿的圖同步到專欄時）。"""
    old = web_admin.get_post(cur, post_id)
    cur.execute(f"UPDATE {web_admin.TABLE} SET cover=%s, updated_by=%s, "
                "extra=JSON_SET(COALESCE(extra, JSON_OBJECT()), '$.cover_ai', CAST('true' AS JSON), '$.cover_src', 'ai', "
                "'$.cover_model', %s, '$.cover_at', %s, '$.cover_prompt', %s, '$.cover_people', %s) WHERE id=%s",
                (name, actor, model or ART_MODEL, at or now_str(), prompt[:300], people, post_id))
    if old and old["cover"] and old["cover"] != name:
        web_admin.remove_if_unused(cur, media, old["cover"])


def illustrate(ids: Optional[Sequence[int]], actor: str, force: bool = False) -> Dict[str, Any]:
    """畫圖並設成那則的圖片（同步執行，給背景執行緒用）。ids＝None：所有沒圖片的評論轉貼。
    force：已經是 AI 示意插圖的重畫一張（換 seed）。"""
    media = Path(config.WEB["media_dir"])
    with _conn() as db, db.cursor() as cur:
        targets = _targets(cur, ids, force)
    status.update(running=True, total=len(targets), done=0, errors=[], finished_at=None)
    tmp = Path(tempfile.mkdtemp(prefix="y1art-"))
    try:
        jobs = []
        for t in targets:
            sc = scene(t["original"], variant=t["id"] + (int(time.time()) if force else 0))
            t.update(sc)
            seed = (t["id"] * 7919 + (int(time.time()) if force else 0)) % 2_000_000_000
            jobs.append({"key": str(t["id"]), "prompt": sc["prompt"], "seeds": [seed]})
        files = run_gen(jobs, tmp) if jobs else {}
        for t in targets:
            f = files.get(str(t["id"]))
            if not f or not f.is_file():
                status["errors"].append(f"#{t['id']} 沒畫出來")
                continue
            name = web_admin.save_image(media, f.read_bytes(), thumb=True)
            with _conn() as db, db.cursor() as cur:
                set_cover(cur, media, t["id"], name, t["prompt"], t["people"], actor)
            status["done"] += 1
            log.info("示意插圖 #%s：%s｜%s", t["id"], t["people"], t["prompt"][:120])
    finally:
        status.update(running=False, finished_at=time.strftime("%Y-%m-%d %H:%M"))
        shutil.rmtree(tmp, ignore_errors=True)
    return dict(status)


def run_bg(fn: Callable[[], Any], name: str) -> bool:
    """背景畫圖（好評示意插圖、貼文配圖共用一張卡、一個進度 status）；已經有一批在跑就回 False。"""
    if not _lock.acquire(blocking=False):
        return False

    def _run() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("插圖失敗（%s）", name)
            status.update(running=False, finished_at=time.strftime("%Y-%m-%d %H:%M"))
            status["errors"].append("畫圖程式出錯，請看伺服器紀錄")
        finally:
            _lock.release()

    status.update(running=True, total=0, done=0, errors=[])
    threading.Thread(target=_run, daemon=True, name=name).start()
    return True


def start(ids: Optional[Sequence[int]], actor: str, force: bool = False) -> bool:
    """背景畫好評示意插圖；已經有一批在跑就回 False。"""
    return run_bg(lambda: illustrate(ids, actor, force), "review-art")
