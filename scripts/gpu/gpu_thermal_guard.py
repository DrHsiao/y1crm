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
MISSING_FLAG = "/tmp/gpu_thermal_card_missing"   # 有卡沒讀到溫度（掉線/報錯）

# 應在線的卡（序號 → 名稱）。少任何一張就記 CARD_MISSING 並立旗標，不可靜默。
# 換卡/拆卡時要同步改這裡。
EXPECTED_CARDS = {
    "1422019000002": "0002 好卡(OCR)",
    "1422019000573": "573 軟壞卡(純文字)",
}
FIELDS = "index,serial,temperature.gpu,power.limit"


class QueryError(RuntimeError):
    """nvidia-smi 查不到溫度＝溫控等於沒有保護，必須大聲記錄，不可靜默。"""


def _run_query(extra_args=()):
    return subprocess.run(
        ["nvidia-smi", *extra_args, f"--query-gpu={FIELDS}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    )


def _parse(stdout: str, gpus: list, bad: list):
    """逐行解析；單張卡回 [Unknown Error]/[GPU requires reset] 只記錯該卡，不拖累其他卡。"""
    for line in stdout.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) != 4:
            bad.append(line[:120])
            continue
        idx, serial, temp, limit = parts
        try:
            gpus.append({"index": idx, "serial": serial, "temp": int(temp), "limit": float(limit)})
        except ValueError:
            bad.append(line[:120])


def _nvidia_bus_ids():
    try:
        return sorted(d for d in os.listdir("/sys/bus/pci/drivers/nvidia") if d.count(":") == 2)
    except OSError:
        return []


def query_gpus():
    """回傳 (讀得到的卡, 錯誤訊息清單)。

    先整批查；整批失敗（常見於某張卡掉線拖垮查詢）時退回逐卡用 bus 查，
    讓仍健康的卡照樣受保護。全部讀不到才丟 QueryError。
    """
    gpus, bad = [], []
    r = _run_query()
    if r.returncode == 0:
        _parse(r.stdout, gpus, bad)
    else:
        bad.append("整批查詢失敗: " + (r.stderr or r.stdout).strip().replace("\n", " ")[:200])
        for bus in _nvidia_bus_ids():
            try:
                rr = _run_query(("-i", bus))
            except subprocess.TimeoutExpired:
                bad.append(f"{bus} 逾時")
                continue
            if rr.returncode == 0:
                _parse(rr.stdout, gpus, bad)
            else:
                bad.append(f"{bus}: " + (rr.stderr or rr.stdout).strip().replace("\n", " ")[:120])
    if not gpus:
        raise QueryError("; ".join(bad) or "nvidia-smi 回報 0 張 GPU")
    return gpus, bad


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
        gpus, errors = query_gpus()
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

    for e in errors:
        log(f"CARD_ERROR {e}")
    seen = {g["serial"] for g in gpus}
    missing = [f"{sn}({name})" for sn, name in EXPECTED_CARDS.items() if sn not in seen]
    if missing:
        log("CARD_MISSING " + " ".join(missing))
        with open(MISSING_FLAG, "w") as f:
            f.write(datetime.now().isoformat() + " " + " ".join(missing) + "\n")
        print(f"[gpu_thermal_guard] 以下卡未受溫控保護：{' '.join(missing)}")
    elif os.path.exists(MISSING_FLAG):
        os.remove(MISSING_FLAG)
        log("CARD_ALL_PRESENT")

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
