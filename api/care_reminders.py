"""
care_reminders.py — 「保養提醒」：依交易紀錄算出哪些客人該回門市（適應回診、保養、保固到期）。

John 2026-10-22 同意做，但先「只列名單、不發訊息」：會主動傳訊息給客人的事，等 John 看過名單和訊息內容再開。
所以這裡只有規則（純函式，有單元測試）；app.py 的 /care-reminders 頁列出名單、預覽 LINE 訊息，
店員可以按「加到行事曆」建一筆 follow_up_reminders（行事曆／看板本來就會顯示）。

時間點（從交易日算，只算助聽器的購買／租借）：
  交機後 14 天　適應回診（依這兩週的感受調整）
  交機後 3 個月　第一次保養
  之後每 6 個月　定期保養（最多算到 5 年）
  保固到期前 30 天　保固即將到期
已經有同類提醒（前後 45 天內、未取消）的就不再列，避免重複打擾。
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

TYPE_LABEL = {"tuning": "適應回診", "maintenance": "保養", "other": "保固到期"}
FIRST_TUNING_DAYS = 14
FIRST_SERVICE_MONTHS = 3
SERVICE_EVERY_MONTHS = 6
SERVICE_UNTIL_MONTHS = 60
WARRANTY_NOTICE_DAYS = 30
DEDUP_DAYS = 45
DEFAULT_AHEAD = 30           # 列出未來幾天內到期的
DEFAULT_BACK = 14            # 也列出過期幾天內、還沒處理的


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def milestones(p: Dict[str, Any]) -> List[Dict[str, Any]]:
    """一筆助聽器交易 → 所有提醒時間點 [{type, due, label, months}]。"""
    start: date = p["transaction_date"]
    out = [{"type": "tuning", "due": start + timedelta(days=FIRST_TUNING_DAYS), "label": "適應回診", "months": 0}]
    # 3、6、12、18 … 60 個月
    months = [FIRST_SERVICE_MONTHS] + list(range(SERVICE_EVERY_MONTHS, SERVICE_UNTIL_MONTHS + 1, SERVICE_EVERY_MONTHS))
    for m in months:
        out.append({"type": "maintenance", "due": add_months(start, m),
                    "label": "第一次保養" if m == FIRST_SERVICE_MONTHS else "定期保養", "months": m})
    end: Optional[date] = p.get("warranty_end_date")
    if end:
        out.append({"type": "other", "due": end - timedelta(days=WARRANTY_NOTICE_DAYS), "label": "保固即將到期",
                    "months": 0, "warranty_end": end})
    return out


def _handled(existing: Iterable[Dict[str, Any]], customer_id: int, rtype: str, due: date) -> bool:
    return any(r["customer_id"] == customer_id and r["reminder_type"] == rtype and r["status"] != "cancelled"
               and abs((r["scheduled_date"] - due).days) <= DEDUP_DAYS for r in existing)


def due_items(purchases: Iterable[Dict[str, Any]], existing: Iterable[Dict[str, Any]], today: date,
              ahead: int = DEFAULT_AHEAD, back: int = DEFAULT_BACK) -> List[Dict[str, Any]]:
    """到期（today−back ～ today+ahead）而且還沒排提醒的項目，依日期排序。
    purchases：助聽器的購買／租借（含 customer_id、id、transaction_date、warranty_end_date）；
    existing：follow_up_reminders（customer_id、reminder_type、scheduled_date、status）。"""
    existing = list(existing)
    lo, hi = today - timedelta(days=back), today + timedelta(days=ahead)
    out = []
    for p in purchases:
        for ms in milestones(p):
            if lo <= ms["due"] <= hi and not _handled(existing, p["customer_id"], ms["type"], ms["due"]):
                out.append({**ms, "customer_id": p["customer_id"], "purchase_id": p["id"],
                            "overdue": ms["due"] < today})
    # 同一位客人同一天有兩筆（例如左右耳分開買）只留一筆
    seen, uniq = set(), []
    for it in sorted(out, key=lambda x: (x["due"], x["customer_id"], x["type"])):
        key = (it["customer_id"], it["type"], it["due"])
        if key not in seen:
            seen.add(key)
            uniq.append(it)
    return uniq


def salutation(name: Optional[str], gender: Optional[str]) -> str:
    """王大明＋M → 王先生；沒有性別就用全名（LINE 訊息是一對一傳給本人）。"""
    name = (name or "").strip()
    if name and gender in ("M", "F"):
        return name[0] + ("先生" if gender == "M" else "小姐")
    return name or "您"


def message(item: Dict[str, Any], who: str, model: str, store: str, phone: str, care_url: str) -> str:
    """LINE 訊息草稿（目前只給店員預覽，不會送出）。不提價格、不寫療效。"""
    device = f"您的助聽器（{model}）" if model else "您的助聽器"
    if item["type"] == "tuning":
        body = f"{device}戴了大約兩週，還習慣嗎？建議回門市，依您這兩週的感受做調整。"
    elif item["type"] == "maintenance":
        body = (f"{device}已經配戴約 {item['months']} 個月，"
                + ("建議回門市清潔保養、檢查耳塞與濾網。" if item["label"] == "第一次保養" else "建議定期回門市清潔、除濕與檢查。"))
    else:
        end = item.get("warranty_end")
        when = f"{end.year - 1911}/{end.month:02d}/{end.day:02d}" if end else "近期"
        body = f"{device}的保固將在 {when} 到期，到期前可以回門市做一次完整檢查。"
    lines = [f"{who}您好，這裡是{store}。", body, f"回覆「預約」就能選時間，也可以直接來電 {phone}。"]
    if care_url:
        lines.append(f"在家保養的方法：{care_url}")
    return "\n".join(lines)
