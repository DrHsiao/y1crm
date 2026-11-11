"""
video_youtube.py — 文章短影音送 YouTube，官網改嵌 YouTube（John 2026-11-08）。

API 稽核通過前（api/youtube.py 開頭的說明）由店員在 YouTube 工作室手動上傳，這裡負責上傳前後：
- pack()：要貼到 YouTube 的標題、說明欄、標籤。由程式組，不讓 AI 寫，網址和門市資料才會一字不差；
  官網宣傳的連結和粉專貼文共用 fb_drafts.FOOTER_LINKS。channel_template()：YouTube 工作室「上傳預設設定」用的說明欄。
- 上傳後把影片網址貼回 CRM：link() 用 YouTube API 查這支影片（是不是門市頻道的、有沒有公開、允許嵌入），
  結果記在文章的 extra.video.youtube。官網只在 ok 時嵌 YouTube，不然照舊播自己的 MP4。
- recheck()：每 6 小時重查一次。影片被下架、改成私人或關掉嵌入 → ok=false，官網自動改回自己的 MP4；恢復了再改回來。
- 重新做影片時（api/article_video.py 整個換掉 extra.video），連結跟著拿掉：YouTube 上的是舊版，要重傳。
- sync_meta()：把說明、標籤寫到已連結的 YouTube 影片（要 youtube.force-ssl 權限）。標題、類別照 YouTube 上現在的，不動。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config
from api import fb_drafts, fb_page, review_art, web_admin, youtube

log = logging.getLogger("y1crm.youtube")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# 一篇文章可以連兩支：橫式（官網嵌這支）、直式（YouTube Shorts，只記錄、一起重查，官網不嵌直的）
PATHS = {"main": "$.video.youtube", "tall": "$.video.tall.youtube"}
FILE_RE = re.compile(r"^videos/[0-9a-f]{16}\.(mp4|jpg)$")      # api/article_video.py 存的檔名
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "studio.youtube.com", "youtu.be",
          "www.youtube-nocookie.com", "youtube-nocookie.com"}
_PATH_RE = re.compile(r"^/(?:shorts|embed|live|v|video)/([A-Za-z0-9_-]{11})(?:/|$)")
TITLE_MAX = 100          # YouTube 的上限
DESC_MAX = 5000
TAGS_MAX = 500           # 全部標籤加起來（含逗號）
BASE_TAGS = ("助聽器", "聽力", "聽力保健", "台北助聽器")
HASHTAGS = ("助聽器", "聽力保健", "台北助聽器")
DISCLAIMER = "本影片為一般衛教資訊，實際情況請以門市專業人員或醫師評估為準。"
CHECK_EVERY = 6 * 3600


class Invalid(ValueError):
    pass


def video_id(text: str) -> Optional[str]:
    """YouTube 網址（watch?v=、youtu.be/、shorts/、embed/、工作室的 video/…/edit）或 11 碼影片 id → id；看不懂回 None。"""
    s = (text or "").strip()
    if VIDEO_ID_RE.match(s):
        return s
    if not re.match(r"^https?://", s):
        s = "https://" + s
    u = urllib.parse.urlparse(s)
    host = (u.hostname or "").lower()
    if host not in _HOSTS:
        return None
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif u.path == "/watch":
        vid = urllib.parse.parse_qs(u.query).get("v", [""])[0]
    else:
        m = _PATH_RE.match(u.path)
        vid = m.group(1) if m else ""
    return vid if VIDEO_ID_RE.match(vid) else None


def _plain(s: str) -> str:
    return re.sub(r"[<>]", "", s or "").strip()     # YouTube 標題、說明欄不能有角括號


def _tag(s: str) -> str:
    return re.sub(r"[<>,#\s]+", "", s or "")


def store_lines(store: Dict[str, Any], site: str) -> List[str]:
    lines = [f"【{store['name']}】"] if store.get("name") else []
    if store.get("address"):
        lines.append(f"📍 {store['address']}")
    transit = [t.get("text", "") for t in store.get("transit") or [] if isinstance(t, dict) and t.get("text")]
    if transit:
        lines.append("🚇 " + "／".join(transit))
    if store.get("phone"):
        lines.append(f"☎ {store['phone']}")
    hours = [f"{h.get('days', '')} {h.get('time', '')}".strip() for h in store.get("hours") or [] if isinstance(h, dict)]
    if hours:
        lines.append("🕘 " + "、".join(h for h in hours if h))
    if site:
        lines += [f"💬 LINE 諮詢：{site}/line?src=youtube", f"🌐 官網：{site}"]
    return lines


def _links(site: str) -> List[str]:
    return [f"{icon} {label}：{site}{path}" for icon, label, path in fb_drafts.FOOTER_LINKS] if site else []


def _hashtags(extra: Iterable[str] = ()) -> str:
    out: List[str] = []
    for h in [*HASHTAGS[:1], *extra, *HASHTAGS[1:]]:
        h = _tag(h)
        if h and h not in out:
            out.append(h)
    return " ".join(f"#{h}" for h in out[:3])        # YouTube 把前 3 個顯示在標題上方


def channel_template(store: Dict[str, Any], site: str) -> str:
    """YouTube 工作室 → 設定 → 上傳預設設定 → 說明：沒有單篇文章的部分，CRM 產生的說明欄會整段取代它。"""
    site = (site or "").rstrip("/")
    blocks = ["\n".join(_links(site)), "\n".join(store_lines(store, site)), DISCLAIMER, _hashtags()]
    return "\n\n".join(b for b in blocks if b)


def pack(post: Dict[str, Any], store: Dict[str, Any], site: str) -> Dict[str, Any]:
    """一篇文章的影片要貼到 YouTube 的 {title, description, tags（list）, tags_text}。"""
    site = (site or "").rstrip("/")
    x = fb_drafts._json(post.get("extra"))
    brand = store.get("brand") or store.get("name") or ""
    title = _plain(post.get("title"))
    if brand and len(f"{title}｜{brand}") <= TITLE_MAX:
        title = f"{title}｜{brand}"
    url = fb_page.post_url(site, post.get("kind") or "", post.get("slug") or "") if site else None
    plan = (x.get("video") or {}).get("plan") if isinstance(x.get("video"), dict) else None
    said = [_plain(p.get("say")) for p in plan or [] if isinstance(p, dict) and p.get("say")]
    # 摘要是寫給官網文章的：「本文說明……」在 YouTube 上要說「這支影片」
    head = [_plain(post.get("summary")).replace("本文", "這支影片")] if post.get("summary") else []
    if url:
        head.append(f"▶ 完整文章：{url}")
    blocks = ["\n".join(head), "\n".join(_links(site)), "\n".join(["【影片內容】", *said]) if said else "",
              "\n".join(store_lines(store, site)), DISCLAIMER, _hashtags([post.get("category") or ""])]
    desc = "\n\n".join(b for b in blocks if b)
    if len(desc) > DESC_MAX:                                       # 太長先拿掉影片內容
        desc = "\n\n".join(b for b in blocks if b and not b.startswith("【影片內容】"))[:DESC_MAX]
    tags: List[str] = []
    for t in [*(x.get("keywords") or []), post.get("category") or "", *BASE_TAGS, brand, _tag(store.get("name") or "")]:
        t = re.sub(r"[<>,]", "", str(t)).strip()
        if t and t not in tags and len(",".join(tags + [t])) <= TAGS_MAX:
            tags.append(t)
    return {"title": title[:TITLE_MAX], "description": desc, "tags": tags, "tags_text": ",".join(tags)}


# ── 貼回網址、檢查 ──────────────────────────────────────────
def judge(item: Optional[Dict[str, Any]], channel_id: str) -> Dict[str, Any]:
    """videos.list 的一筆（None＝查不到）→ {ok, problem, note, title, privacy, embeddable}。ok＝官網可以嵌。"""
    if not item:
        return {"ok": False, "problem": "YouTube 上找不到這支影片（可能被刪除，或網址貼錯了）"}
    sn, st = item.get("snippet") or {}, item.get("status") or {}
    out = {"title": sn.get("title") or "", "privacy": st.get("privacyStatus") or "",
           "embeddable": st.get("embeddable") is not False, "ok": True, "problem": "", "note": ""}
    if channel_id and sn.get("channelId") != channel_id:
        out.update(ok=False, problem="這支影片不是門市頻道的")
    elif st.get("uploadStatus") in ("deleted", "failed", "rejected"):
        out.update(ok=False, problem="YouTube 沒有處理成功（上傳失敗或被拒絕）")
    elif out["privacy"] == "private":
        out.update(ok=False, problem="影片是「私人」，官網嵌不了：請在 YouTube 工作室改成「公開」")
    elif not out["embeddable"]:
        out.update(ok=False, problem="影片沒有勾「允許嵌入」：YouTube 工作室 → 影片詳細資料 → 顯示更多 → 授權")
    elif out["privacy"] == "unlisted":
        out["note"] = "「不公開」：官網嵌得了，但在 YouTube 上搜尋不到"
    elif st.get("uploadStatus") == "uploaded":
        out["note"] = "YouTube 還在處理，處理完才能播放"
    return out


def fetch(ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """videos.list（一次最多 50 支，每次 1 單位額度）→ {id: item}。用門市頻道的授權查：私人影片也看得到。"""
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(ids), 50):
        r = youtube.get("videos", {"part": "snippet,status", "id": ",".join(ids[i:i + 50]), "maxResults": "50"})
        out.update({it["id"]: it for it in r.get("items") or [] if it.get("id")})
    return out


def post_row(cur, pid: int) -> Optional[Dict[str, Any]]:
    """文章（pack 要用的欄位）＋ video（程式做的短影音；沒有的話 None）。找不到文章回 None。"""
    cur.execute(f"SELECT id, kind, slug, title, summary, category, status, extra FROM {web_admin.TABLE} WHERE id=%s",
                (pid,))
    post = cur.fetchone()
    if post is None:
        return None
    v = fb_drafts._json(post["extra"]).get("video")
    return {**post, "video": v if isinstance(v, dict) and v.get("file") else None}


def _video(cur, pid: int) -> Dict[str, Any]:
    p = web_admin.get_post(cur, pid)
    if p is None:
        raise Invalid("找不到這篇文章（可能已被刪除）。")
    v = fb_drafts._json(p.get("extra")).get("video")
    if not isinstance(v, dict) or not v.get("file"):
        raise Invalid("這篇還沒有影片。")
    return v


def _save(cur, pid: int, info: Optional[Dict[str, Any]], which: str = "main") -> None:
    path = PATHS[which]
    parent = path.rsplit(".", 1)[0]
    if info is None:
        cur.execute(f"UPDATE {web_admin.TABLE} SET extra=JSON_REMOVE(extra, '{path}') WHERE id=%s", (pid,))
    else:
        cur.execute(f"UPDATE {web_admin.TABLE} SET extra=JSON_SET(extra, '{path}', CAST(%s AS JSON)) "
                    f"WHERE id=%s AND JSON_TYPE(JSON_EXTRACT(extra, '{parent}'))='OBJECT'",
                    (json.dumps(info, ensure_ascii=False), pid))


def _target(v: Dict[str, Any], which: str) -> Dict[str, Any]:
    """橫式＝extra.video、直式＝extra.video.tall（沒做直式就不能連 Shorts）。"""
    if which not in PATHS:
        raise Invalid("不認得的影片版本。")
    if which == "tall":
        if not isinstance(v.get("tall"), dict) or not v["tall"].get("file"):
            raise Invalid("這篇還沒有直式影片。")
        return v["tall"]
    return v


def link(cur, pid: int, text: str, actor: str, which: str = "main") -> Dict[str, Any]:
    """店員貼回的 YouTube 網址 → 查過再記到 extra.video.youtube（直式＝extra.video.tall.youtube）。
    查到有問題也記，官網先不嵌，畫面上告訴店員怎麼改。"""
    v = _target(_video(cur, pid), which)
    vid = video_id(text)
    if not vid:
        raise Invalid("看不懂這個網址：請貼 YouTube 影片的網址（youtube.com/watch?v=… 或 youtu.be/…）。")
    if not youtube.authorized():
        raise Invalid("YouTube 還沒授權，沒辦法確認這支影片（sudo venv/bin/python scripts/youtube_auth.py login）。")
    try:
        item = fetch([vid]).get(vid)
    except youtube.YouTubeError as e:
        raise Invalid(f"暫時查不到 YouTube（{e}），請稍後再試。") from None
    now = review_art.now_str()
    info = {"id": vid, **judge(item, config.YOUTUBE.get("channel_id") or ""), "at": now, "by": actor,
            "checked_at": now, "for": v["file"]}
    _save(cur, pid, info, which)
    log.info("YouTube 連結 #%s ← %s（%s）%s", pid, vid, actor, info["problem"] or "可以嵌入")
    return info


def unlink(cur, pid: int, which: str = "main") -> None:
    _target(_video(cur, pid), which)
    _save(cur, pid, None, which)


def meta_body(item: Dict[str, Any], description: str, tags: List[str]) -> Dict[str, Any]:
    """videos.update 的內容：只換說明和標籤。snippet 沒給的欄位會被 YouTube 清掉，所以標題、類別、語言照現在的帶上。"""
    sn = item.get("snippet") or {}
    snippet = {"title": sn.get("title"), "categoryId": sn.get("categoryId"), "description": description, "tags": tags}
    for k in ("defaultLanguage", "defaultAudioLanguage"):
        if sn.get(k):
            snippet[k] = sn[k]
    return {"id": item["id"], "snippet": snippet}


def clean_meta(description: str, tags: Any) -> Tuple[str, List[str]]:
    """店員在視窗裡改過的說明、標籤（字串用逗號分隔或 list）→ 檢查過的值。"""
    desc = _plain(description)
    if not desc:
        raise Invalid("說明不能是空的。")
    if len(desc) > DESC_MAX:
        raise Invalid(f"說明超過 {DESC_MAX} 字。")
    raw = tags.split(",") if isinstance(tags, str) else [str(t) for t in tags or []]
    out: List[str] = []
    for t in raw:
        t = re.sub(r"[<>]", "", t).strip()
        if t and t not in out:
            out.append(t)
    if len(",".join(out)) > TAGS_MAX:
        raise Invalid(f"標籤加起來超過 {TAGS_MAX} 字。")
    return desc, out


def sync_meta(cur, pid: int, actor: str, description: Optional[str] = None, tags: Any = None,
              which: str = "main") -> Dict[str, Any]:
    """說明、標籤寫到這篇連結的 YouTube 影片（which＝tall：Shorts 那支）。沒給就用 pack() 產生的。
    記下同步時間（…youtube.synced_at）。"""
    post = post_row(cur, pid)
    if post is None:
        raise Invalid("找不到這篇文章（可能已被刪除）。")
    if post["video"] is None:
        raise Invalid("這篇還沒有影片。")
    yt = _target(post["video"], which).get("youtube") or {}
    if not VIDEO_ID_RE.match(str(yt.get("id") or "")):
        raise Invalid("這篇還沒貼 YouTube 網址。")
    if description is None:
        p = pack(post, fb_drafts.store_info(cur), fb_drafts.site_url(cur))
        description, tags = p["description"], p["tags"]
    desc, tag_list = clean_meta(description, tags)
    if not youtube.authorized():
        raise Invalid("YouTube 還沒授權。")
    try:
        item = fetch([yt["id"]]).get(yt["id"])
        if item is None:
            raise Invalid("YouTube 上找不到這支影片。")
        if (item.get("snippet") or {}).get("channelId") != config.YOUTUBE.get("channel_id"):
            raise Invalid("這支影片不是門市頻道的，不能改。")
        youtube.put("videos", {"part": "snippet"}, meta_body(item, desc, tag_list))
    except youtube.YouTubeError as e:
        if "insufficient" in str(e).lower():
            raise Invalid("YouTube 授權少了「改影片說明」的權限：請管理者在 Google Cloud 加 youtube.force-ssl，"
                          "再跑 sudo venv/bin/python scripts/youtube_auth.py login。") from None
        raise Invalid(f"YouTube 更新失敗（{e}），請稍後再試。") from None
    now = review_art.now_str()
    path = PATHS[which]
    cur.execute(f"UPDATE {web_admin.TABLE} SET extra=JSON_SET(extra, '{path}.synced_at', %s, "
                f"'{path}.synced_by', %s) WHERE id=%s", (now, actor, pid))
    log.info("YouTube #%s %s 說明、標籤已更新（%s，說明 %d 字、標籤 %d 個）", pid, yt["id"], actor, len(desc), len(tag_list))
    return {"ok": True, "synced_at": now, "synced_by": actor, "description_len": len(desc), "tags": len(tag_list)}


def recheck(pid: Optional[int] = None) -> int:
    """重查有連 YouTube 的文章（pid＝只查這一篇），回狀態有變的篇數。連不上 YouTube 不動任何資料。"""
    if not youtube.authorized():
        return 0
    with review_art._conn() as db, db.cursor() as cur:
        rows = []
        for which, path in PATHS.items():
            cur.execute(f"SELECT id, JSON_UNQUOTE(JSON_EXTRACT(extra, '{path}')) AS yt FROM {web_admin.TABLE} "
                        f"WHERE JSON_EXTRACT(extra, '{path}.id') IS NOT NULL" + (" AND id=%s" if pid else ""),
                        (pid,) if pid else ())
            rows += [(r["id"], fb_drafts._json(r["yt"]), which) for r in cur.fetchall()]
        rows = [(i, y, w) for i, y, w in rows if VIDEO_ID_RE.match(str(y.get("id") or ""))]
        if not rows:
            return 0
        try:
            items = fetch(sorted({y["id"] for _, y, _ in rows}))
        except youtube.YouTubeError as e:
            log.warning("YouTube 重查失敗（不更動）：%s", e)
            return 0
        changed, now = 0, review_art.now_str()
        for i, y, which in rows:
            j = judge(items.get(y["id"]), config.YOUTUBE.get("channel_id") or "")
            if j["ok"] != y.get("ok") or j.get("problem") != y.get("problem"):
                changed += 1
                log.warning("YouTube #%s %s（%s）：%s", i, y["id"], which, j["problem"] or "恢復正常")
            _save(cur, i, {**y, **j, "checked_at": now}, which)
    return changed


_sched_started = False


def start_scheduler() -> None:
    """背景執行緒：開機 5 分鐘後查一次，之後每 6 小時一次（沒授權就什麼都不做）。"""
    global _sched_started
    if _sched_started:
        return
    _sched_started = True

    def _loop() -> None:
        time.sleep(300)
        while True:
            try:
                recheck()
            except Exception:  # noqa: BLE001 — 排程不可拖垮服務
                log.exception("YouTube 重查失敗")
            time.sleep(CHECK_EVERY)

    threading.Thread(target=_loop, daemon=True, name="youtube-check").start()
