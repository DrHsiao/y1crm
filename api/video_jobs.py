"""
video_jobs.py — CRM「做影片」的背景工作（John 2026-11-08「CRM 加做影片和發粉專的按鈕」）。

api/motion_video.build 要 5 分鐘左右，網頁不能一直等：按「產生影片」只是開一個背景工作，畫面每幾秒問一次進度。
- 一次只做一支（顯卡還和 SDXL 插圖共用 review_art.GEN_LOCK）；
- 做好的影片先放在暫存資料夾（<base_dir>/video_jobs/<文章 id>-<時間>/）給店員預覽，按「放上官網」才 install，
  不滿意可以捨棄、改分鏡再做；
- 狀態存在 status.json。CRM 重新啟動時還在「做」的標成失敗（行程已經沒了）；暫存放超過 3 天的刪掉。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import config
from api import motion_video

log = logging.getLogger("y1crm.video")

JOB_RE = re.compile(r"^(\d+)-(\d{14})$")
KEEP_DAYS = 3
_lock = threading.Lock()
_running: Optional[str] = None


class Busy(RuntimeError):
    pass


def root() -> Path:
    return Path(config.STORAGE["base_dir"]) / "video_jobs"


def _dir(job_id: str) -> Path:
    if not JOB_RE.match(job_id or ""):
        raise KeyError(job_id)
    d = root() / job_id
    if not (d / "status.json").is_file():
        raise KeyError(job_id)
    return d


def _write(d: Path, **kw: Any) -> Dict[str, Any]:
    p = d / "status.json"
    st = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    st.update(kw, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
    tmp = d / "status.json.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return st


def status(job_id: str) -> Dict[str, Any]:
    d = _dir(job_id)
    st = json.loads((d / "status.json").read_text(encoding="utf-8"))
    return {**st, "id": job_id}


def latest(post_id: int) -> Optional[Dict[str, Any]]:
    """這篇文章最近一個還沒放上官網、也還沒捨棄的工作（重新打開視窗時接著看進度或預覽）。"""
    if not root().is_dir():
        return None
    ids = sorted((p.name for p in root().iterdir() if p.name.startswith(f"{post_id}-") and JOB_RE.match(p.name)),
                 reverse=True)
    for i in ids:
        try:
            st = status(i)
        except (KeyError, ValueError):
            continue
        if st.get("state") in ("queued", "running", "done", "failed"):
            return st
    return None


def start(post_id: int, plan: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """檢查分鏡、開背景工作。已經有一支在做就丟 Busy。"""
    global _running
    plan = motion_video.check_plan(plan)
    post = motion_video.get_post(post_id)
    with _lock:
        if _running:
            raise Busy("已經有一支影片在做，請等它做完再開始。")
        job_id = f"{post_id}-{time.strftime('%Y%m%d%H%M%S')}"
        d = root() / job_id
        d.mkdir(parents=True, exist_ok=False)
        (d / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        _write(d, state="queued", post_id=post_id, title=post["title"], by=actor, step="準備中", progress=0.0,
               started=time.strftime("%Y-%m-%d %H:%M:%S"))
        _running = job_id
    threading.Thread(target=_run, args=(job_id, post, plan), daemon=True, name=f"video-{job_id}").start()
    log.info("影片工作 %s 開始（#%s，%s）", job_id, post_id, actor)
    return status(job_id)


def _run(job_id: str, post: Dict[str, Any], plan: Dict[str, Any]) -> None:
    global _running
    d = root() / job_id
    t0 = time.time()
    try:
        _write(d, state="running")
        res = motion_video.build(post, plan, d, progress=lambda step, frac: _write(d, step=step, progress=round(frac, 3)))
        (d / "res.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        for f in d.glob("v*.wav"):                    # 旁白暫存檔做完就不用了
            f.unlink(missing_ok=True)
        final = {"state": "done", "step": "做好了，請預覽", "progress": 1.0, "sec": res["sec"],
                 "took": round(time.time() - t0)}
        log.info("影片工作 %s 做好（%.0f 秒，花了 %.0f 秒）", job_id, res["sec"], time.time() - t0)
    except Exception as e:  # noqa: BLE001 — 失敗寫進狀態給畫面看
        log.exception("影片工作 %s 失敗", job_id)
        final = {"state": "failed", "step": "失敗", "error": str(e)[-600:]}
    with _lock:                                       # 先放掉「正在做」，再寫結果：畫面看到做完時，一定可以開始下一支
        _running = None
    _write(d, **final)


def file_path(job_id: str, name: str) -> Path:
    """預覽用的檔案（wide／tall 的 mp4、jpg）。"""
    if name not in ("wide.mp4", "tall.mp4", "wide.jpg", "tall.jpg"):
        raise KeyError(name)
    p = _dir(job_id) / name
    if not p.is_file():
        raise KeyError(name)
    return p


def install(job_id: str, actor: str) -> Dict[str, Any]:
    """預覽過、按「放上官網」：換掉文章的影片（舊檔刪掉、YouTube 連結跟著拿掉），暫存資料夾刪掉。"""
    d = _dir(job_id)
    st = status(job_id)
    if st.get("state") != "done":
        raise ValueError("影片還沒做好。")
    res = json.loads((d / "res.json").read_text(encoding="utf-8"))
    info = motion_video.install(int(st["post_id"]), d, res, actor)
    shutil.rmtree(d, ignore_errors=True)
    log.info("影片工作 %s 放上官網（%s）", job_id, actor)
    return info


def discard(job_id: str) -> None:
    d = _dir(job_id)
    if status(job_id).get("state") in ("queued", "running"):
        raise ValueError("影片還在做，等做完再捨棄。")
    shutil.rmtree(d, ignore_errors=True)


def cleanup() -> None:
    """CRM 啟動時：還在做的標成失敗（行程沒了），放超過 KEEP_DAYS 天的刪掉。"""
    if not root().is_dir():
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for d in root().iterdir():
        if not JOB_RE.match(d.name):
            continue
        p = d / "status.json"
        if not p.is_file() or p.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            continue
        st = json.loads(p.read_text(encoding="utf-8"))
        if st.get("state") in ("queued", "running"):
            _write(d, state="failed", step="失敗", error="CRM 重新啟動，影片沒有做完，請再產生一次。")
