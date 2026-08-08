#!/usr/bin/env python3
"""GPU 溫度守護：偵測所有 V100 溫度，過熱時降功耗上限防止不穩，退燒後自動恢復。

以「序號」認卡（bus 位址每次冷開機會漂移，不可信）。
硬體本身在 87C 會自動 slowdown、90C shutdown；本機制在更低的門檻先行降功耗，
避免真的撞到硬體保護線導致算力驟降或 Xid 錯誤。
只做動作與記錄，不發通知。
"""
import json
import os
import subprocess
from datetime import datetime

STATE_FILE = "/tmp/gpu_thermal_state.json"
LOG_FILE = "/mnt/raid1/monitoring/gpu_thermal.log"

CRIT_TEMP = 80      # 降功耗上限（V100 87C 就硬體 slowdown，要在那之前先介入）
RECOVER_TEMP = 70   # 退燒後恢復原功耗上限（含滯後區間，避免來回抖動）
THROTTLE_LIMIT_W = 200   # 過熱時降到的功耗上限
NORMAL_LIMIT_W = 300     # 正常功耗上限

MAX_LOG_BYTES = 20 * 1024 * 1024   # 超過就轉存 .1，避免長期塞爆
FAIL_FLAG = "/tmp/gpu_thermal_failing"   # 查詢失敗旗標（連續失敗才視為異常）


class QueryError(RuntimeError):
    """nvidia-smi 查不到溫度＝溫控等於沒有保護，必須大聲記錄，不可靜默。"""


def query_gpus():
    r = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=index,serial,temperature.gpu,power.limit",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise QueryError((r.stderr or r.stdout).strip().replace("\n", " ")[:200])
    gpus = []
    for line in r.stdout.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 4:
            raise QueryError(f"無法解析 nvidia-smi 輸出: {line[:120]}")
        idx, serial, temp, limit = parts
        gpus.append({"index": idx, "serial": serial, "temp": int(temp), "limit": float(limit)})
    if not gpus:
        raise QueryError("nvidia-smi 回報 0 張 GPU")
    return gpus


def set_power_limit(index: str, watts: int) -> bool:
    r = subprocess.run(
        ["sudo", "nvidia-smi", "-i", index, "-pl", str(watts)],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def log(line: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:   # 超量就轉存一份，避免無限成長
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass
    with open(LOG_FILE, "a") as f:
        f.write(f"{now} {line}\n")


def main():
    try:
        gpus = query_gpus()
    except Exception as e:   # noqa: BLE001 — 失敗要留在 log 裡，不是 cron 的 traceback 垃圾
        log(f"QUERY_FAILED {type(e).__name__}: {e}")
        # 旗標檔供健康檢查/看板判讀：溫控目前沒有保護作用
        with open(FAIL_FLAG, "w") as f:
            f.write(datetime.now().isoformat())
        print(f"[gpu_thermal_guard] 查詢 GPU 失敗，溫控未生效：{e}")
        raise SystemExit(1)
    if os.path.exists(FAIL_FLAG):
        os.remove(FAIL_FLAG)
        log("QUERY_RECOVERED")

    state = load_state()

    for g in gpus:
        serial, temp, index = g["serial"], g["temp"], g["index"]
        throttled = state.get(serial, {}).get("throttled", False)

        if temp >= CRIT_TEMP and not throttled:
            if set_power_limit(index, THROTTLE_LIMIT_W):
                state[serial] = {"throttled": True}
                log(f"THROTTLE serial={serial} index={index} temp={temp}")
            else:
                log(f"THROTTLE_FAILED serial={serial} index={index} temp={temp}")
        elif temp <= RECOVER_TEMP and throttled:
            if set_power_limit(index, NORMAL_LIMIT_W):
                state[serial] = {"throttled": False}
                log(f"RECOVER serial={serial} index={index} temp={temp}")

        log(f"READ serial={serial} index={index} temp={temp} limit={g['limit']}")

    save_state(state)


if __name__ == "__main__":
    main()
