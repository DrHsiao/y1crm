#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主機維運通知（Telegram）— 單一發送點（2026-09-26）

所有主機層的告警（健康檢查、mdadm、smartd）都走這支，只發給 John 一人
（.openclaw.env 的 TELEGRAM_CHAT_ID），不發給交易推播的其他訂閱者。

用法：
    notify.py "訊息"                  # 直接發
    echo "訊息" | notify.py            # 從 stdin
    notify.py --flush                  # 只補送佇列（健康檢查每輪會呼叫）

    from notify import send            # 程式內呼叫；回傳是否送達

## 送不出去時（不可靜默）
- 訊息進佇列 /var/lib/ops/notify_spool.jsonl，下次任何一次發送前先補送（最多保留 48 小時）。
- 建立旗標 /tmp/ops_notify_failing、寫 log；恢復時移除並記 RECOVERED。
- 每日摘要會列出佇列積壓數；摘要本身沒收到＝整條管道壞了。

stdlib only，用系統 /usr/bin/python3 執行（不依賴任何專案 venv）。
"""
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

ENV_FILE = "/home/john/.openclaw.env"         # 沿用 openclaw bot（John 2026-09-26 決定）
STATE_DIR = "/var/lib/ops"
SPOOL = os.path.join(STATE_DIR, "notify_spool.jsonl")
LOG_DIR = "/var/log/ops"                       # 放 SSD：/mnt/raid1 是單碟，壞了 log 也跟著沒
LOG = os.path.join(LOG_DIR, "notify.log")
FAIL_FLAG = "/tmp/ops_notify_failing"
SPOOL_MAX_AGE = 48 * 3600
TIMEOUT = 10
PREFIX = f"🖥 主機 {socket.gethostname()}"
MAX_LEN = 3900                                 # Telegram 上限 4096，留餘裕


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def _creds():
    env = {}
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    tok, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        raise RuntimeError(f"{ENV_FILE} 缺 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    return tok, chat


def _post(text):
    tok, chat = _creds()
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:MAX_LEN],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = json.load(r)
    if not body.get("ok"):
        raise RuntimeError(f"Telegram 回應 ok=false：{body.get('description')}")


def _spool_load():
    try:
        with open(SPOOL, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def _spool_save(items):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = SPOOL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, SPOOL)


def _mark_fail(err):
    first = not os.path.exists(FAIL_FLAG)
    with open(FAIL_FLAG, "w") as f:
        f.write(f"{datetime.now().isoformat()} {err}\n")
    log(f"SEND_FAILED {err}")
    if first:
        print(f"[notify] ⚠ Telegram 發送失敗，已進佇列：{err}", file=sys.stderr)


def _mark_ok():
    if os.path.exists(FAIL_FLAG):
        os.remove(FAIL_FLAG)
        log("RECOVERED")


def flush():
    """補送佇列；回傳剩餘筆數。"""
    items = _spool_load()
    if not items:
        return 0
    now = time.time()
    keep, dropped = [], 0
    for i, it in enumerate(items):
        if now - it["ts"] > SPOOL_MAX_AGE:
            dropped += 1
            continue
        try:
            _post(f"{it['text']}\n\n（延遲送達，原時間 {datetime.fromtimestamp(it['ts']):%m-%d %H:%M}）")
        except Exception as e:
            _mark_fail(e)
            keep = [x for x in items[i:] if now - x["ts"] <= SPOOL_MAX_AGE]
            break
    else:
        _mark_ok()
    if dropped:
        log(f"SPOOL_DROPPED {dropped} 筆（超過 48 小時）")
    _spool_save(keep)
    return len(keep)


def pending():
    return len(_spool_load())


def send(text):
    """發送（前面自動加主機前綴）；失敗進佇列。回傳是否送達。"""
    text = f"{PREFIX}\n{text.strip()}"
    if flush():                       # 佇列還塞著＝管道仍不通，新訊息排在後面保持順序
        items = _spool_load()
        items.append({"ts": time.time(), "text": text})
        _spool_save(items)
        return False
    try:
        _post(text)
        _mark_ok()
        log("SENT " + text.replace("\n", " ⏎ ")[:300])
        return True
    except Exception as e:
        items = _spool_load()
        items.append({"ts": time.time(), "text": text})
        _spool_save(items)
        _mark_fail(e)
        return False


if __name__ == "__main__":
    if sys.argv[1:] == ["--flush"]:
        left = flush()
        sys.exit(1 if left else 0)
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    if not msg.strip():
        print("用法：notify.py \"訊息\"", file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if send(msg) else 1)
