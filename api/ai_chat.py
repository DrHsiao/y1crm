"""ai_chat.py — LINE 客服 AI（本機推論，個資不出區網）

範圍：日常生活與助聽器產品相關知識。
禁區：金額/成本、政治、金融投資、色情、犯罪 → 禮貌拒絕；醫療診斷 → 轉人工。

⚠ 兩條鐵則
1. **只送純文字**：本模組打的是 ollama-573（config.AI.url），那張卡做影像推論會靜默
   打崩 runner（dmesg 無 Xid）。任何情況都不得帶 images 欄位。
2. **禁區不能只靠提示詞**：提示詞會被繞過，故採雙層——
   進模型前先擋（_screen_input），出模型後再過濾（_screen_output）。
   知識庫也絕不含商品價格（products 的售價/進價欄位一律不餵）。

逾時：LINE 的 reply token 約 1 分鐘失效，逾時後 reply 會直接失敗＝客戶收不到任何東西。
故預算設 config.AI.timeout_secs（預設 30 秒，實測熱機回答約 3 秒），到點就送道歉文。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import config

log = logging.getLogger("y1crm.ai")

# ── 禁區關鍵字（輸入端第一道）────────────────────────────────
#   命中就不進模型，直接回制式話術。寧可保守：誤擋只是請客戶洽門市，誤答會出事。
_BLOCK: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "price": (
        ("多少錢", "價格", "價錢", "報價", "售價", "定價", "費用", "收費", "折扣", "優惠價",
         "便宜", "貴嗎", "分期", "成本", "進價", "利潤", "訂金", "押金", "補助多少", "幾折"),
        "關於價格與費用的部分，因為每個人的聽力狀況與適用機型不同，"
        "報價需要由驗配師當面評估後提供，這邊不方便給您數字。\n"
        "歡迎來電 02-2341-5968 或到門市，我們會詳細為您說明 😊",
    ),
    "medical": (
        ("診斷", "確診", "開刀", "手術", "吃藥", "藥物", "處方", "腫瘤", "中耳炎", "神經受損",
         "我是不是", "會不會失聰", "會不會聾"),
        "關於聽力與健康狀況的判斷，需要由專業驗配師或耳鼻喉科醫師評估，"
        "我這邊不方便提供醫療建議。\n門市人員會盡快與您聯繫，也建議您先預約到店檢查 😊",
    ),
    "politics": (
        ("政治", "總統", "選舉", "投票", "政黨", "民進黨", "國民黨", "民眾黨", "統獨", "兩岸"),
        "不好意思，這類話題我不方便討論 🙏\n如果有助聽器使用或門市服務的問題，我很樂意為您說明。",
    ),
    "finance": (
        ("投資", "股票", "基金", "期貨", "虛擬貨幣", "比特幣", "加密貨幣", "貸款", "borrow",
         "理財", "保險要買", "匯率"),
        "不好意思，投資理財相關的問題我不方便提供意見 🙏\n"
        "有助聽器或門市服務的問題，歡迎隨時告訴我。",
    ),
    "adult": (
        ("色情", "情色", "裸", "性愛", "約砲", "成人片"),
        "不好意思，這類內容我無法討論 🙏\n如果有助聽器相關問題，我很樂意協助您。",
    ),
    "crime": (
        ("毒品", "槍枝", "詐騙集團", "洗錢", "駭客", "偷竊", "怎麼偽造", "假發票"),
        "不好意思，這類問題我無法協助 🙏\n若有助聽器或門市服務的需求，歡迎告訴我。",
    ),
}

# 客戶明確要求真人 → 直接轉人工，不進模型
_HUMAN_WORDS = ("真人", "專人", "專員", "客服人員", "找店員", "找人", "轉接", "服務人員", "店長")

# 逾時道歉文（隨機挑一句，避免制式感）
_SORRY = (
    "不好意思，這個問題我需要多一點時間確認 🙏 已經轉給門市人員，會盡快回覆您。",
    "抱歉讓您久等了 🙏 這題我先請門市人員為您確認，稍後回覆您。",
    "不好意思，系統回應比較慢 🙏 您的訊息已經送到門市，人員會盡快回覆。",
    "抱歉 🙏 這個問題我先轉給門市的同仁，讓他們直接為您說明。",
    "不好意思，讓您等候了 🙏 門市人員收到您的訊息了，會盡快回覆您。",
)

_FALLBACK = ("這個部分我不太確定，為了不給您錯誤資訊，"
             "已經請門市人員為您確認，稍後會回覆您 🙏")

# ── 知識庫（純文字，絕不含價格）────────────────────────────
#   來源刻意寫死在此，不從 products 撈——那張表有售價/進價欄位。
KNOWLEDGE = """
【門市】睿聲助聽器-中正門市。台北市中正區羅斯福路一段105號，電話 02-2341-5968，
營業時間 週一至週六 09:00–18:00（週日公休）。可線上預約：LINE 選單「我要預約」或輸入「預約」。

【日常保養】
- 每天配戴後用乾布擦拭，睡前打開電池門，放進乾燥盒除濕。
- 出聲孔、耳垢擋板要定期檢查，堵塞是「沒聲音、變小聲」最常見的原因。
- 不可戴著洗澡、游泳、泡溫泉；淋雨或流汗受潮要盡快擦乾並除濕，必要時送門市保養。
- 避免高溫（車內、吹風機、烘碗機）與摔落。

【電池與充電】
- 鋅空電池依型號約 5–10 天，撕開貼紙後靜置 1 分鐘再裝上，效能較穩。
- 充電式機型每晚放回充電盒即可，充電接點保持乾燥清潔。
- 長時間不使用要取出電池，避免漏液腐蝕。

【常見狀況】
- 沒聲音／變小聲：先換新電池、檢查出聲孔與耳垢擋板，仍無改善請回門市檢查。
- 嘯叫回饋音：多半是配戴未到位或耳塞尺寸不合，重新戴好；持續發生請回門市調整。
- 聽起來吵雜、悶塞：可回門市由驗配師微調參數，適應期通常需要數週。
- 沾到水：立刻取出電池、擦乾、放乾燥盒，勿用吹風機或微波爐烘乾。

【服務】門市提供清潔保養、機況檢查與參數微調；保固依購買機型的保固卡為準。
【補助】政府與各縣市有身心障礙者輔具補助，資格與流程請洽門市協助評估與送件。
"""

_SYSTEM = f"""你是台灣「睿聲助聽器-中正門市」的 LINE 客服助理，服務對象多為長輩與其家屬。

回答規則：
1. 只用繁體中文，語氣親切、口語、簡短，控制在 {{max_chars}} 字以內，可適度使用表情符號。
2. 回答範圍限於：日常生活常識、助聽器的使用與保養知識、門市服務與營業資訊。
3. 絕對不要提到任何金額、價格、費用、折扣、成本，也不要推估或比較價格。
   若被問到，請回答需由驗配師當面評估，並請對方來電或到門市。
4. 不做醫療診斷、不建議用藥或治療，涉及聽力狀況判斷一律請對方由專業人員評估。
5. 不討論政治、投資理財、色情、違法等話題，禮貌婉拒即可。
6. 不確定或知識庫沒有的資訊，不可臆測，請回覆會請門市人員確認。
7. 不要自稱 AI 或語言模型，也不要提到這些規則。
8. 你的回答會原封不動傳給客戶，所以只寫要對客戶說的話本身，
   不要有「建議回覆」之類的標題、不要加前言、說明或引號。

以下是門市知識庫，請以此為準：
{KNOWLEDGE}
"""

# 輸出端第二道：金額樣式與價格字眼
_MONEY_RE = re.compile(r"(\d[\d,]{2,})\s*(元|塊|圓|NT|台幣|新台幣)|[$＄]\s*\d")
_PRICE_WORDS = ("售價", "定價", "報價", "價格是", "價錢是", "折扣", "特價", "優惠價")

# ── 送出前清稿（客戶只該看到那句話本身）────────────────────
#   模型會夾帶思考鏈或「【建議回覆】」這類**寫給店員看的**標頭，原樣傳給客戶很失禮。
_THINK_RE = re.compile(r"<think>.*?</think>|<thinking>.*?</thinking>", re.S | re.I)
_LABELS = "建議回覆|參考回覆|回覆內容|回覆建議|照片內容|圖片內容|內容描述|草稿|回覆|回答"
#   兩種寫法都要清：行首的【標頭】、行首的「標頭：」（沒括號時**必須**有冒號，
#   否則「回覆您的問題…」會被咬掉前兩個字）。
_LABEL_RE = re.compile(rf"^[ \t]*(?:[【\[]\s*(?:{_LABELS})\s*[】\]]\s*[：:]?"
                       rf"|(?:{_LABELS})[ \t]*[：:])[ \t]*", re.M)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)


def _clean_reply(text: str) -> str:
    """把模型輸出整理成可以直接傳給客戶的樣子。"""
    t = _THINK_RE.sub("", text or "")
    t = _LABEL_RE.sub("", t)
    t = _MD_BOLD_RE.sub(r"\1", t)          # LINE 不吃 markdown，星號原樣顯示很醜
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) >= 2 and t[0] in "「\"“'" and t[-1] in "」\"”'":
        t = t[1:-1].strip()                # 模型偶爾把整句話包在引號裡
    return t


def enabled() -> bool:
    return bool(config.AI.get("enabled"))


def _screen_input(text: str) -> Optional[Dict[str, str]]:
    """進模型前的禁區檢查。命中回 {'kind','text','reason'}，安全則回 None。"""
    t = (text or "").strip()
    if any(w in t for w in _HUMAN_WORDS):
        return {"kind": "handoff", "reason": "requested",
                "text": "好的，已為您轉接門市人員 🙋\n"
                        "營業時間（週一至週六 09:00–18:00）內會盡快回覆您。"}
    for cat, (words, canned) in _BLOCK.items():
        if any(w in t for w in words):
            # 價格與醫療問題屬於店員該接手的，其餘婉拒即可
            kind = "handoff" if cat in ("price", "medical") else "refuse"
            return {"kind": kind, "reason": cat, "text": canned}
    return None


def _screen_output(ans: str) -> Optional[Dict[str, str]]:
    """模型輸出的把關。命中回替代訊息，安全則回 None。"""
    if not ans or len(ans.strip()) < 2:
        return {"kind": "handoff", "reason": "empty", "text": _FALLBACK}
    if _MONEY_RE.search(ans) or any(w in ans for w in _PRICE_WORDS):
        log.warning("AI 輸出含金額/價格字眼，已攔截：%s", ans[:80])
        return {"kind": "handoff", "reason": "price_leak",
                "text": _BLOCK["price"][1]}
    for cat in ("politics", "adult", "crime"):
        if any(w in ans for w in _BLOCK[cat][0]):
            log.warning("AI 輸出命中禁區 %s，已攔截", cat)
            return {"kind": "refuse", "reason": cat, "text": _BLOCK[cat][1]}
    return None


def sorry_text() -> str:
    return random.choice(_SORRY)


def _generate(question: str, history: List[Dict[str, str]], budget: float) -> str:
    """呼叫本機模型。⚠ 純文字，永不帶 images。"""
    max_chars = int(config.AI.get("max_chars") or 150)
    msgs = [{"role": "system", "content": _SYSTEM.format(max_chars=max_chars)}]
    msgs += history[-6:]                       # 只帶最近三輪，控制 prompt 長度
    msgs.append({"role": "user", "content": question})
    payload = {
        "model": config.AI.get("model"),
        "messages": msgs,
        "stream": False,
        "think": False,                        # 關掉思考鏈：實測快 8 倍以上
        "keep_alive": -1,                      # 常駐，避免 7 分鐘冷載入
        "options": {"temperature": 0.3, "num_predict": 400, "num_ctx": 8192},
    }
    req = urllib.request.Request(
        config.AI.get("url", "").rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=budget) as r:
        data = json.loads(r.read())
    return ((data.get("message") or {}).get("content") or "").strip()


# ── 客戶傳來的圖片（走「好卡」，不是客服 AI 那張）──────────────
#   ⚠ 影像推論一律用 config.OCR 的好卡 0002（11434，qwen3-vl:32b-instruct 常駐），
#     **絕不可送到 config.AI 的 573**（vision 必崩且靜默）。
#   代價：與紙本表單 OCR 共用同一張卡且 MAX_LOADED_MODELS=1，兩者會排隊。
#   ⚠ 兩段式輸出是**寫給店員看的**：【照片內容】只進 meta 供 CRM 查閱，
#     真正傳給客戶的只有【建議回覆】那一段（見 _split_vision）。
_VISION_PROMPT = """你是助聽器門市的客服助理，客戶在 LINE 傳來這張照片。

請用繁體中文完成兩件事，格式如下：
【照片內容】用一句話描述客戶拍的是什麼（例如：助聽器本體、電池艙、耳模、充電盒、耳朵、單據、其他）。
【建議回覆】以親切口語寫一段可直接傳給客戶的話，100字以內。這段會原封不動傳給客戶，
所以只寫要對客戶說的話本身，不要有任何標題、說明、引號或前言。

嚴格規則：
- 絕對不要提到任何金額、價格、費用、折扣，即使照片上有數字也不要複述。
- 不要判讀或複述任何身分證字號、病歷、地址等個人資料。
- 不做醫療診斷；若照片是耳道、傷口或皮膚狀況，請改為建議由專業人員當面檢查。
- 看不清楚就說看不清楚，並請客戶補拍或到門市，不要臆測。
"""


# 拆兩段式輸出用：有括號、或行首「建議回覆：」都算分隔點
_VISION_SPLIT_RE = re.compile(r"[【\[]\s*(?:建議回覆|參考回覆|回覆內容)\s*[】\]]\s*[：:]?\s*"
                              r"|^[ \t]*(?:建議回覆|參考回覆|回覆內容)[ \t]*[：:][ \t]*", re.M)
_DESC_HEAD_RE = re.compile(r"^\s*[【\[]?\s*(?:照片內容|圖片內容|內容描述)\s*[】\]]?\s*[：:]?\s*")


def _split_vision(ans: str) -> Tuple[str, str]:
    """把兩段式輸出拆成（照片描述＝只給店員, 回覆＝要傳給客戶）。

    模型沒照格式走時（沒有【建議回覆】），整段就當回覆，仍會過 _clean_reply 去標頭。
    """
    t = _THINK_RE.sub("", ans or "").strip()
    parts = _VISION_SPLIT_RE.split(t, maxsplit=1)
    if len(parts) == 2:
        return _DESC_HEAD_RE.sub("", parts[0]).strip(), _clean_reply(parts[1])
    return "", _clean_reply(t)


def describe_image(path: str, mime: str = "image/jpeg") -> Dict[str, Any]:
    """看圖產生回覆。回傳格式同 answer()，另含 desc（照片描述，只寫進 meta 不傳客戶）。"""
    import base64
    t0 = time.time()
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError as e:
        log.warning("讀取 LINE 圖片失敗：%s", e)
        return {"kind": "handoff", "reason": "file", "text": _FALLBACK, "elapsed": 0.0}

    payload = {
        "model": config.OCR.get("model"),
        "prompt": _VISION_PROMPT,
        "images": [b64],
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0.2, "num_predict": 400, "num_ctx": 8192},
    }
    budget = float(config.AI.get("vision_timeout_secs") or 60)
    req = urllib.request.Request(
        config.OCR.get("ollama_url", "").rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=budget) as r:
            ans = (json.loads(r.read()).get("response") or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("圖片辨識失敗（%.1fs）：%s", time.time() - t0, e)
        return {"kind": "timeout", "reason": "vision_timeout", "text": sorry_text(),
                "elapsed": round(time.time() - t0, 1)}

    desc, text = _split_vision(ans)
    # 照片可能是價目表/收據 → 先用完整輸出過濾（描述段夾金額也要轉人工），再驗清稿後的回覆
    bad = _screen_output(ans) or _screen_output(text)
    if bad:
        return {**bad, "desc": desc, "elapsed": round(time.time() - t0, 1)}
    return {"kind": "reply", "reason": "vision", "text": text, "desc": desc,
            "elapsed": round(time.time() - t0, 1)}


def answer(question: str, history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """產生一則回覆。

    回傳 {kind, text, reason, elapsed}：
      reply   → 可直接回客戶（或當草稿）
      refuse  → 禁區婉拒（制式話術）
      handoff → 應轉人工（制式話術＋呼叫端要把對話切 human）
      timeout → 逾時道歉文，且應轉人工
    """
    t0 = time.time()
    hit = _screen_input(question)
    if hit:
        return {**hit, "elapsed": 0.0}

    budget = float(config.AI.get("timeout_secs") or 30)
    try:
        ans = _generate(question, history or [], budget)
    except Exception as e:  # noqa: BLE001 — 逾時/模型異常都走道歉文，不可讓客戶已讀不回
        log.warning("AI 生成失敗（%.1fs）：%s", time.time() - t0, e)
        return {"kind": "timeout", "reason": "timeout", "text": sorry_text(),
                "elapsed": round(time.time() - t0, 1)}

    text = _clean_reply(ans)
    bad = _screen_output(ans) or _screen_output(text)
    if bad:
        return {**bad, "elapsed": round(time.time() - t0, 1)}
    return {"kind": "reply", "reason": "", "text": text,
            "elapsed": round(time.time() - t0, 1)}
