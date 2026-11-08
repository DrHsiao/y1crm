"""
wording.py — 對外文字的用語檢查（粉專貼文、官網好評轉貼共用；純函式）。

價格一律擋（門市規定網站與社群都不談價格）；療效／最高級用語只提醒，由店員決定
（「最近」「第一次」這類正常用法也會中，所以不硬擋）。
"""
from __future__ import annotations

import re
from typing import List, Optional

PRICE_RE = re.compile(r"\d[\d,]*\s*(元|塊|圓)|NT\s*\$?\s*\d|[$＄]\s*\d"
                      r"|售價|定價|特價|優惠價|折扣|打折|價格|價錢|報價|元起")
CLAIMS = ("最好", "最佳", "最強", "最便宜", "最專業", "最清楚", "第一品牌", "唯一", "保證",
          "治療", "治癒", "根治", "痊癒", "恢復聽力", "改善聽力", "百分之百", "100%",
          "神奇", "見效", "療效", "名醫")


def price_word(text: str) -> Optional[str]:
    """找到的第一個價格字眼；沒有回 None。"""
    m = PRICE_RE.search(text or "")
    return m.group(0) if m else None


def claim_words(text: str) -> List[str]:
    t = text or ""
    return [w for w in CLAIMS if w in t]
