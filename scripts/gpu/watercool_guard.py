#!/usr/bin/env python3
"""水冷守衛：5717（序號 1565019015717）的水冷泵與功耗保護。2026-11-11 建立。

硬體（2026-11-11 實測）：
- 泵接主機板 SYS_FAN1（4-pin）＝ nct6779 的 pwm1 輸出 / fan1 測速。
- **必須用 PWM 模式**（pwm1_mode=1）；BIOS 預設的 DC 模式對這顆泵完全無效。
- 冷排目前沒裝風扇：閒置（~43W）撐得住；滿載 ~290W 時 HBM 約 215 秒到 85°C 上限。
- 泵 PWM→轉速：100%≈3200、50%≈2140、30%≈1440、20%≈1010、10%≈495 rpm（10% 時 6 秒升 4°C，流量不夠）。

平時不吵：
- 泵：閒置 30%；溫度或功耗上來才加速。加速立即、降速慢慢來（避免忽快忽慢）。
- 智能試停：夠冷、夠閒、穩定 10 分鐘就試停泵；一升溫就恢復 30%，失敗後間隔加倍再試。
- 推播：只在需要人處理時立旗標 /tmp/watercool_alert，由 ops healthcheck 推（狀態改變才推一次）。
- log：只記事件＋每小時一行摘要。

安全退路：守衛停止／當掉 → 泵回 BIOS 全速（SIGTERM 處理＋systemd ExecStopPost --restore）。
守衛運作時會寫 /run/watercool_guard.json（心跳）；gpu_thermal_guard.py 看到心跳就不碰 5717 的功耗，
心跳超過 30 秒沒更新則由它接手。

用法：
    watercool_guard.py            # 常駐（watercool-guard.service）
    watercool_guard.py --status   # 看目前狀態
    watercool_guard.py --restore  # 泵回 BIOS 全速（ExecStopPost 用）

stdlib only，用系統 /usr/bin/python3 以 root 執行。
"""
import argparse
import glob
import json
import math
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

SERIAL = "1565019015717"
HWMON_NAME = "nct6779"
CH = 1                      # SYS_FAN1：pwm1 輸出、fan1 測速
POLL_S = 2

# ── 泵速曲線（%），依 T = max(GPU 溫度, HBM 溫度) ─────────────────────────
DUTY_QUIET = 30             # 閒置檔位（約 1440 rpm）
CURVE_LO_C = 50             # 這以下＝安靜
CURVE_HI_C = 65             # 這以上＝全速；中間線性、無條件進位到 10%
LOAD_W = 120                # GPU 開始吃電就直接全速（冷排沒風扇，熱來得快）
DOWN_HOLD_S = 120           # 需求下降後先等這麼久才開始降速
DOWN_STEP = 10              # 每次降 10%
DOWN_STEP_S = 30            # 每 30 秒降一級

# ── 智能停泵：條件夠冷就試停；溫度一升就恢復 30%，間隔加倍再試 ──────────────
# 2026-11-11 實測：閒置 ~40W 停泵 5 秒升 3°C（AOI 佔著卡＝P0），現況幾乎每次都會失敗；
# 從靜止直接給 30%／20% 都能在 2 秒內轉起來（不需先全速衝）。
PROBE_ENABLE = True
PROBE_MAX_C = 45            # max(GPU,HBM) ≤ 這個才試
PROBE_MAX_W = 60            # 功耗 < 這個才試；停泵中超過就恢復
PROBE_STABLE_S = 600        # 安靜檔穩定 10 分鐘才試
PROBE_RISE_C = 3            # 停泵後比停泵當下高 3°C → 恢復
PROBE_JUDGE_S = 60          # 停滿 60 秒沒升溫＝試停成功，泵就一直停著
PROBE_BACKOFF_S = (1800, 3600, 7200, 14400)   # 失敗後 30 分→1→2→4 小時（封頂）再試

# ── 泵異常 ────────────────────────────────────────────────────────────────
STALL_RPM = 300             # 指令 ≥30% 但轉速低於此＝停轉
STALL_SAMPLES = 3           # 連續 3 次（約 6 秒）
EXPECTED_RPM = {100: 3200, 90: 3000, 80: 2820, 70: 2590, 60: 2350, 50: 2140, 40: 1780, 30: 1440}
WEAK_RATIO = 0.6            # 低於預期 60% 持續 30 秒＝偏弱（只記 log，不推播）
WEAK_SAMPLES = 15

# ── 功耗保護（只動 5717） ──────────────────────────────────────────────────
LIMIT_W = {0: 300, 1: 200, 2: 150}      # 150W＝這張卡的最低值
L1 = (78, 82)               # (GPU, HBM) 任一達到 → 200W
L2 = (82, 84)               # 任一達到 → 150W（HBM 官方上限 85、GPU 83）
RECOVER = (70, 75)          # 兩者都 ≤ 才開始恢復
RECOVER_HOLD_S = 60         # 每降一級要穩定 60 秒

# ── 推播條件 ──────────────────────────────────────────────────────────────
HOT_ALERT_S = 60            # 最低功耗還壓不住超過 60 秒
PUMP_CTL_ALERT_S = 60       # 無法讀寫泵超過 60 秒
ALERT_CLEAR_S = 120         # 條件消失 120 秒才算恢復（避免抖動重複推）

ALERT_FLAG = "/tmp/watercool_alert"
STATUS_FILE = "/run/watercool_guard.json"
LOG_FILE = "/mnt/raid1/monitoring/watercool_guard.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
SUMMARY_S = 3600

ALERT_TEXT = {
    "stall": "水泵停轉（已拉全速、功耗壓到 150W）",
    "hot": "已降到最低功耗 150W 仍過熱",
    "pump_ctl": "守衛無法讀寫水泵（nct6775 驅動？），泵維持 BIOS 全速但沒有停轉偵測",
}


def log(line: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a") as f:
            f.write(f"{now} {line}\n")
    except OSError:
        print(f"{now} {line}", flush=True)   # 資料碟沒掛上時退到 journal


# ── 硬體存取 ─────────────────────────────────────────────────────────────

def find_hwmon():
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(f"{d}/name") as f:
                if f.read().strip() == HWMON_NAME:
                    return d
        except OSError:
            pass
    return None


class Pump:
    def __init__(self):
        self.h = None

    def ensure(self) -> bool:
        if self.h and os.path.exists(f"{self.h}/pwm{CH}"):
            return True
        self.h = find_hwmon()
        if not self.h:
            subprocess.run(["modprobe", "nct6775"], capture_output=True, timeout=20)
            self.h = find_hwmon()
        return self.h is not None

    def _r(self, name) -> int:
        with open(f"{self.h}/{name}") as f:
            return int(f.read().strip())

    def _w(self, name, val):
        with open(f"{self.h}/{name}", "w") as f:
            f.write(str(val))

    def rpm(self) -> int:
        return self._r(f"fan{CH}_input")

    def apply(self, pct: int) -> bool:
        """設定泵速；若被別人改掉（enable/mode/值不對）就重設。回傳是否有重設。"""
        val = int(pct * 255 / 100 + 0.5)
        en = self._r(f"pwm{CH}_enable")
        # nct6775 驅動：手動模式＋pwm=255 時 enable 讀回 0（＝全速），不是被改掉
        if (self._r(f"pwm{CH}_mode") == 1 and self._r(f"pwm{CH}") == val
                and (en == 1 or (en == 0 and val == 255))):
            return False
        self._w(f"pwm{CH}_enable", 1)        # 手動
        self._w(f"pwm{CH}_mode", 1)          # PWM（DC 對這顆泵無效）
        self._w(f"pwm{CH}", val)
        return True

    def restore_bios(self):
        """BIOS 原設定：DC 模式、255、enable=0（全速、不受控）。"""
        if not self.ensure():
            return False
        self._w(f"pwm{CH}_mode", 0)
        self._w(f"pwm{CH}", 255)
        self._w(f"pwm{CH}_enable", 0)
        return True


GPU_FIELDS = "serial,uuid,temperature.gpu,temperature.memory,power.draw.instant,power.limit"


def _query(extra=()):
    return subprocess.run(["nvidia-smi", *extra, f"--query-gpu={GPU_FIELDS}", "--format=csv,noheader,nounits"],
                          capture_output=True, text=True, timeout=10)


def read_gpu(uuid_hint=None):
    """回傳 5717 的讀數 dict；卡不在回 None；查詢失敗丟例外。
    整批查詢失敗（常見於別張卡掉線）時改用 UUID 單查。"""
    r = _query()
    if r.returncode != 0 and uuid_hint:
        r = _query(("-i", uuid_hint))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().replace("\n", " ")[:160])
    for line in r.stdout.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) != 6 or p[0] != SERIAL:
            continue
        gpu = int(p[2])                      # GPU 溫度讀不到就讓它丟例外
        try:
            hbm = int(p[3])
        except ValueError:
            hbm = gpu
        try:
            power = float(p[4])
        except ValueError:
            power = 0.0
        return {"uuid": p[1], "gpu": gpu, "hbm": hbm, "power": power, "limit": float(p[5])}
    return None


def set_power_limit(uuid: str, watts: int) -> bool:
    r = subprocess.run(["nvidia-smi", "-i", uuid, "-pl", str(watts)], capture_output=True, text=True, timeout=10)
    return r.returncode == 0


# ── 控制邏輯（純計算，不碰硬體，方便測試） ──────────────────────────────────

def expected_rpm(duty: int) -> int:
    return EXPECTED_RPM.get(duty, EXPECTED_RPM[DUTY_QUIET])


class Controller:
    def __init__(self, now: float):
        self.duty = 100                      # 啟動先全速，再慢慢降到安靜
        self.last_ge = now                   # 最後一次「需求 ≥ 目前泵速」的時間
        self.last_down = now
        self.level = 0
        self.recover_since = None
        self.hot_since = None
        self.stall_n = 0
        self.weak_n = 0
        self.weak = False
        self.pump_bad_since = None
        self.alert_on = {}                   # key → True
        self.alert_clear_since = {}
        # 智能停泵
        self.off = False
        self.off_since = None
        self.off_base = None
        self.off_ok_logged = False
        self.stable_since = None
        self.next_probe = now
        self.backoff_i = 0

    def demand(self, r):
        if r is None:
            return 100, "讀不到 GPU"
        t = max(r["gpu"], r["hbm"])
        if r["power"] >= LOAD_W:
            return 100, f"功耗 {r['power']:.0f}W"
        if t >= CURVE_HI_C:
            return 100, f"{t}°C"
        if t <= CURVE_LO_C:
            return DUTY_QUIET, f"{t}°C"
        d = DUTY_QUIET + (100 - DUTY_QUIET) * (t - CURVE_LO_C) / (CURVE_HI_C - CURVE_LO_C)
        return min(100, math.ceil(d / 10) * 10), f"{t}°C"

    def _pump(self, now, want):
        if want >= self.duty:
            self.duty = want
            self.last_ge = now
        elif now - self.last_ge >= DOWN_HOLD_S and now - self.last_down >= DOWN_STEP_S:
            self.duty = max(want, self.duty - DOWN_STEP)
            self.last_down = now
        return self.duty

    def _level(self, now, r):
        if r is None:
            return self.level                # 讀不到就維持
        g, m = r["gpu"], r["hbm"]
        target = 2 if (g >= L2[0] or m >= L2[1]) else 1 if (g >= L1[0] or m >= L1[1]) else 0
        if target > self.level:
            self.level = target
            self.recover_since = None
        elif self.level > 0 and target == 0 and g <= RECOVER[0] and m <= RECOVER[1]:
            if self.recover_since is None:
                self.recover_since = now
            elif now - self.recover_since >= RECOVER_HOLD_S:
                self.level -= 1
                self.recover_since = now     # 下一級再等一輪
        else:
            self.recover_since = None
        return self.level

    def _alert(self, now, key, cond):
        if cond:
            self.alert_on[key] = True
            self.alert_clear_since.pop(key, None)
        elif self.alert_on.get(key):
            since = self.alert_clear_since.setdefault(key, now)
            if now - since >= ALERT_CLEAR_S:
                self.alert_on.pop(key)
                self.alert_clear_since.pop(key)

    def _off_reason(self, r, lvl, pump_ok):
        """停泵中：回傳該恢復運轉的理由；None＝繼續停。"""
        if r is None:
            return "讀不到 GPU"
        if not pump_ok:
            return "泵控制異常"
        t = max(r["gpu"], r["hbm"])
        if t >= self.off_base + PROBE_RISE_C:
            return f"{self.off_base}→{t}°C"
        if t >= CURVE_LO_C:
            return f"{t}°C"
        if r["power"] >= PROBE_MAX_W:
            return f"功耗 {r['power']:.0f}W"
        if lvl > 0 or self.alert_on:
            return "保護動作中"
        return None

    def step(self, now, r, rpm, pump_ok):
        """回傳 (泵速 %, 功耗等級, 事件清單)。泵速 0＝智能停泵中。"""
        ev = []
        # 泵停轉／偏弱（以上一輪的指令泵速判斷；停泵中不算）
        running = self.duty >= DUTY_QUIET
        if pump_ok and rpm is not None and running and rpm < STALL_RPM:
            self.stall_n += 1
        else:
            self.stall_n = 0
        stalled = self.stall_n >= STALL_SAMPLES
        if (pump_ok and rpm is not None and running and not stalled
                and rpm < WEAK_RATIO * expected_rpm(self.duty)):
            self.weak_n += 1
        else:
            self.weak_n = 0
        if self.weak_n >= WEAK_SAMPLES and not self.weak:
            self.weak = True
            ev.append(f"PUMP_WEAK 泵速 {self.duty}% 轉速 {rpm} rpm（預期約 {expected_rpm(self.duty)}）")
        elif self.weak and self.weak_n == 0 and running and not stalled:
            self.weak = False
            ev.append(f"PUMP_NORMAL 轉速恢復 {rpm} rpm")

        # 功耗等級
        old_lvl = self.level
        lvl = self._level(now, r)
        if stalled and lvl < 2:
            self.level = lvl = 2
        if lvl != old_lvl:
            t = f"GPU {r['gpu']}°C HBM {r['hbm']}°C" if r else "讀不到溫度"
            ev.append(f"POWER {LIMIT_W[old_lvl]}→{LIMIT_W[lvl]}W（{t}{'，泵停轉' if stalled else ''}）")

        want, why = self.demand(r)
        if stalled:
            want, why = 100, f"停轉 {rpm} rpm"

        # 智能停泵中：該恢復就恢復，否則維持 0
        resumed = False
        if self.off:
            reason = self._off_reason(r, lvl, pump_ok)
            if reason is None:
                if not self.off_ok_logged and now - self.off_since >= PROBE_JUDGE_S:
                    self.off_ok_logged = True
                    ev.append(f"PROBE_OK 停泵 {PROBE_JUDGE_S} 秒溫度沒升（{self.off_base}°C），泵維持停止")
            else:
                lasted = now - self.off_since
                self.off = False
                resumed = True
                self.duty = DUTY_QUIET
                self.stable_since = None
                if lasted < PROBE_JUDGE_S:            # 試停失敗 → 間隔加倍
                    wait = PROBE_BACKOFF_S[min(self.backoff_i, len(PROBE_BACKOFF_S) - 1)]
                    self.backoff_i += 1
                    self.next_probe = now + wait
                    ev.append(f"PROBE_FAIL 試停 {lasted:.0f} 秒就 {reason}，恢復運轉；{wait // 60} 分鐘後再試")
                else:                                 # 停了一陣子才需要恢復
                    self.backoff_i = 0
                    self.next_probe = now + PROBE_BACKOFF_S[0]
                    ev.append(f"PUMP_RESUME 停泵 {lasted / 60:.0f} 分鐘後 {reason}，恢復運轉")

        if self.off:
            duty = 0
        else:
            old = self.duty
            duty = self._pump(now, want)
            if duty > old and not (resumed and duty == DUTY_QUIET):
                ev.append(f"PUMP {old}→{duty}%（{why}）")
            elif duty < old and duty == DUTY_QUIET:
                ev.append(f"PUMP 回到安靜 {duty}%")
            # 夠冷、夠閒、夠久 → 試停
            t = max(r["gpu"], r["hbm"]) if r else None
            eligible = (PROBE_ENABLE and r is not None and pump_ok and t <= PROBE_MAX_C
                        and r["power"] < PROBE_MAX_W and duty == DUTY_QUIET and want == DUTY_QUIET
                        and lvl == 0 and not self.alert_on and not self.weak and not stalled)
            if not eligible:
                self.stable_since = None
            elif self.stable_since is None:
                self.stable_since = now
            if (eligible and now - self.stable_since >= PROBE_STABLE_S and now >= self.next_probe):
                self.off, self.off_since, self.off_base, self.off_ok_logged = True, now, t, False
                self.duty = duty = 0

        # 推播條件
        hot_now = r is not None and lvl == 2 and (r["gpu"] >= L2[0] or r["hbm"] >= L2[1])
        if not hot_now:
            self.hot_since = None
        elif self.hot_since is None:
            self.hot_since = now
        if pump_ok:
            self.pump_bad_since = None
        elif self.pump_bad_since is None:
            self.pump_bad_since = now
        before = set(self.alert_on)
        self._alert(now, "stall", stalled)
        self._alert(now, "hot", self.hot_since is not None and now - self.hot_since >= HOT_ALERT_S)
        self._alert(now, "pump_ctl", self.pump_bad_since is not None and now - self.pump_bad_since >= PUMP_CTL_ALERT_S)
        for k in set(self.alert_on) - before:
            ev.append(f"ALERT {ALERT_TEXT[k]}")
        for k in before - set(self.alert_on):
            ev.append(f"ALERT_CLEARED {ALERT_TEXT[k]}")
        return duty, lvl, ev


# ── 常駐主迴圈 ───────────────────────────────────────────────────────────

def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def update_flag(ctl: Controller):
    try:
        if ctl.alert_on:
            with open(ALERT_FLAG, "w") as f:
                f.write(datetime.now().isoformat(timespec="seconds") + " "
                        + "；".join(ALERT_TEXT[k] for k in sorted(ctl.alert_on)) + "\n")
        elif os.path.exists(ALERT_FLAG):
            os.remove(ALERT_FLAG)
    except OSError as e:
        log(f"FLAG_WRITE_FAILED {e}")


def run():
    running = True

    def stop(signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    pump = Pump()
    ctl = Controller(time.time())
    uuid = None
    read_err = None                  # 上一輪 GPU 讀取問題（只在變化時記 log）
    pump_err = None
    pl_err = False
    last_applied = None
    flag_keys = None
    stats, stats_t0 = {}, time.time()
    log(f"START 泵 SYS_FAN1(pwm{CH}) 先全速、穩定後降到 {DUTY_QUIET}%；功耗保護 {L1}/{L2} → "
        f"{LIMIT_W[1]}/{LIMIT_W[2]}W")

    while running:
        t0 = time.time()
        # 讀 GPU
        r, err = None, None
        try:
            r = read_gpu(uuid)
            if r is None:
                err = "5717 不在 nvidia-smi 清單（掉卡？）"
            else:
                uuid = r["uuid"]
        except Exception as e:      # noqa: BLE001 — 任何讀取失敗都當「讀不到」，泵拉全速
            err = f"{type(e).__name__}: {e}"
        if err != read_err:
            log(f"GPU_READ_FAILED {err}（泵全速）" if err else "GPU_READ_OK")
            read_err = err

        # 讀泵
        rpm, perr = None, None
        pump_ok = pump.ensure()
        if pump_ok:
            try:
                rpm = pump.rpm()
            except OSError as e:
                pump_ok, perr = False, f"讀轉速失敗 {e}"
        else:
            perr = f"找不到 {HWMON_NAME}"

        duty, lvl, events = ctl.step(t0, r, rpm, pump_ok)

        if pump_ok:
            try:
                changed = pump.apply(duty)
                if changed and duty == last_applied:       # 我們沒改、值卻變了＝被外部改掉
                    log(f"PUMP_REASSERT pwm{CH} 被改動，已重設為 {duty}%")
                last_applied = duty
            except OSError as e:
                perr = f"寫入失敗 {e}"
        if perr != pump_err:
            log(f"PUMP_CTL_FAILED {perr}" if perr else "PUMP_CTL_OK")
            pump_err = perr

        # 功耗上限
        if r is not None:
            want_w = LIMIT_W[lvl]
            if abs(r["limit"] - want_w) > 1:
                ok = set_power_limit(r["uuid"], want_w)
                if not ok and not pl_err:
                    log(f"POWER_SET_FAILED 想設 {want_w}W，目前 {r['limit']:.0f}W")
                pl_err = not ok

        for e in events:
            snap = f" | GPU {r['gpu']}°C HBM {r['hbm']}°C {r['power']:.0f}W" if r else ""
            log(e + snap + (f" 轉速 {rpm}" if rpm is not None else ""))
        if set(ctl.alert_on) != flag_keys:                 # 只在推播項目改變時重寫旗標
            update_flag(ctl)
            flag_keys = set(ctl.alert_on)

        # 心跳／狀態（gpu_thermal_guard 看這個決定要不要碰 5717 的功耗）
        status = {"ts": t0, "time": datetime.now().isoformat(timespec="seconds"),
                  "managed_serials": [SERIAL], "duty": duty, "rpm": rpm, "level": lvl,
                  "limit_w": LIMIT_W[lvl], "reading": r, "read_error": read_err, "pump_error": pump_err,
                  "alerts": [ALERT_TEXT[k] for k in sorted(ctl.alert_on)], "pump_weak": ctl.weak,
                  "pump_off": ctl.off, "next_probe": ctl.next_probe, "probe_fails": ctl.backoff_i}
        try:
            write_json_atomic(STATUS_FILE, status)
        except OSError:
            pass

        # 每小時摘要
        if r is not None:
            for k, v in (("gpu", r["gpu"]), ("hbm", r["hbm"]), ("w", r["power"])):
                stats[k] = max(stats.get(k, v), v)
        stats["duty_max"] = max(stats.get("duty_max", duty), duty)
        stats["lvl_max"] = max(stats.get("lvl_max", lvl), lvl)
        if rpm is not None and duty >= DUTY_QUIET:          # 停泵中的 0 不算
            stats["rpm_min"] = min(stats.get("rpm_min", rpm), rpm)
        if duty == 0:
            stats["off_s"] = stats.get("off_s", 0) + POLL_S
        if t0 - stats_t0 >= SUMMARY_S:
            log("HOURLY 最高 GPU {}°C HBM {}°C {:.0f}W｜泵最高 {}% 最低轉速 {}｜停泵 {} 分鐘｜最低功耗上限 {}W".format(
                stats.get("gpu", "-"), stats.get("hbm", "-"), stats.get("w", 0), stats["duty_max"],
                stats.get("rpm_min", "-"), stats.get("off_s", 0) // 60, LIMIT_W[stats["lvl_max"]]))
            stats, stats_t0 = {}, t0

        time.sleep(max(0.2, POLL_S - (time.time() - t0)))

    restored = False
    try:
        restored = pump.restore_bios()
    except OSError:
        pass
    note = "" if ctl.level == 0 else f"；⚠ 5717 功耗上限維持 {LIMIT_W[ctl.level]}W（gpu_thermal_guard 接手）"
    log(f"STOP 泵{'已回 BIOS 全速' if restored else '還原失敗！'}{note}")
    try:
        os.remove(STATUS_FILE)
    except OSError:
        pass


def show_status():
    try:
        with open(STATUS_FILE) as f:
            s = json.load(f)
    except OSError:
        print("守衛沒在跑（沒有狀態檔）")
        return 1
    age = time.time() - s["ts"]
    r = s.get("reading") or {}
    print(f"更新於 {s['time']}（{age:.0f} 秒前{'，⚠ 心跳過期' if age > 30 else ''}）")
    print(f"泵速 {s['duty']}%　轉速 {s['rpm']} rpm{'　⚠ 偏弱' if s.get('pump_weak') else ''}"
          f"{'　（智能停泵中）' if s.get('pump_off') else ''}")
    if not s.get("pump_off") and s.get("next_probe"):
        wait = s["next_probe"] - time.time()
        when = "條件滿足即試（需安靜檔穩定 10 分鐘）" if wait <= 0 else \
            datetime.fromtimestamp(s["next_probe"]).strftime("%H:%M") + " 之後"
        print(f"下次試停：{when}　已連續失敗 {s.get('probe_fails', 0)} 次")
    if r:
        print(f"GPU {r['gpu']}°C　HBM {r['hbm']}°C　功耗 {r['power']:.0f}W　上限 {r['limit']:.0f}W（目標 {s['limit_w']}W）")
    if s.get("read_error"):
        print(f"⚠ GPU 讀取：{s['read_error']}")
    if s.get("pump_error"):
        print(f"⚠ 泵控制：{s['pump_error']}")
    print("推播中：" + ("；".join(s["alerts"]) if s["alerts"] else "無"))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true", help="顯示目前狀態")
    ap.add_argument("--restore", action="store_true", help="泵回 BIOS 全速")
    a = ap.parse_args()
    if a.status:
        sys.exit(show_status())
    if a.restore:
        ok = Pump().restore_bios()
        print("泵已回 BIOS 全速" if ok else f"找不到 {HWMON_NAME}，無法還原（BIOS 預設本來就是全速）")
        sys.exit(0)
    run()


if __name__ == "__main__":
    main()
