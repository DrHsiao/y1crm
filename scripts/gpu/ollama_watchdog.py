#!/usr/bin/env python3
"""ollama 常駐模型看門狗：模型掉出 VRAM 就自動載回，並留下可追查的紀錄。

為什麼需要：`KEEP_ALIVE=-1` 只保證「已載入的不會因閒置被卸載」，擋不掉別的請求
把它擠掉。MAX_LOADED_MODELS=1 之下，只要有人向同一個實例要**別的模型**，ollama 會
先卸掉常駐那顆；若新模型冷載入（HDD RAID1 約 455s）超過呼叫端的 timeout，載入被取消，
**兩顆都不在卡上**。2026-08-07 就這樣發生過：TWIII 的 telegram bot 要 deepseek-r1:32b
擠掉 LINE 客服的 qwen3:32b，之後每則客戶文字訊息都跑滿 30s 逾時、收到道歉文、
被自動轉人工 4 小時——**全程沒有任何錯誤或告警**，是最難發現的那種故障。

行為：
  - 該在的模型還在      → OK，什麼都不做
  - 卡上空的            → 自動送 num_predict=1 的請求把它載回（RELOADED）
  - 卡上是別的模型      → **不驅逐**（可能有人正在用），只記 OTHER_MODEL 並告警
  - API 連不上          → FAIL 並告警

告警＝寫 /tmp/ollama_watchdog_failing 旗標 + stderr（systemd 會記進 journal 並把
unit 標成 failed，`systemctl --failed` 看得到）。模型名與 ollama_warmup.sh 同一份清單，
改模型時兩邊都要改。
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

# （名稱, API, 應常駐的模型）——與 /home/john/workspace/ollama_warmup.sh 一致
ENDPOINTS = [
    ("OCR/好卡0002", "http://127.0.0.1:11434", "qwen3-vl:32b-instruct"),
    ("客服AI/573", "http://127.0.0.1:11435", "qwen3:32b"),
]

LOG_FILE = "/mnt/raid1/monitoring/ollama_watchdog.log"
FAIL_FLAG = "/tmp/ollama_watchdog_failing"
MAX_LOG_BYTES = 10 * 1024 * 1024

PS_TIMEOUT = 10        # 查詢很輕，不該慢
LOAD_TIMEOUT = 1200    # 冷載入最慢約 455s（blob 在 HDD），給足餘裕


def log(line: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{now} {line}\n")
    except OSError as e:      # log 寫不進去不該讓看門狗自己掛掉
        print(f"[ollama_watchdog] 無法寫 log：{e}", file=sys.stderr)


def _post(url: str, payload: dict, timeout: float):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def loaded_models(base: str) -> list:
    with urllib.request.urlopen(f"{base}/api/ps", timeout=PS_TIMEOUT) as r:
        return [m.get("name") for m in (json.loads(r.read()).get("models") or [])]


def warm(base: str, model: str) -> float:
    """把模型載回 VRAM。num_predict=1＝只觸發載入，不需要真的生成內容。"""
    t0 = time.time()
    _post(f"{base}/api/generate",
          {"model": model, "prompt": "hi", "stream": False,
           "keep_alive": -1, "options": {"num_predict": 1}}, LOAD_TIMEOUT)
    return time.time() - t0


def check(tag: str, base: str, model: str) -> bool:
    """回 True 表示這個實例現在是好的。"""
    try:
        models = loaded_models(base)
    except Exception as e:                       # noqa: BLE001
        log(f"FAIL {tag} {base} 查詢失敗 {type(e).__name__}: {e}")
        return False

    if model in models:
        log(f"OK {tag} {model}")
        return True

    if models:      # 別人的模型正在用，不搶——但要讓人知道常駐的那顆不在
        log(f"OTHER_MODEL {tag} 應為 {model}，實際載入 {','.join(models)}")
        return False

    log(f"MISSING {tag} {model} 不在 VRAM，開始載回")
    try:
        secs = warm(base, model)
    except Exception as e:                       # noqa: BLE001
        log(f"RELOAD_FAILED {tag} {model} {type(e).__name__}: {e}")
        return False
    log(f"RELOADED {tag} {model} {secs:.0f}s")
    # 已自動修好，但要留痕：這代表有人把模型擠掉了，值得回頭查是誰
    print(f"[ollama_watchdog] {tag} 的 {model} 曾掉出 VRAM，已自動載回（{secs:.0f}s）",
          file=sys.stderr)
    return True


def main():
    healthy = all([check(*e) for e in ENDPOINTS])   # 用 list 確保每個都跑到
    if healthy:
        if os.path.exists(FAIL_FLAG):
            os.remove(FAIL_FLAG)
            log("RECOVERED")
        return
    with open(FAIL_FLAG, "w") as f:
        f.write(datetime.now().isoformat())
    print("[ollama_watchdog] 有實例不正常，詳見 " + LOG_FILE, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
