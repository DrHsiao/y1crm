#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主機健康檢查＋每日摘要（2026-09-26）

    healthcheck.py            # 每 10 分鐘（ops-healthcheck.timer）：有變化才通知
    healthcheck.py --summary  # 每天 08:00（ops-summary.timer）：一則總覽＝心跳
    healthcheck.py --dry-run  # 只印結果，不發送、不寫狀態

## 推播原則（John 2026-09-26 決定）
只推「需要人處理」的事，且只在【狀態改變】時推：新問題出現推一次、恢復推一次。
持續中的問題不重複推，改列在每日摘要。每日摘要沒收到＝監控本身壞了。

## 誤報控制
- 服務／HTTP 類要【連續 2 輪】都異常才告警（開機、重啟中的短暫失敗不算）。
- 硬碟、RAID、GPU 掉卡這類第一輪就告警。
- KNOWN_ISSUES：已知且已決定處理方式的問題（例如待拆的壞碟），不告警、只列摘要。
  問題解決後記得從這裡移除。

stdlib only，用系統 /usr/bin/python3 以 root 執行。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify  # noqa: E402

STATE = "/var/lib/ops/health_state.json"

# 已知問題：key → 說明（不告警，只列在摘要）
KNOWN_ISSUES = {
    "smart:57JEK0PAFPBE": "sdb 壞碟（SMART FAILED），已退出 RAID，待拆除送修",
}

SERVICES = ["y1crm", "mysql", "docker", "nginx", "openclaw-twii", "openclaw-twiii"]
CONTAINERS = ["ollama", "ollama-573"]
DISKS = {"/": 90, "/mnt/raid1": 90, "/boot": 85}     # 掛載點 → 告警門檻 %
GPU_TEMP_WARN = 85                                   # V100：87 硬體降頻、90 關機
FLAGS = {
    "/tmp/gpu_thermal_failing": "GPU 溫控讀不到溫度（溫控失效）",
    "/tmp/gpu_thermal_card_missing": "GPU 缺卡（溫控清點少了卡）",
    "/tmp/ollama_watchdog_failing": "ollama 看門狗：常駐模型不在卡上",
    "/tmp/backup_failing": "每日備份失敗",
}
BACKUP_LOG = "/mnt/backup/backup.log"
BACKUP_MAX_AGE_H = 26


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        return 124, ""


# ── 各項檢查：回傳 [(key, 說明, 需連續幾輪)] 與摘要用的資訊 ──────────────

def check_raid(p, info):
    try:
        md = open("/proc/mdstat").read()
    except OSError:
        return
    arrays = re.findall(r"^(md\d+) : (\w+) (\w+) .*?\n\s+\d+ blocks.*?\[(\d+)/(\d+)\] \[([U_]+)\]", md, re.M)
    if not arrays:
        p.append(("raid:none", "找不到 RAID 陣列 md0（/mnt/raid1 可能沒掛上）", 1))
    for name, state, level, want, have, bar in arrays:
        info.append(f"{name} {level} [{have}/{want}] [{bar}]")
        if "_" in bar or have != want or state != "active":
            p.append((f"raid:{name}", f"{name} 降級：[{have}/{want}] [{bar}]（{state}）", 1))
    if "resync" in md or "recovery" in md:
        m = re.search(r"(resync|recovery)\s*=\s*([\d.]+%)", md)
        info.append(f"RAID 同步中 {m.group(0) if m else ''}")


def check_smart(p, info):
    for dev in sorted(glob.glob("/dev/sd?")):
        rc, out = sh(["smartctl", "-H", "-A", "-i", dev])
        sn = re.search(r"Serial Number:\s*(\S+)", out)
        sn = sn.group(1) if sn else dev
        key = f"smart:{sn}"
        health = re.search(r"overall-health self-assessment test result:\s*(\S+)", out)
        health = health.group(1) if health else "UNKNOWN"
        attrs = {}
        for m in re.finditer(r"^\s*(5|187|197|198)\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)", out, re.M):
            attrs[m.group(1)] = int(m.group(2))
        bad = {k: v for k, v in attrs.items() if v}
        label = f"{dev.split('/')[-1]}（{sn}）"
        info.append(f"{label} SMART {health}" + (f" 壞軌計數 {bad}" if bad else ""))
        if health != "PASSED" or bad:
            p.append((key, f"硬碟 {label} SMART {health}" +
                      (f"，重新配置/待處理磁區 {bad}（5=已重配置 197=待處理 198=無法修正）" if bad else ""), 1))


def check_disks(p, info):
    for mnt, limit in DISKS.items():
        if not os.path.ismount(mnt):
            p.append((f"mount:{mnt}", f"{mnt} 沒有掛載", 1))
            continue
        u = shutil.disk_usage(mnt)
        pct = u.used * 100 // u.total
        info.append(f"{mnt} {pct}%（剩 {u.free // 2**30}G）")
        if pct >= limit:
            p.append((f"disk:{mnt}", f"{mnt} 已用 {pct}%（剩 {u.free // 2**30}G）", 1))


def check_gpu(p, info, st):
    rc, out = sh(["nvidia-smi", "--query-gpu=serial,temperature.gpu,memory.used",
                  "--format=csv,noheader,nounits"])
    if rc != 0:
        p.append(("gpu:nvml", "nvidia-smi 執行失敗（驅動／NVML 異常）", 2))
    else:
        temps = []
        for line in out.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 3:
                continue
            sn, t, mem = parts
            try:
                t = int(t)
            except ValueError:
                p.append((f"gpu:read:{sn}", f"GPU {sn[-4:]} 讀不到溫度（{t}）", 2))
                continue
            temps.append(f"{sn[-4:]}={t}°C")
            if t >= GPU_TEMP_WARN:
                p.append((f"gpu:hot:{sn}", f"GPU {sn[-4:]} 溫度 {t}°C（≥{GPU_TEMP_WARN}）", 1))
        info.append("GPU " + " ".join(temps))
    # 自上次檢查以來的 Xid（GPU 硬體／驅動錯誤）
    since = st.get("last_run_ts")
    arg = f"@{int(since)}" if since else "-15min"
    rc, out = sh(["journalctl", "-k", "--since", arg, "--no-pager", "-o", "cat"], timeout=60)
    xids = [l for l in out.splitlines() if "NVRM: Xid" in l or "fallen off the bus" in l]
    if xids:
        p.append(("gpu:xid:" + str(int(time.time())), "GPU 核心錯誤：\n" + "\n".join(xids[-3:]), 1))


def check_flags(p):
    for path, desc in FLAGS.items():
        if os.path.exists(path):
            try:
                detail = open(path).read().strip()[:200]
            except OSError:
                detail = ""
            p.append((f"flag:{os.path.basename(path)}", f"{desc}\n{detail}", 1))


def check_services(p, info):
    down = []
    for s in SERVICES:
        rc, out = sh(["systemctl", "is-active", s])
        if out.strip() != "active":
            down.append(s)
            p.append((f"svc:{s}", f"服務 {s} 狀態 {out.strip() or '未知'}", 2))
    rc, out = sh(["systemctl", "--failed", "--no-legend", "--plain"])
    for line in out.strip().splitlines():
        unit = line.split()[0] if line.split() else ""
        if unit:
            p.append((f"failed:{unit}", f"systemd 單元失敗：{unit}", 2))
    rc, out = sh(["docker", "ps", "--format", "{{.Names}}"])
    running = set(out.split())
    for c in CONTAINERS:
        if c not in running:
            p.append((f"ctr:{c}", f"容器 {c} 沒有在跑", 2))
    info.append(f"服務 {len(SERVICES) - len(down)}/{len(SERVICES)} 正常、容器 {len([c for c in CONTAINERS if c in running])}/{len(CONTAINERS)}")


def check_http(p):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8004/api/health", timeout=10) as r:
            body = json.load(r)
        if not body.get("ok"):
            p.append(("http:y1crm", f"y1crm /api/health 回報異常：{body}", 2))
    except Exception as e:
        p.append(("http:y1crm", f"y1crm /api/health 連不上：{e}", 2))


def check_memory(p, info, st):
    mem = dict(re.findall(r"^(\w+):\s+(\d+)", open("/proc/meminfo").read(), re.M))
    total, avail = int(mem["MemTotal"]), int(mem["MemAvailable"])
    pct = avail * 100 // total
    info.append(f"記憶體可用 {avail // 2**20}G（{pct}%）")
    if pct < 5:
        p.append(("mem:low", f"記憶體只剩 {avail // 2**20}G 可用（{pct}%）", 2))
    since = st.get("last_run_ts")
    arg = f"@{int(since)}" if since else "-15min"
    rc, out = sh(["journalctl", "-k", "--since", arg, "--no-pager", "-o", "cat"], timeout=60)
    oom = [l for l in out.splitlines() if "Out of memory: Killed process" in l]
    if oom:
        p.append(("mem:oom:" + str(int(time.time())), "記憶體不足，核心砍了程序：\n" + "\n".join(oom[-3:]), 1))


def check_backup(p, info):
    rc, out = sh(["systemctl", "list-unit-files", "raid1-backup.timer", "--no-legend"])
    if "raid1-backup.timer" not in out:
        p.append(("backup:missing", "每日備份尚未安裝（y1crm/scripts/backup/README.md 的一次性安裝步驟）", 1))
        info.append("備份：未安裝")
        return
    try:
        age_h = (time.time() - os.path.getmtime(BACKUP_LOG)) / 3600
        info.append(f"備份：{age_h:.0f} 小時前")
        if age_h > BACKUP_MAX_AGE_H:
            p.append(("backup:stale", f"每日備份已 {age_h:.0f} 小時沒有更新", 1))
    except OSError:
        p.append(("backup:stale", f"找不到備份 log {BACKUP_LOG}（/mnt/backup 沒掛載？）", 1))


# ── 主流程 ────────────────────────────────────────────────────────────

def collect(st):
    p, info = [], []
    for fn in (lambda: check_raid(p, info), lambda: check_smart(p, info),
               lambda: check_disks(p, info), lambda: check_gpu(p, info, st),
               lambda: check_flags(p), lambda: check_services(p, info),
               lambda: check_http(p), lambda: check_memory(p, info, st),
               lambda: check_backup(p, info)):
        try:
            fn()
        except Exception as e:                       # 單項檢查壞掉不能拖垮整輪
            p.append((f"check:{getattr(fn, '__name__', 'x')}:{type(e).__name__}",
                      f"健康檢查本身出錯：{type(e).__name__}: {e}", 2))
    return p, info


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": {}, "streak": {}}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def run(dry=False):
    st = load_state()
    problems, info = collect(st)
    now = time.time()
    cur = {k: (msg, need) for k, msg, need in problems if k not in KNOWN_ISSUES}
    streak = {k: st.get("streak", {}).get(k, 0) + 1 for k in cur}
    active = dict(st.get("active", {}))                # 已通知過、尚未恢復的問題

    new = [(k, cur[k][0]) for k in cur if k not in active and streak[k] >= cur[k][1]]
    # 一次性事件（Xid、OOM）的 key 帶時間戳，不列入「恢復」
    recovered = [(k, v["msg"]) for k, v in active.items()
                 if k not in cur and not re.search(r":\d{10}$", k)]
    for k in [k for k in active if re.search(r":\d{10}$", k)]:
        active.pop(k)

    if dry:
        print("資訊：", *info, sep="\n  ")
        print("目前問題：", *(f"{k} (連續 {streak[k]} 輪) {m}" for k, (m, _) in cur.items()) or ["（無）"], sep="\n  ")
        print("已知問題：", *(f"{k} {v}" for k, v in KNOWN_ISSUES.items()), sep="\n  ")
        print("將通知：", *(f"🔴 {m}" for _, m in new), *(f"🟢 {m}" for _, m in recovered), sep="\n  ")
        return

    msgs = []
    if new:
        msgs.append("🔴 需要處理\n" + "\n\n".join(f"• {m}" for _, m in new))
    if recovered:
        msgs.append("🟢 已恢復\n" + "\n".join(f"• {m.splitlines()[0]}" for _, m in recovered))
    if msgs:
        notify.send("\n\n".join(msgs))
    else:
        notify.flush()                                  # 沒新訊息也順便補送佇列

    for k, m in new:
        active[k] = {"msg": m, "since": now}
    for k, _ in recovered:
        active.pop(k, None)
    st.update(active=active, streak=streak, last_run_ts=now, last_info=info)
    save_state(st)


def summary():
    st = load_state()
    problems, info = collect(st)
    cur = [(k, m) for k, m, _ in problems if k not in KNOWN_ISSUES]
    known = [KNOWN_ISSUES[k] for k, _, _ in problems if k in KNOWN_ISSUES]
    head = "☀️ 每日摘要：" + ("一切正常" if not cur else f"{len(cur)} 個問題待處理")
    parts = [head]
    if cur:
        parts.append("待處理：\n" + "\n".join(f"• {m.splitlines()[0]}" for _, m in cur))
    if known:
        parts.append("已知（不重複告警）：\n" + "\n".join(f"• {m}" for m in known))
    parts.append("狀態：\n" + "\n".join(f"• {x}" for x in info))
    rc, up = sh(["uptime", "-p"])
    parts.append(f"開機時間：{up.strip()}")
    left = notify.pending()
    if left:
        parts.append(f"⚠ 通知佇列還有 {left} 則沒送出（Telegram 曾中斷）")
    notify.send("\n\n".join(parts))


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--summary" in a:
        summary()
    else:
        run(dry="--dry-run" in a)
