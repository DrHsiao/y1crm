"""
api/hearing.py — 聽檢輔助：MA42 讀值正規化 + 講義規則判讀（純函式，不碰 DB／網路）

規則來源：doc/4.臨床聽力檢查20210818.pdf（利凌科技聽力培訓部講義）。各規則旁的
「p.N」是該 PDF 的頁碼（每頁 4 張投影片）。講義沒寫的判準集中在 PENDING，畫面標
「暫定，待聽力師確認」；遮蔽量、轉介條件這類講義完全沒有的，系統一律不給數值。

資料格式（API 與 DB hearing_tests.data 共用）：
  thr[ear][kind][freq]  int（dB HL）或 "NR"（無反應）
      ear：R 右／L 左；kind：ac 氣導、acm 氣導（遮蔽）、bc 骨導（未遮蔽）、bcm 骨導（遮蔽）
      freq：字串鍵（JSON 物件鍵只能是字串），如 "1000"
  retest1k[ear]         先測耳 1 kHz 重測值（int）
  tymp[ear]             {"pressure": int daPa, "compliance": float, "np": bool}
  speech[ear]           {"srt": int dB HL, "wds": int %, "mcl": int dB HL}
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

RULES_VERSION = "講義20210818-v1"

EARS = ("R", "L")
EAR_NAME = {"R": "右耳", "L": "左耳"}
OTHER = {"R": "L", "L": "R"}
KINDS = ("ac", "acm", "bc", "bcm")

FREQS = (250, 500, 750, 1000, 1500, 2000, 3000, 4000, 6000, 8000)
BC_FREQS = (250, 500, 750, 1000, 1500, 2000, 3000, 4000)
# MA42 規格書的輸出範圍（dB HL）：超出＝輸入或辨識錯誤
LIMITS = {"ac": (-10, 120), "bc": (-10, 85)}

MODES = ("adult", "child")
TRANSDUCERS = ("DD45", "IP30")
TALKS = ("ok", "poor")

# p.6 測試順序（"retest"＝回到 1 kHz 重測，只有先測耳要做）
ORDER_AC = {"adult": (1000, 2000, 4000, 8000, "retest", 500, 250),
            "child": (500, 4000, 2000, 1000)}
ORDER_BC = (1000, 2000, 4000, 500)
# p.6 起始音量
START_LEVEL = {"ok": 30, "poor": 60}
# p.6 相鄰兩個全音程閾值差 ≥ 20 dB → 補測中間頻率
INTER_DIFF = 20
INTERMEDIATE = ((500, 1000, 750), (1000, 2000, 1500), (2000, 4000, 3000), (4000, 8000, 6000))
# p.9 遮蔽時機
BC_MASK_ABG = 15
IA = {"DD45": 40}          # 耳間衰減：講義用 40 dB（耳罩式）；IP30 插入式未訂
# p.7 聽力損失程度（上限含）
DEGREES = ((25, "正常"), (40, "輕度"), (55, "中度"), (70, "中重度"), (90, "重度"))
DEGREE_MAX = "極重度"
# p.8 聽力損失類型門檻
AC_NORMAL_MAX, ABG_MIN, BC_NORMAL_MAX = 25, 15, 25
# p.3 鼓室圖
TYMP_PRESSURE, TYMP_C_LOW, TYMP_C_HIGH = 100, 0.3, 1.5
TYMP_MEANING = {"A": "中耳功能正常", "As": "耳硬化／鼓膜硬化", "Ad": "聽小骨鏈斷裂",
                "B": "中耳積液／耳垢栓塞", "C": "中耳負壓，可能是積液正在消退"}

# 講義沒寫、暫用常見做法的判準（畫面一律標「暫定」）——聽力師確認後改這裡
PENDING = {
    "pta_freqs": (500, 1000, 2000),
    "retest_tol": 5,
}
PENDING_NOTES = [
    "程度分級與氣骨導差暫用 500／1k／2k 三頻平均",
    "1 kHz 重測差 ≤ 5 dB 暫視為一致",
    "遮蔽要加多少 dB、何時算遮夠：講義未定，系統不給數值",
    "IP30 插入式耳機的耳間衰減：講義未定，選 IP30 時不判斷氣導遮蔽",
    "轉介耳鼻喉科的條件：講義未定，系統不提示",
    "SRT 與 PTA 的容許差：講義未定，只顯示差值",
]


def hz(f: int) -> str:
    return f"{f} Hz" if f < 1000 else f"{f / 1000:g} kHz"


# ── 輸入清洗（前端手填與模型讀值都經過這裡）────────────────────────

def _level(v: Any, kind: str) -> Any:
    """單格聽閾：合法回 int 或 "NR"，否則 None。"""
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip().upper()
    if s in ("NR", "N"):
        return "NR"
    if not re.fullmatch(r"-?\d{1,3}", s):
        return None
    n = int(s)
    lo, hi = LIMITS[kind[:2]]
    return n if lo <= n <= hi else None


def _int(v: Any, lo: int, hi: int) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(str(v).strip())
    except ValueError:
        return None
    return n if lo <= n <= hi else None


def _float(v: Any, lo: float, hi: float) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        n = round(float(str(v).strip()), 2)
    except ValueError:
        return None
    return n if lo <= n <= hi else None


def _dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def options(body: Dict[str, Any]) -> Dict[str, str]:
    """檢查設定：不合法一律退回預設（成人、DD45、先測右耳、對談可以）。"""
    pick = lambda k, allowed: body.get(k) if body.get(k) in allowed else allowed[0]  # noqa: E731
    return {"mode": pick("mode", MODES), "transducer": pick("transducer", TRANSDUCERS),
            "first_ear": pick("first_ear", EARS), "talk": pick("talk", TALKS)}


def clean_data(raw: Any) -> Dict[str, Any]:
    """只留合法值；不合法的格子直接丟掉（前端已先標紅提示）。"""
    raw = _dict(raw)
    thr_in = _dict(raw.get("thr"))
    thr: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for ear in EARS:
        e_in = _dict(thr_in.get(ear))
        thr[ear] = {}
        for kind in KINDS:
            k_in = _dict(e_in.get(kind))
            row = {}
            for f in (FREQS if kind.startswith("ac") else BC_FREQS):
                v = _level(k_in.get(str(f)), kind)
                if v is not None:
                    row[str(f)] = v
            thr[ear][kind] = row

    retest, tymp, speech = {}, {}, {}
    for ear in EARS:
        r = _int(_dict(raw.get("retest1k")).get(ear), *LIMITS["ac"])
        if r is not None:
            retest[ear] = r
        t_in = _dict(_dict(raw.get("tymp")).get(ear))
        t = {"pressure": _int(t_in.get("pressure"), -600, 400),
             "compliance": _float(t_in.get("compliance"), 0, 9.99),
             "np": t_in.get("np") is True}
        if t["np"] or t["pressure"] is not None or t["compliance"] is not None:
            tymp[ear] = t
        s_in = _dict(_dict(raw.get("speech")).get(ear))
        s = {"srt": _int(s_in.get("srt"), -10, 120), "wds": _int(s_in.get("wds"), 0, 100),
             "mcl": _int(s_in.get("mcl"), -10, 120)}
        if any(v is not None for v in s.values()):
            speech[ear] = s
    return {"thr": thr, "retest1k": retest, "tymp": tymp, "speech": speech}


# ── 取值 ────────────────────────────────────────────────────────────

def _v(d: Dict[str, Any], ear: str, kind: str, f: int) -> Any:
    return d["thr"][ear][kind].get(str(f))


def _n(v: Any) -> Optional[int]:
    """只取數值；NR 與空白都回 None（差值比較時略過）。"""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def final_ac(d: Dict[str, Any], ear: str, f: int) -> Any:
    """有遮蔽後的值就用遮蔽後的（那才是真正閾值），否則用未遮蔽值。"""
    v = _v(d, ear, "acm", f)
    return v if v is not None else _v(d, ear, "ac", f)


def final_bc(d: Dict[str, Any], ear: str, f: int) -> Any:
    v = _v(d, ear, "bcm", f)
    return v if v is not None else _v(d, ear, "bc", f)


# ── 判讀 ────────────────────────────────────────────────────────────

def degree(pta: float) -> str:
    for hi, name in DEGREES:
        if pta <= hi:
            return name
    return DEGREE_MAX


def hl_type(ac: float, bc: float, abg: float) -> str:
    """p.8 聽力損失類型（條件須完全符合；都不符合就交給人判讀）。"""
    if ac <= AC_NORMAL_MAX and abg < ABG_MIN:
        return "聽力正常"
    if ac > AC_NORMAL_MAX and abg >= ABG_MIN and bc <= BC_NORMAL_MAX:
        return "傳導型"
    if ac > AC_NORMAL_MAX and abg < ABG_MIN:
        return "感音神經型"
    if ac > AC_NORMAL_MAX and abg >= ABG_MIN and bc > BC_NORMAL_MAX:
        return "混合型"
    return "表外"


def classify_tymp(t: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """p.3 鼓室圖五型。"""
    if not t:
        return None
    if t.get("np"):
        typ = "B"
    else:
        p, c = t.get("pressure"), t.get("compliance")
        if p is None or c is None:
            return None
        if -TYMP_PRESSURE <= p <= TYMP_PRESSURE:
            typ = "As" if c < TYMP_C_LOW else "Ad" if c > TYMP_C_HIGH else "A"
        elif p < -TYMP_PRESSURE and TYMP_C_LOW <= c <= TYMP_C_HIGH:
            typ = "C"
        else:
            return {"type": None, "meaning": "不符合講義表列條件，請人工判讀"}
    return {"type": typ, "meaning": TYMP_MEANING[typ]}


def _interpret(d: Dict[str, Any], ear: str, masking_open: bool) -> Dict[str, Any]:
    fs = PENDING["pta_freqs"]
    notes: List[str] = []
    out: Dict[str, Any] = {"pta_ac": None, "pta_bc": None, "abg": None,
                           "degree": None, "type": None, "notes": notes}
    acs = [final_ac(d, ear, f) for f in fs]
    missing = [hz(f) for f, v in zip(fs, acs) if v is None]
    if missing:
        if len(missing) < len(fs):
            notes.append("缺氣導 " + "、".join(missing) + "，無法計算平均")
        return out
    if "NR" in acs:
        notes.append("平均頻率含無反應（NR），請人工判讀")
        return out
    pta = round(sum(acs) / len(acs), 1)
    out.update(pta_ac=pta, degree=degree(pta))

    bcs = [final_bc(d, ear, f) for f in fs]
    if any(v is None for v in bcs):
        notes.append("骨導未測齊，無法判斷類型")
    elif "NR" in bcs:
        notes.append("骨導含無反應（NR），請人工判讀類型")
    else:
        ptb = round(sum(bcs) / len(bcs), 1)
        abg = round(pta - ptb, 1)
        out.update(pta_bc=ptb, abg=abg, type=hl_type(pta, ptb, abg))
        if out["type"] == "表外":
            notes.append("不符合講義表列的類型條件，請人工判讀")
    if masking_open:
        notes.append("尚有遮蔽未完成，判讀為暫定")
    return out


def summary_text(interp: Dict[str, Dict[str, Any]]) -> str:
    """列表用的一行摘要，例如「右耳 中度 感音神經型（48）／左耳 正常」。"""
    parts = []
    for ear in EARS:
        it = interp.get(ear) or {}
        if it.get("pta_ac") is None:
            continue
        s = f"{EAR_NAME[ear]} {it['degree']}"
        if it.get("type") and it["type"] not in ("聽力正常", "表外"):
            s += f" {it['type']}"
        parts.append(f"{s}（{it['pta_ac']:g}）")
    return "／".join(parts)


def assess(d: Dict[str, Any], mode: str = "adult", transducer: str = "DD45",
           first_ear: str = "R", talk: str = "ok") -> Dict[str, Any]:
    """d 必須是 clean_data() 的輸出。回傳檢查清單（依講義順序）、遮蔽提醒、判讀。"""
    mode = mode if mode in MODES else "adult"
    first = first_ear if first_ear in EARS else "R"
    ears = (first, OTHER[first])
    ia = IA.get(transducer if transducer in TRANSDUCERS else "DD45")
    checklist: List[Dict[str, Any]] = []
    warnings: List[str] = []

    def add(ear: str, kind: str, f: int, done: bool, text: str, reason: str = "") -> None:
        checklist.append({"ear": ear, "kind": kind, "freq": f, "done": bool(done),
                          "text": text, "reason": reason})

    # 1. 氣導：講義順序，各耳測完接著看要不要補中間頻率
    for ear in ears:
        for step in ORDER_AC[mode]:
            if step == "retest":
                if ear == first:
                    add(ear, "retest", 1000, ear in d["retest1k"], f"{EAR_NAME[ear]} 1 kHz 重測",
                        "首次檢查的耳朵要重測 1 kHz，確認受測者專注（p.6）")
                continue
            add(ear, "ac", step, _v(d, ear, "ac", step) is not None, f"{EAR_NAME[ear]} 氣導 {hz(step)}")
        for lo, hi, mid in INTERMEDIATE:
            a, b = _n(_v(d, ear, "ac", lo)), _n(_v(d, ear, "ac", hi))
            if a is None or b is None or abs(a - b) < INTER_DIFF:
                continue
            add(ear, "ac", mid, _v(d, ear, "ac", mid) is not None, f"{EAR_NAME[ear]} 補測氣導 {hz(mid)}",
                f"{hz(lo)} {a} 與 {hz(hi)} {b} 相差 {abs(a - b)} dB ≥ {INTER_DIFF}（p.6）")

    # 2. 骨導
    for ear in ears:
        for f in ORDER_BC:
            add(ear, "bc", f, final_bc(d, ear, f) is not None, f"{EAR_NAME[ear]} 骨導 {hz(f)}")

    # 3. 遮蔽時機（p.9）：只判斷「要不要」，遮蔽量講義未定
    for ear in ears:
        o = OTHER[ear]
        for f in BC_FREQS:
            ac, bc = _n(_v(d, ear, "ac", f)), _n(_v(d, ear, "bc", f))
            if ac is not None and bc is not None and ac - bc >= BC_MASK_ABG:
                add(ear, "bcm", f, _v(d, ear, "bcm", f) is not None, f"{EAR_NAME[ear]} {hz(f)} 骨導需遮蔽",
                    f"氣導 {ac} − 未遮蔽骨導 {bc} = {ac - bc} dB ≥ {BC_MASK_ABG}（p.9）")
        if ia is None:
            continue
        for f in FREQS:
            ac = _n(_v(d, ear, "ac", f))
            if ac is None:
                continue
            bc_o, ac_o = _n(final_bc(d, o, f)), _n(final_ac(d, o, f))
            if bc_o is not None and ac - bc_o >= ia:
                reason = f"氣導 {ac} − {EAR_NAME[o]}骨導 {bc_o} = {ac - bc_o} dB ≥ {ia}（p.9）"
            elif ac_o is not None and ac - ac_o >= ia:
                reason = f"氣導 {ac} − {EAR_NAME[o]}氣導 {ac_o} = {ac - ac_o} dB ≥ {ia}（p.9）"
            else:
                continue
            add(ear, "acm", f, _v(d, ear, "acm", f) is not None, f"{EAR_NAME[ear]} {hz(f)} 氣導需遮蔽", reason)
    if ia is None:
        warnings.append("IP30 插入式耳機的耳間衰減講義未定，本次不判斷氣導遮蔽。")
    if any(v == "NR" for ear in EARS for kind in KINDS for v in d["thr"][ear][kind].values()):
        warnings.append("有無反應（NR）的點：牽涉到它的差值比較已略過，請人工判斷。")

    # 4. 1 kHz 重測一致性
    retest = None
    r, a = d["retest1k"].get(first), _n(_v(d, first, "ac", 1000))
    if r is not None and a is not None:
        diff = abs(r - a)
        ok = diff <= PENDING["retest_tol"]
        retest = {"ear": first, "diff": diff, "ok": ok,
                  "text": f"{EAR_NAME[first]} 1 kHz：第一次 {a}、重測 {r}，相差 {diff} dB" +
                          ("" if ok else f"，超過暫定容許 {PENDING['retest_tol']} dB，"
                                         "請重新說明指導語後再測")}

    # 5. 判讀
    open_mask = {ear: any(c["ear"] == ear and c["kind"] in ("acm", "bcm") and not c["done"]
                          for c in checklist) for ear in EARS}
    interp = {ear: _interpret(d, ear, open_mask[ear]) for ear in EARS}
    tymp = {ear: classify_tymp(d["tymp"].get(ear)) for ear in EARS}
    speech = {}
    for ear in EARS:
        srt, pta = (d["speech"].get(ear) or {}).get("srt"), interp[ear]["pta_ac"]
        if srt is not None and pta is not None:
            speech[ear] = {"srt_pta_diff": round(srt - pta, 1)}

    start = None
    if not any(d["thr"][e]["ac"] for e in EARS):
        lvl = START_LEVEL.get(talk, START_LEVEL["ok"])
        start = (f"起始音量 {lvl} dB HL（對談{'困難' if talk == 'poor' else '可以'}，p.6）；"
                 "降 10 升 5，同一音量 3 次有 2 次反應即為閾值（p.7）")
    nxt = next((c for c in checklist if not c["done"]), None)

    return {"checklist": checklist, "next": nxt, "start": start, "retest": retest,
            "interp": interp, "tymp": tymp, "speech": speech, "warnings": warnings,
            "summary": summary_text(interp), "pending": PENDING_NOTES, "version": RULES_VERSION}


# ── 本機看圖模型（qwen3-vl）讀 MA42 螢幕／PDF 報告 ─────────────────────

READ_PROMPT = """這是一張聽力圖（MAICO MA42 聽力檢查儀的螢幕或報告）。縱軸是 dB HL（上方 -10，往下每條橫格線 +10，到 120），橫軸是頻率。
圖上的符號：
  氣導（有連線）：右耳紅色 ○、左耳藍色 ×
  骨導（沒有連線，畫在頻率直線的旁邊）：右耳紅色 <（在直線左側）、左耳藍色 >（在直線右側）
  有遮蔽時改用：右耳 △（氣導）、[（骨導）；左耳 □（氣導）、]（骨導）
  符號下方帶向下箭頭＝無反應，填 "NR"
請「逐一頻率」檢查（250、500、750、1000、1500、2000、3000、4000、6000、8000 Hz），在每條頻率直線上和它左右兩側，找出所有符號，
判斷它的中心落在哪一條橫格線，或兩條橫格線正中間（＝中間值，例如 10 與 20 之間是 15）。
若畫面另有數值表格，以表格上的數字為準。
只輸出 JSON（沒有符號的頻率省略整列）：
{"rows":[{"freq":250,"R_AC":數字或null,"L_AC":數字或null,"R_BC":數字或null,"L_BC":數字或null,
          "R_ACM":null,"L_ACM":null,"R_BCM":null,"L_BCM":null}],
 "current":null}
有遮蔽符號時才填 R_ACM／L_ACM／R_BCM／L_BCM。若畫面上看得到目前給音的設定，current 填 {"ear":"R或L","type":"AC或BC","freq":頻率,"level":音量,"mask_level":遮蔽音量或null}。"""


def _truthy(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes", "y")


def from_vlm(raw: Any) -> Dict[str, Any]:
    """模型回傳的 rows（每頻率一列、每種符號一欄）轉成 thr 格式；不合法的值丟掉並計數。
    2026-10-19 合成聽力圖（已知答案 20 點）實測 qwen3-vl:32b-instruct：結果隨提示詞措辭大幅擺盪，
    最好一版氣導 11/12 正確但骨導全漏，另一版抓到骨導但約一半差 5 dB；模型也不會自己標出沒把握。
    → 前端把讀值全部標黃待人工確認；可靠的路要等真實 MA42 PDF 樣本做專用解析。"""
    raw = _dict(raw)
    thr: Dict[str, Dict[str, Dict[str, Any]]] = {ear: {k: {} for k in KINDS} for ear in EARS}
    unc_in = {str(u).strip().upper() for u in raw.get("uncertain") or [] if isinstance(u, str)}
    uncertain: set = set()
    dropped = count = 0
    for row in raw.get("rows") or []:
        row = _dict(row)
        f = _int(row.get("freq"), 0, 20000)
        for ear in EARS:
            for kind in KINDS:
                col = f"{ear}_{kind.upper()}"
                v_raw = row.get(col)
                if v_raw is None or str(v_raw).strip().lower() in ("", "null", "none"):
                    continue
                v = _level(v_raw, kind)
                if v is None or f not in (FREQS if kind.startswith("ac") else BC_FREQS):
                    dropped += 1
                    continue
                key = f"{ear}-{kind}-{f}"
                if str(f) in thr[ear][kind]:   # 同頻率讀出兩次：不一致就交給人確認
                    if thr[ear][kind][str(f)] != v:
                        uncertain.add(key)
                    continue
                thr[ear][kind][str(f)] = v
                count += 1
                if isinstance(v, int) and v % 5:   # MA42 預設 5 dB 一階，非 5 的倍數多半是讀歪
                    uncertain.add(key)
                if f"{col}_{f}" in unc_in:
                    uncertain.add(key)

    c = _dict(raw.get("current"))
    cur_ear = str(c.get("ear") or "").strip().upper()[:1]
    cur_typ = str(c.get("type") or "").strip().upper()
    cur_f, lvl, mask = _int(c.get("freq"), 125, 16000), _int(c.get("level"), -10, 120), \
        _int(c.get("mask_level"), 0, 120)
    current = None
    if cur_ear in EARS and cur_f and lvl is not None:
        current = (f"{EAR_NAME[cur_ear]} {'骨導' if cur_typ == 'BC' else '氣導'} {hz(cur_f)} "
                   f"{lvl} dB HL" + (f"（遮蔽 {mask} dB）" if mask else ""))
    return {"thr": thr, "uncertain": sorted(uncertain), "count": count,
            "dropped": dropped, "current": current}
