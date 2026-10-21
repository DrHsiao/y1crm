"""
api/noah.py — 解析 Noah 4 匯出檔（.nhax）：聽力圖＋ReSound 選配設定（純函式，不碰 DB／網路）

格式是 2026-10 拿門市實際匯出檔解開的（HIMSA 沒公開外層格式，以下是觀察結果）：
  外層      專有標頭＋數段 zlib；其中一段是 <Patient> XML（Sessions/Session/Action）
  Action    DataTypeCode 決定內容
    1         聽力圖：PublicData＝base64 的 HIMSA Audiogram 標準 XML（各家聽檢儀模組都寫這個）
              MAICO 模組的 PrivateData＝<TestData>，記有機型（MA 42）與耳機（DD45／B71）
              店員在 Noah 手動輸入的聽力圖沒有遮蔽資訊
    129/130   左／右耳選了哪台助聽器（Description 是可讀文字）
    131/132   左／右耳選配：ReSound Smart Fit 的 PrivateData＝base64＋gzip 的 <ActiveProduct> XML；
              其他廠牌（Phonak Target 等）是各自的二進位格式，讀不了
  ReSound   Programs/Program/Features/Feature 的 FeatureValue＝目前值、InitialFitFeatureValue＝
            初始選配值（Smart Fit 滑桿上的深灰點）；Gains/SoundInputLevel＝50/65/80 dB 輸入的增益

上傳檔不可信：解壓有上限、拒絕 DOCTYPE／ENTITY（XML 炸彈）、每個欄位只取需要的並轉型；
單一 Action 解不開就略過並計入 warnings，不讓整份匯入失敗。
"""
from __future__ import annotations

import base64
import re
import zlib
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from api import hearing as H

MAX_SECTION = 64 * 1024 * 1024    # 外層單段 zlib 解壓上限（實測一位客人 1.6MB）
MAX_GZIP = 16 * 1024 * 1024       # ReSound 單次選配 gzip 解壓上限（實測 190KB）
NS = "{http://www.himsa.com/Measurement/Audiogram}"
RESOUND_MODULE = "5177347"

# HIMSA StimulusSignalOutput → (耳, 種類)
OUTPUTS = {"AirConductorRight": ("R", "ac"), "AirConductorLeft": ("L", "ac"),
           "InsertPhoneRight": ("R", "ac"), "InsertPhoneLeft": ("L", "ac"),
           "BoneConductorRight": ("R", "bc"), "BoneConductorLeft": ("L", "bc")}
KIND_NAME = {"ac": "氣導", "acm": "氣導遮蔽", "bc": "骨導", "bcm": "骨導遮蔽"}

# Smart Fit 2.3 中文版畫面上的名稱與刻度文字（2026-10 用門市螢幕照片逐項對過 XML 值）。
# 順序＝畫面由上而下。對照表沒有的值（沒在畫面上看過的）直接顯示原始值，不猜翻譯。
RS_FEATURES = (
    ("Directionality", "方向性", "語音", {"SoftSwitching": "同步智能轉換系統"}),
    ("BeamWidthLevel", "多方向性", "語音", {"Wide": "寬"}),
    ("BandsplitLevel", "方向性混合", "語音", {"Low": "極低", "Mild": "低", "Medium": "中", "High": "高"}),
    ("FreqLow.Level", "移頻", "語音", {"LevelOff": "關", "Level1": "非常輕度 (5K)", "Level2": "輕度 (4K)",
                                     "Level4": "中度 (3.5K)", "Level6": "強度 (2.5K)"}),
    ("DFS", "數位回饋音抑制 DFS", "舒適", {"Off": "關", "Mild": "輕度", "Moderate": "中度",
                                       "Strong": "強度", "Music": "音樂模式"}),
    ("NoiseTracker", "噪音追蹤系統 II", "舒適", {"Off": "關", "Mild": "輕度", "Medium": "中度"}),
    ("WindNoiseReduction.Mode", "風噪音抑制", "舒適", {"Off": "關", "Mild": "輕度", "Medium": "中度"}),
    ("ImpulseNoiseReduction", "脈衝降噪", "舒適", {"Off": "關", "Mild": "輕度", "Moderate": "中等"}),
    ("Expansion", "擴展", "舒適", {"Mild": "關", "Medium": "輕度", "Moderate": "中度"}),   # 最低一格叫 Mild，畫面顯示「關」
)
RS_FEATURE_IDS = {f[0] for f in RS_FEATURES}
RS_PROGRAM_NAME = {"1": "全面"}     # DisplayName 是預設的 "Program" 時，依 EnvironmentId 顯示
GAIN_LEVELS = (("DB50", "50"), ("DB65", "65"), ("DB80", "80"), ("DBMPO", "mpo"))


class NoahError(ValueError):
    """檔案不是可讀的 Noah 匯出檔（訊息直接給使用者看）。"""


# ── 低階：解壓與安全 XML ──────────────────────────────────────────

def _xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise NoahError("檔案內含不允許的 XML 宣告。")
    return ET.fromstring(data)


def _sections(raw: bytes) -> List[bytes]:
    """找出外層所有 zlib 段並解壓（標頭格式沒公開，用 zlib 魔術位元組掃描）。"""
    mv, out, i = memoryview(raw), [], 0
    while i < len(raw) - 1:
        if raw[i] == 0x78 and raw[i + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            o = zlib.decompressobj()
            try:
                data = o.decompress(mv[i:], MAX_SECTION)
            except zlib.error:
                i += 1
                continue
            if o.unconsumed_tail:
                raise NoahError("檔案解壓後過大，不像單一客戶的匯出檔。")
            if o.eof and data:
                out.append(data)
                i = len(raw) - len(o.unused_data)
                continue
        i += 1
    return out


def _gunzip(data: bytes) -> Optional[bytes]:
    if data[:2] != b"\x1f\x8b":
        return None
    o = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = o.decompress(data, MAX_GZIP)
    return None if o.unconsumed_tail else out


def _b64(text: Optional[str]) -> bytes:
    return base64.b64decode(text or "") if text and text.strip() else b""


def _kids(e: Optional[ET.Element], tag: str) -> List[ET.Element]:
    x = e.find(tag) if e is not None else None      # Element 的真假值看子節點數，別用 `or []`
    return list(x) if x is not None else []


def _txt(e: Optional[ET.Element], tag: str) -> Optional[str]:
    x = e.find(tag) if e is not None else None
    return x.text.strip() if x is not None and x.text else None


def _dt(s: Optional[str]) -> str:
    """Noah 的 CreateDate（本地時間 2026-10-17T10:12:33）→ '2026-10-17 10:12'。"""
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", s or "")
    return f"{m.group(1)} {m.group(2)}" if m else ""


def _num(s: Optional[str]) -> Optional[float]:
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


# ── 聽力圖 ──────────────────────────────────────────────────────────

def _device_info(priv: bytes) -> Dict[str, Optional[str]]:
    """聽檢儀模組的 PrivateData（MAICO 是 <TestData>）→ 機型與氣導耳機；其他格式回空。"""
    info: Dict[str, Optional[str]] = {"device": None, "transducer": None}
    try:
        data = _gunzip(priv) or priv
        if b"<TestData" not in data[:500]:
            return info
        root = _xml(data)
    except (ET.ParseError, NoahError, zlib.error):
        return info
    info["device"] = _txt(root.find("DeviceInformation"), "DisplayName")
    for t in root.iter("Transducer"):
        if t.text and t.text.strip() in H.TRANSDUCERS:
            info["transducer"] = t.text.strip()
            break
    return info


def parse_audiogram(xml_bytes: bytes) -> Dict[str, Any]:
    """HIMSA Audiogram XML → {thr, count, masking_info, skipped}。
    點上有 MaskingLevel>0 就算遮蔽值（MAICO 把遮蔽／未遮蔽的點放在同一條曲線裡）。"""
    root = _xml(xml_bytes)
    thr: Dict[str, Dict[str, Dict[str, Any]]] = {e: {k: {} for k in H.KINDS} for e in H.EARS}
    skipped: List[str] = []
    masking_info = False
    for curve in root.iter(NS + "ToneThresholdAudiogram"):
        cond = curve.find(NS + "AudMeasurementConditions")
        out = _txt(cond, NS + "StimulusSignalOutput")
        sig = _txt(cond, NS + "StimulusSignalType")
        aided = _txt(cond, NS + "HearingInstrument_1_Condition")
        if out not in OUTPUTS or sig not in (None, "PureTone") or aided not in (None, "UnAided"):
            skipped.append(f"{out or '未知輸出'}（{sig or '-'}／{aided or '-'}）整條略過")
            continue
        if (_txt(cond, NS + "MaskingSignalType") or "NoSignalApplied") != "NoSignalApplied":
            masking_info = True
        ear, base = OUTPUTS[out]
        for p in curve.findall(NS + "TonePoints"):
            f, lv = _num(_txt(p, NS + "StimulusFrequency")), _num(_txt(p, NS + "StimulusLevel"))
            ml = _num(_txt(p, NS + "MaskingLevel"))
            kind = base + ("m" if ml is not None and ml > 0 else "")
            where = f"{H.EAR_NAME[ear]} {KIND_NAME[kind]} {H.hz(int(f)) if f else '?'}"
            if f is None or int(f) not in (H.FREQS if base == "ac" else H.BC_FREQS):
                skipped.append(where)
                continue
            nr = _txt(p, NS + "TonePointStatus") == "NoResponse"
            v = H._level("NR" if nr else (round(lv) if lv is not None else None), kind)
            if v is None:
                skipped.append(f"{where}（{'沒有數值' if lv is None else f'數值 {lv:g} 超出範圍'}）")
                continue
            row = thr[ear][kind]
            if str(int(f)) in row and row[str(int(f))] != v:
                skipped.append(f"{where}（重複的點 {v}，保留 {row[str(int(f))]}）")
                continue
            row[str(int(f))] = v
    thr = H.clean_data({"thr": thr})["thr"]
    count = sum(len(thr[e][k]) for e in H.EARS for k in H.KINDS)
    return {"thr": thr, "count": count, "masking_info": masking_info, "skipped": skipped}


def audiogram_diff(old: Dict[str, Any], new: Dict[str, Any], limit: int = 12) -> List[str]:
    """兩份聽力圖的差異（比最終閾值：有遮蔽值就用遮蔽值）。"""
    a, b = {"thr": old}, {"thr": new}
    out = []
    for ear in H.EARS:
        for kind, freqs, fn in (("ac", H.FREQS, H.final_ac), ("bc", H.BC_FREQS, H.final_bc)):
            for f in freqs:
                x, y = fn(a, ear, f), fn(b, ear, f)
                if x != y:
                    out.append(f"{H.EAR_NAME[ear]} {KIND_NAME[kind]} {H.hz(f)}："
                               f"{'—' if x is None else x} → {'—' if y is None else y}")
    return out if len(out) <= limit else out[:limit] + [f"…另 {len(out) - limit} 點"]


# ── ReSound 選配 ────────────────────────────────────────────────────

def _match_value(v: Optional[str], ids: List[str]) -> Optional[str]:
    """InitialFitFeatureValue 有時不是值清單裡的字（如 'AFX-DFS and AFGC mild'、'ABWA Wide'），
    找清單裡被它包含的值；再不行反過來找包含它的（'Off' → 'LevelOff'）。"""
    if not v or v in ids:
        return v
    lv = v.lower()
    inner = sorted((i for i in ids if i and i.lower() in lv), key=len, reverse=True)
    if inner:
        return inner[0]
    outer = [i for i in ids if i and lv in i.lower()]
    return outer[0] if len(outer) == 1 else v


def _gains(text: Optional[str]) -> List[Optional[int]]:
    vals = {int(f): round(float(g)) for f, g in re.findall(r"\((\d+) (-?[\d.]+)\)", text or "")}
    return [vals.get(f) for f in H.FREQS]


def parse_resound(xml_bytes: bytes) -> Optional[Dict[str, Any]]:
    """ReSound <ActiveProduct> → {ear, model, programs:[{loc, name, features{id:{value,init}}, gains}]}。"""
    root = _xml(xml_bytes)
    if root.tag != "ActiveProduct":
        return None
    ear = {"RIGHT_EAR": "R", "LEFT_EAR": "L"}.get(root.get("EarSide") or "")
    if not ear:
        return None
    programs = []
    for p in _kids(root, "Programs"):
        name, loc = (p.get("DisplayName") or "").strip(), p.get("Location") or ""
        if name in ("", "Program"):
            name = RS_PROGRAM_NAME.get(p.get("EnvironmentId") or "", f"程式 {loc}")
        feats = {}
        for fe in _kids(p, "Features"):
            fid = fe.get("FeatureID")
            if fid in RS_FEATURE_IDS:
                ids = [v.get("FeatureValueId") for v in fe.iter("FeatureValue")]
                feats[fid] = {"value": fe.get("FeatureValue"),
                              "init": _match_value(fe.get("InitialFitFeatureValue"), ids)}
        gains = {key: _gains(g.text) for g in _kids(p, "Gains")
                 for lvl, key in GAIN_LEVELS if g.get("InputLevel") == lvl}
        mpo = p.find("MPO")
        if mpo is not None:
            gains["mpo"] = _gains(mpo.text)
        programs.append({"loc": loc, "name": name, "features": feats, "gains": gains})
    return {"ear": ear, "model": root.get("Name") or "", "programs": programs}


def _feature_rows(latest: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """兩耳最新選配 → 畫面用的列：每個程式 × 每個功能，左右耳各給「目前／初始」的畫面文字。"""
    progs: Dict[str, str] = {}
    for fit in latest.values():
        for p in fit["programs"]:
            progs.setdefault(p["loc"], p["name"])
    rows = []
    for loc, pname in sorted(progs.items(), key=lambda x: (len(x[0]), x[0])):
        feats = []
        for fid, label, tab, names in RS_FEATURES:
            cell: Dict[str, Any] = {}
            for ear, fit in latest.items():
                p = next((q for q in fit["programs"] if q["loc"] == loc), None)
                f = p and p["features"].get(fid)
                if f:
                    cell[ear] = {"value": names.get(f["value"], f["value"]),
                                 "init": names.get(f["init"], f["init"]) if f["init"] else None,
                                 "changed": bool(f["init"]) and f["init"] != f["value"]}
            if cell:
                feats.append({"id": fid, "label": label, "tab": tab, **cell})
        rows.append({"loc": loc, "name": pname, "features": feats,
                     "gains": {ear: next((q["gains"] for q in fit["programs"] if q["loc"] == loc), {})
                               for ear, fit in latest.items()}})
    return rows


def _fit_changes(prev: Dict[str, Any], cur: Dict[str, Any]) -> List[str]:
    """同一耳前後兩次存檔的功能差異（用畫面文字）。"""
    names = {fid: (label, n) for fid, label, _tab, n in RS_FEATURES}
    out = []
    if prev["model"] != cur["model"]:
        out.append(f"機型 {prev['model']} → {cur['model']}")
    for p in cur["programs"]:
        q = next((x for x in prev["programs"] if x["loc"] == p["loc"]), None)
        if q is None:
            continue
        for fid, f in p["features"].items():
            g = q["features"].get(fid)
            if g and g["value"] != f["value"]:
                label, n = names[fid]
                out.append(f"{p['name']} {label} {n.get(g['value'], g['value'])} → {n.get(f['value'], f['value'])}")
    return out


# ── 主入口 ──────────────────────────────────────────────────────────

def _patient_name(p: ET.Element) -> str:
    first, last = (p.get("FirstName") or "").strip(), (p.get("LastName") or "").strip()
    if not first or not last or first == last:
        return first or last          # 門市習慣把全名同時填在姓與名
    if re.search(r"[一-鿿]", first + last):
        return last + first
    return f"{first} {last}"


def parse_nhax(raw: bytes) -> Dict[str, Any]:
    if not raw:
        raise NoahError("沒有收到檔案內容。")
    patient_xml = next((s for s in _sections(raw)
                        if re.match(rb"(\xef\xbb\xbf)?\s*(<\?xml[^>]*\?>)?\s*<Patient[\s>]", s[:400])), None)
    if patient_xml is None:
        raise NoahError("這不是 Noah 匯出的客戶檔（.nhax），或檔案已損壞。")
    try:
        root = _xml(patient_xml)
    except ET.ParseError:
        raise NoahError("Noah 檔內容無法解讀，檔案可能已損壞。")

    audiograms: List[Dict[str, Any]] = []
    fits: List[Dict[str, Any]] = []
    others: Dict[tuple, Dict[str, Any]] = {}
    bad = 0
    for session in root.iter("Session"):
        for a in session.findall("Action"):
            code, module = a.get("DataTypeCode"), a.get("ModuleId")
            when = _dt(a.get("CreateDate")) or _dt(session.get("CreateDate"))
            try:
                if code == "1":
                    pub = _b64(_txt(a, "PublicData"))
                    if b"HIMSAAudiometricStandard" not in pub[:1000]:
                        continue
                    ag = parse_audiogram(pub)
                    dev = _device_info(_b64(_txt(a, "PrivateData")))
                    audiograms.append({"datetime": when, "date": when[:10],
                                       "source": dev["device"] or (a.get("Description") or "").strip() or "聽力圖",
                                       "measured": bool(dev["device"]), **dev, **ag})
                elif code in ("131", "132") and module == RESOUND_MODULE:
                    fit = parse_resound(_gunzip(_b64(_txt(a, "PrivateData"))) or b"")
                    if fit:
                        fits.append({"datetime": when, **fit})
                    else:
                        bad += 1
                elif code in ("129", "130") and module != RESOUND_MODULE:
                    ear = "L" if code == "129" else "R"
                    text = (a.get("Description") or "").strip()
                    if text:
                        others[(ear, text)] = {"datetime": when, "ear": ear, "text": text}
            except (ET.ParseError, NoahError, ValueError, zlib.error):
                bad += 1

    audiograms.sort(key=lambda x: x["datetime"])
    for i, ag in enumerate(audiograms):
        ag["index"] = i
        ag["diff_prev"] = audiogram_diff(audiograms[i - 1]["thr"], ag["thr"]) if i else []

    fits.sort(key=lambda x: x["datetime"])
    latest: Dict[str, Dict[str, Any]] = {}
    history = []
    for fit in fits:
        prev = latest.get(fit["ear"])
        changes = _fit_changes(prev, fit) if prev else []
        g65 = next((p["gains"].get("65") for p in fit["programs"] if p["loc"] == "1"), None)
        prev_g65 = prev and next((p["gains"].get("65") for p in prev["programs"] if p["loc"] == "1"), None)
        if prev is None or changes or g65 != prev_g65:     # 沒改任何東西的重複存檔不列
            history.append({"datetime": fit["datetime"], "ear": fit["ear"], "model": fit["model"],
                            "g65": g65, "changes": changes})
        latest[fit["ear"]] = fit

    fitting = None
    if latest:
        fitting = {
            "software": "ReSound Smart Fit",
            "devices": {e: {"model": f["model"], "datetime": f["datetime"]} for e, f in latest.items()},
            "programs": _feature_rows(latest),
            "history": history,
        }
    return {
        "patient": {"no": root.get("PatientNo") or "", "name": _patient_name(root),
                    "birth_date": (root.get("BirthDate") or "")[:10] or None},
        "audiograms": audiograms,
        "fitting": fitting,
        "other_fittings": sorted(others.values(), key=lambda x: x["datetime"]),
        "freqs": list(H.FREQS),
        "warnings": [f"有 {bad} 筆紀錄無法解讀，已略過。"] if bad else [],
    }
