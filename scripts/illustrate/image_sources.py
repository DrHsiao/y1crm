#!/usr/bin/env python
"""
image_sources.py — 官網和粉專貼文的圖：哪些是原圖、哪些是 AI 生成，以及批次重畫 AI 圖。

John 2026-11-08：「要標註圖片是原圖還是 AI 生成，以後才能批次替換。」
標記存在哪裡（見 api/web_admin.py 的 COVER_KEYS）：
- 官網內容 y1web.web_posts.extra：cover_src＝"ai"／"upload"（原圖），AI 的另有 cover_model、cover_at；
- 粉專草稿 fb_post_drafts.image_meta：src＝"ai"、model、at（草稿的圖都是 AI 畫的）。

用法（在 y1crm 目錄）：
    venv/bin/python scripts/illustrate/image_sources.py list
    venv/bin/python scripts/illustrate/image_sources.py backfill          # 舊資料補標記（可以重跑）
    venv/bin/python scripts/illustrate/image_sources.py redraw --kind care [--model 舊版本] [--before 2026-12-01] [--ids 80 81] [--yes]
    venv/bin/python scripts/illustrate/image_sources.py set --id 80 --stock wm-zinc-air-312   # 指定圖庫照片
redraw 沒加 --yes 只列出會重畫哪幾筆。只動 AI 圖，原圖一律不動。畫圖要用 0002（和 CRM 用同一把跨行程鎖排隊）。
set：專欄、小教室的封面換成圖庫裡指定的那張（自動挑得不對題時用；原圖一樣不動）。
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from api import fb_drafts, post_art, review_art, web_admin  # noqa: E402

KINDS = ("article", "care", "share", "fb")
BATCH = review_art.MAX_IDS


def _x(v):
    return fb_drafts._json(v)


def rows(cur):
    """[(種類, id, 來源, 模型, 時間)]：有圖的官網內容和粉專草稿。"""
    out = []
    cur.execute(f"SELECT id, kind, cover, extra, updated_at FROM {web_admin.TABLE} WHERE cover IS NOT NULL AND cover<>''")
    for r in cur.fetchall():
        x = _x(r["extra"])
        src = x.get("cover_src") or ("ai" if x.get("cover_ai") else "")
        out.append((r["kind"], r["id"], src, x.get("cover_model") or "", x.get("cover_at") or ""))
    cur.execute("SELECT id, status, image_meta FROM fb_post_drafts WHERE image IS NOT NULL AND status<>'discarded'")
    for r in cur.fetchall():
        m = _x(r["image_meta"])
        out.append(("fb", r["id"], m.get("src") or "", m.get("model") or "", m.get("at") or ""))
    return out


def cmd_list(cur, _args):
    c = Counter((k, src or "（未標記）", model) for k, _, src, model, _ in rows(cur))
    for (k, src, model), n in sorted(c.items()):
        print(f"{k:8} {web_admin.COVER_SRC.get(src, src):6} {model:32} {n}")


def mark(x, updated_at):
    """一筆的新標記（純函式）：沒有 cover_src 的，cover_ai＝AI 生成（2026-11-08 以前畫的都是 ART_MODEL 這一版），
    其他＝原圖。已經有標記的回 None（不動）。"""
    if x.get("cover_src"):
        return None
    when = updated_at.strftime("%Y-%m-%d %H:%M") if updated_at else ""
    if x.get("cover_ai") is True:
        return {**x, "cover_src": "ai", "cover_model": x.get("cover_model") or review_art.ART_MODEL,
                "cover_at": x.get("cover_at") or when}
    return {**x, "cover_src": "upload"}


def cmd_backfill(cur, _args):
    """舊資料補標記（可以重跑：已經有標記的不動）。⚠ 不用 SQL 的 COALESCE 判斷：MySQL 的 JSON true 和 FALSE 比較不可靠。"""
    n = Counter()
    cur.execute(f"SELECT id, extra, updated_at FROM {web_admin.TABLE} WHERE cover IS NOT NULL AND cover<>''")
    for r in cur.fetchall():
        new = mark(_x(r["extra"]), r["updated_at"])
        if new:
            cur.execute(f"UPDATE {web_admin.TABLE} SET extra=%s, updated_at=updated_at WHERE id=%s",   # 只補標記：不算修改
                        (json.dumps(new, ensure_ascii=False), r["id"]))
            n[new["cover_src"]] += 1
    cur.execute("SELECT id, image_meta, updated_at FROM fb_post_drafts WHERE image IS NOT NULL")
    for r in cur.fetchall():
        m = _x(r["image_meta"])
        if not m.get("src"):                     # 草稿的圖只有 AI 畫的
            m.update(src="ai", model=m.get("model") or review_art.ART_MODEL,
                     at=m.get("at") or r["updated_at"].strftime("%Y-%m-%d %H:%M"))
            cur.execute("UPDATE fb_post_drafts SET image_meta=%s, updated_at=updated_at WHERE id=%s",
                        (json.dumps(m, ensure_ascii=False), r["id"]))
            n["fb"] += 1
    print(f"補標記：官網 AI 生成 {n['ai']} 筆、原圖 {n['upload']} 筆；粉專草稿 {n['fb']} 筆")


def pick(cur, args):
    out = []
    for k, i, src, model, at in rows(cur):
        if k != args.kind or src != "ai":
            continue
        if args.model and model != args.model:
            continue
        if args.before and not (at and at < args.before):
            continue
        if args.ids and i not in args.ids:
            continue
        out.append(i)
    return sorted(out)


def cmd_redraw(cur, args):
    ids = pick(cur, args)
    print(f"{args.kind}：符合條件的 AI 圖 {len(ids)} 張", ids[:50], "…" if len(ids) > 50 else "")
    if not args.yes or not ids:
        print("（只列出；要重畫請加 --yes）" if ids else "")
        return
    if not review_art.enabled():
        sys.exit("這台主機沒有設定畫圖的環境（config [imagegen]）。")
    for k in range(0, len(ids), BATCH):
        part = ids[k:k + BATCH]
        if args.kind == "share":
            s = review_art.illustrate(part, "批次重畫", force=True)
        elif args.kind == "fb":
            s = post_art.illustrate(part, [], "批次重畫", force=True)
        else:
            s = post_art.illustrate([], part, "批次重畫", force=True, kinds=(args.kind,))
        print(f"#{part[0]}–#{part[-1]}：{s['done']}/{s['total']}", s["errors"] or "", flush=True)


def cmd_set(cur, args):
    from pathlib import Path
    import config
    from api import stock_art
    media = Path(config.WEB["media_dir"])
    e = next((x for x in stock_art.available(stock_art.load(), media) if x["id"] == args.stock), None)
    if e is None:
        sys.exit(f"圖庫裡沒有 {args.stock}（或還沒匯入）。")
    for pid in args.id:
        cur.execute(f"SELECT kind, cover, extra FROM {web_admin.TABLE} WHERE id=%s", (pid,))
        r = cur.fetchone()
        if not r or r["kind"] not in ("article", "care"):
            print(f"#{pid}：不是專欄或小教室，跳過"); continue
        x = _x(r["extra"])
        if r["cover"] and (x.get("cover_src") or ("ai" if x.get("cover_ai") else "upload")) == "upload":
            print(f"#{pid}：是原圖，不動"); continue
        stock_art.set_cover(cur, media, pid, e["file"], stock_art.credit(e), "指定圖庫照片", review_art.now_str())
        print(f"#{pid} → {e['id']}（{e['title']}）")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("backfill")
    r = sub.add_parser("redraw")
    r.add_argument("--kind", choices=KINDS, required=True)
    r.add_argument("--model", help="只重畫這個版本畫的（例如 sdxl-base-1.0/watercolor-v1）")
    r.add_argument("--before", help="只重畫這個時間以前畫的（2026-12-01 或 2026-12-01 10:00）")
    r.add_argument("--ids", type=int, nargs="+", help="只重畫這幾筆（id）")
    r.add_argument("--yes", action="store_true", help="真的重畫（沒加只列出）")
    st = sub.add_parser("set")
    st.add_argument("--id", type=int, nargs="+", required=True)
    st.add_argument("--stock", required=True, help="stock_library.json 的 id")
    args = ap.parse_args()
    with review_art._conn() as db, db.cursor() as cur:
        {"list": cmd_list, "backfill": cmd_backfill, "redraw": cmd_redraw, "set": cmd_set}[args.cmd](cur, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
