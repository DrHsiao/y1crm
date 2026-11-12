#!/usr/bin/env python
"""
gpu573_image_test.py — 573 卡的影像推論測試（John 2026-11-08「今晚做 573 的影像測試」）。

背景：2026-07-12 573（當時插在 08:00.0）送圖給 qwen3-vl 連續 3 次讓 ollama runner 掛掉（RemoteDisconnected、
dmesg 沒有 Xid），之後規定 573 只跑文字。11-08 John 說「573 之前是散熱出問題」；查到兩張卡都是 PCIe Gen3 x4、
replay 0、沒有 AER，靜態資料分不出原因 → 實測。

由輕到重四段，前一段有問題就停、不往下做：
  A. 運算：fp16 矩陣乘＋主機↔顯卡來回搬 2GB，逐次比對（gpu573_torch_stage.py compute），和 0002 算的雜湊比
  B. SDXL 畫 3 張（cpu_offload＝每一步都在 PCIe 上搬權重），同 seed 在 0002 畫一張比對
  C. 臨時 ollama（只看得到 573、127.0.0.1:11437）用 minicpm-v 讀 5 張合成測試圖——和 LINE 客服的 qwen3:32b 並存
  D. 原始情境：ollama-573 卸下 qwen3:32b、載入 qwen3-vl:32b-instruct 讀 3 張測試圖，做完一定換回 qwen3:32b
     並確認回得了話。⚠ 這段 LINE 客服 AI 會暫停（逾時會送道歉文、轉人工）。
監控：每秒記 573 的溫度／功耗／時脈／PCIe；≥ STOP_TEMP 立刻停下那段，降到 RESUME_TEMP 以下再繼續；
      kernel 出現新的 Xid／NVRM 錯誤就全部中止。測試圖都是程式畫的，沒有客人資料。
結果：/mnt/raid1/monitoring/gpu573_test_<時間>.log／.json

    python3 scripts/gpu/gpu573_image_test.py [--stages ABCD] [--yes]
"""
import argparse
import base64
import io
import json
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SERIAL_573, SERIAL_0002 = "1422019000573", "1422019000002"
TORCH_PY = "/home/john/media/ghibli/venv/bin/python"          # 有 torch（sm_70）＋diffusers 的環境，只借來跑
HF_HOME = "/mnt/raid1/llm_models/huggingface"
STAGE_SCRIPT = Path(__file__).resolve().parent / "gpu573_torch_stage.py"
OLLAMA_573, OLLAMA_TEST = "http://127.0.0.1:11435", "http://127.0.0.1:11437"
TEXT_MODEL, VL_MODEL, SMALL_VL = "qwen3:32b", "qwen3-vl:32b-instruct", "minicpm-v:latest"
STOP_TEMP, RESUME_TEMP = 80, 65
OUT = Path("/mnt/raid1/monitoring")
STAMP = datetime.now().strftime("%Y%m%d_%H%M")
LOG = OUT / f"gpu573_test_{STAMP}.log"
WORK = OUT / f"gpu573_test_{STAMP}"

state = {"temp": 0, "max_temp": 0, "hot": False, "xid": [], "stop": False}
result = {"started": STAMP, "stages": {}}


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def gpus():
    r = subprocess.run(["nvidia-smi", "--query-gpu=index,serial,uuid", "--format=csv,noheader"],
                       capture_output=True, text=True, timeout=20)
    return {s.strip(): (i.strip(), u.strip()) for i, s, u in (l.split(",") for l in r.stdout.strip().splitlines())}


def monitor(index: str) -> None:
    """每秒記 573 的狀態；過熱設 hot（各段自己停）。"""
    p = subprocess.Popen(["nvidia-smi", "-i", index, "--query-gpu=temperature.gpu,power.draw,clocks.sm,utilization.gpu,"
                          "memory.used,pcie.link.gen.current,pcie.link.width.current,clocks_throttle_reasons.active",
                          "--format=csv,noheader,nounits", "-l", "1"], stdout=subprocess.PIPE, text=True)
    with open(WORK / "dmon_573.csv", "a") as f:
        f.write("time,temp,power,sm_mhz,util,mem_mib,pcie_gen,pcie_width,throttle\n")
        for line in p.stdout:
            f.write(f"{datetime.now():%H:%M:%S},{line.strip()}\n")
            f.flush()
            try:
                t = int(line.split(",")[0])
            except ValueError:
                continue
            state["temp"], state["max_temp"] = t, max(state["max_temp"], t)
            if t >= STOP_TEMP and not state["hot"]:
                state["hot"] = True
                log(f"⚠ 溫度 {t}°C ≥ {STOP_TEMP}：停下這段")
            if state["stop"]:
                p.terminate()
                return


def xid_since(since: str):
    r = subprocess.run(["journalctl", "-k", "--since", since, "--no-pager"], capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if "Xid" in l or "NVRM" in l or "fallen off" in l]


def cool_down() -> None:
    while state["temp"] > RESUME_TEMP:
        time.sleep(5)
    state["hot"] = False


def run_proc(args, env, timeout: float, stage: str):
    """跑子行程、逐行記 JSON；過熱或逾時就殺掉。回 (結束碼, 事件列表, 是否過熱中止)。"""
    events, hot = [], False
    errf = open(WORK / f"{stage}.stderr", "w")          # 錯誤輸出寫檔：放管道裡太多會把子行程卡死
    p = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=errf, text=True)
    t0 = time.time()

    def reader():
        for line in p.stdout:
            line = line.strip()
            if line.startswith("{"):
                events.append(json.loads(line))
                log(f"[{stage}] {line[:220]}")
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    while p.poll() is None:
        if state["hot"]:
            hot = True
            p.kill()
            break
        if time.time() - t0 > timeout:
            log(f"[{stage}] 逾時 {timeout}s，中止")
            p.kill()
            break
        time.sleep(1)
    p.wait()
    th.join(timeout=5)
    errf.close()
    err = (WORK / f"{stage}.stderr").read_text(errors="replace")[-1500:]
    if p.returncode not in (0, None) and not hot:
        log(f"[{stage}] 結束碼 {p.returncode}：{err.strip()[-600:]}")
    return p.returncode, events, hot


def torch_env(uuid: str):
    import os
    return {**os.environ, "CUDA_VISIBLE_DEVICES": uuid, "HF_HOME": HF_HOME, "HF_HUB_OFFLINE": "1",
            "PYTHONUNBUFFERED": "1"}


def stage_a(u573: str, u0002: str) -> bool:
    log("== A：運算＋大量傳輸（573，90 秒）")
    rc, ev, hot = run_proc([TORCH_PY, "-I", str(STAGE_SCRIPT), "compute", "90"], torch_env(u573), 600, "A")
    done = next((e for e in ev if e.get("event") == "done"), None)
    rc2, ev2, _ = run_proc([TORCH_PY, "-I", str(STAGE_SCRIPT), "compute", "5"], torch_env(u0002), 300, "A-0002")
    ref2 = next((e["hash"] for e in ev2 if e.get("event") == "ref"), None)
    ok = bool(done) and rc == 0 and done["matmul_bad"] == 0 and done["transfer_bad"] == 0
    same = bool(done) and done["hash"] == ref2
    result["stages"]["A"] = {"ok": ok, "hot_stop": hot, "rc": rc, "done": done, "same_as_0002": same}
    log(f"A 結果：{'通過' if ok else '失敗'}；和 0002 的運算結果{'一致' if same else '不一致'}")
    return ok


def stage_b(u573: str, u0002: str) -> bool:
    log("== B：SDXL 畫 3 張（573）")
    rc, ev, hot = run_proc([TORCH_PY, "-I", str(STAGE_SCRIPT), "sdxl", str(WORK / "sdxl_573"), "11", "12", "13"],
                           torch_env(u573), 1800, "B")
    imgs = [e for e in ev if e.get("event") == "image"]
    rc2, ev2, _ = run_proc([TORCH_PY, "-I", str(STAGE_SCRIPT), "sdxl", str(WORK / "sdxl_0002"), "11"],
                           torch_env(u0002), 1200, "B-0002")
    h2 = next((e["hash"] for e in ev2 if e.get("event") == "image"), None)
    ok = rc == 0 and len(imgs) == 3
    # 0002 沒畫出來（例如 573 過熱停下時一起被停掉）就是「沒比對」，不能說成「不一樣」（11-08 第一次測試誤報過）
    same = (imgs[0]["hash"] == h2) if imgs and h2 else None
    result["stages"]["B"] = {"ok": ok, "hot_stop": hot, "rc": rc, "images": imgs, "same_as_0002": same}
    log(f"B 結果：{'通過' if ok else '失敗'}（{len(imgs)}/3 張）；seed 11 和 0002 畫的"
        + {True: "一模一樣", False: "不一樣", None: "沒比對到（0002 沒畫出來）"}[same])
    if hot:
        cool_down()
    return ok


def test_image(i: int) -> str:
    """合成的測試圖（不是客人資料）：一張像表單的圖，上面幾行字，回 base64 PNG。"""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (1200, 800), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 44, index=3)
    rows = ["測試表單（測試請忽略）", f"編號：TEST-{i:03d}", "項目：聽力測試練習", "日期：2026 年 11 月 8 日"]
    for k, row in enumerate(rows):
        d.text((80, 100 + k * 140), row, font=f, fill="black")
    d.rectangle((60, 60, 1140, 740), outline="black", width=4)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def ollama_chat(base: str, model: str, prompt: str, image=None, keep_alive="5m", timeout=900, num_predict=300):
    msg = {"role": "user", "content": prompt}
    if image:
        msg["images"] = [image]
    body = {"model": model, "messages": [msg], "stream": False, "keep_alive": keep_alive,
            "options": {"num_predict": num_predict, "num_ctx": 8192}}
    if model.startswith("qwen3"):
        body["think"] = False                      # 只有 qwen3 系列有思考模式
    req = urllib.request.Request(base + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ((json.loads(r.read()).get("message") or {}).get("content") or "").strip()


def ollama_unload(base: str, model: str) -> None:
    """卸載模型（空的 prompt＋keep_alive 0 是 ollama 正式的卸載方式；沒載入的不會被載入）。"""
    req = urllib.request.Request(base + "/api/generate", data=json.dumps({"model": model, "keep_alive": 0}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        r.read()


def vision_rounds(base: str, model: str, n: int, stage: str):
    rounds = []
    for i in range(1, n + 1):
        if state["hot"]:
            log(f"[{stage}] 過熱，等降溫")
            cool_down()
        t = time.time()
        try:
            text = ollama_chat(base, model, "請把圖片裡的文字逐行抄出來。", test_image(i))
            ok = "TEST" in text.upper() or "測試" in text
            rounds.append({"ok": ok, "sec": round(time.time() - t, 1), "text": text[:120]})
            log(f"[{stage}] 第 {i} 張：{'讀對' if ok else '讀錯'}（{time.time() - t:.0f}s）{text[:80]!r}")
        except Exception as e:  # noqa: BLE001 — 這正是要抓的（runner 掛掉）
            rounds.append({"ok": False, "sec": round(time.time() - t, 1), "error": f"{type(e).__name__}: {e}"})
            log(f"[{stage}] 第 {i} 張：失敗 {type(e).__name__}: {e}")
    return rounds


def stage_c(u573: str) -> bool:
    log("== C：臨時 ollama（只看得到 573）＋minicpm-v 讀 5 張測試圖")
    subprocess.run(["docker", "rm", "-f", "ollama-573-test"], capture_output=True)
    r = subprocess.run(["docker", "run", "-d", "--rm", "--name", "ollama-573-test", "--gpus", f"device={u573}",
                        "-e", "CUDA_VISIBLE_DEVICES=0", "-e", "OLLAMA_KEEP_ALIVE=0", "-e", "OLLAMA_MAX_LOADED_MODELS=1",
                        "-v", "/mnt/raid1/llm_models/ollama:/root/.ollama", "-p", "127.0.0.1:11437:11434",
                        "ollama/ollama"], capture_output=True, text=True)
    if r.returncode:
        log(f"C：臨時容器起不來：{r.stderr.strip()}")
        result["stages"]["C"] = {"ok": False, "error": r.stderr.strip()}
        return False
    try:
        for _ in range(30):
            try:
                urllib.request.urlopen(OLLAMA_TEST + "/api/version", timeout=3)
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
        rounds = vision_rounds(OLLAMA_TEST, SMALL_VL, 5, "C")
    finally:
        logs = subprocess.run(["docker", "logs", "--tail", "60", "ollama-573-test"], capture_output=True, text=True)
        (WORK / "ollama_test_container.log").write_text(logs.stdout + logs.stderr)
        subprocess.run(["docker", "rm", "-f", "ollama-573-test"], capture_output=True)
    ok = all(x["ok"] for x in rounds)
    result["stages"]["C"] = {"ok": ok, "rounds": rounds}
    log(f"C 結果：{'通過' if ok else '失敗'}（{sum(x['ok'] for x in rounds)}/5）")
    return ok


def restore_text_model() -> bool:
    """把 LINE 客服的 qwen3:32b 換回 ollama-573（常駐），確認回得了話。"""
    for attempt in range(3):
        try:
            ollama_unload(OLLAMA_573, VL_MODEL)
        except Exception:  # noqa: BLE001 — 卸載失敗也繼續載回文字模型
            pass
        try:
            t = time.time()
            text = ollama_chat(OLLAMA_573, TEXT_MODEL, "請用一句話打招呼。", keep_alive=-1, timeout=900, num_predict=40)
            log(f"客服模型已換回 {TEXT_MODEL}（{time.time() - t:.0f}s）：{text[:40]!r}")
            return bool(text)
        except Exception as e:  # noqa: BLE001
            log(f"換回客服模型失敗（第 {attempt + 1} 次）：{e}")
            time.sleep(20)
    return False


def stage_d() -> bool:
    log(f"== D：原始情境（ollama-573 換成 {VL_MODEL} 讀 3 張測試圖）⚠ LINE 客服暫停")
    t0 = time.time()
    try:
        ollama_unload(OLLAMA_573, TEXT_MODEL)                                                  # 卸下客服模型
    except Exception as e:  # noqa: BLE001
        log(f"D：卸下 {TEXT_MODEL} 時出錯（繼續）：{e}")
    try:
        rounds = vision_rounds(OLLAMA_573, VL_MODEL, 3, "D")
    finally:
        restored = restore_text_model()
        logs = subprocess.run(["docker", "logs", "--since", f"{int(time.time() - t0) + 60}s", "ollama-573"],
                              capture_output=True, text=True)
        (WORK / "ollama_573_during_D.log").write_text(logs.stdout + logs.stderr)
    ok = all(x["ok"] for x in rounds)
    result["stages"]["D"] = {"ok": ok, "rounds": rounds, "restored": restored,
                             "line_ai_paused_sec": round(time.time() - t0)}
    log(f"D 結果：{'通過' if ok else '失敗'}；客服模型{'已恢復' if restored else '沒有恢復！'}"
        f"（客服暫停 {time.time() - t0:.0f} 秒）")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="ABCD")
    ap.add_argument("--yes", action="store_true", help="D 段會暫停 LINE 客服，要明確給 --yes 才做")
    args = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    g = gpus()
    if SERIAL_573 not in g or SERIAL_0002 not in g:
        log(f"找不到兩張卡：{g}")
        return 1
    (i573, u573), (_, u0002) = g[SERIAL_573], g[SERIAL_0002]
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"開始：573＝index {i573}（{u573}），0002＝{u0002}；階段 {args.stages}")
    threading.Thread(target=monitor, args=(i573,), daemon=True).start()
    time.sleep(3)
    log(f"起始溫度 {state['temp']}°C")
    steps = {"A": lambda: stage_a(u573, u0002), "B": lambda: stage_b(u573, u0002), "C": lambda: stage_c(u573),
             "D": stage_d}
    try:
        for s in args.stages:
            if s == "D" and not args.yes:
                log("D 段會暫停 LINE 客服：沒給 --yes，不做")
                continue
            if state["hot"]:
                cool_down()
            ok = steps[s]()
            x = xid_since(since)
            if x:
                state["xid"] = x
                log(f"⚠ kernel 出現 GPU 錯誤，全部中止：{x[-3:]}")
                break
            if not ok:
                log(f"{s} 段沒通過，後面不做（有問題就停）")
                break
    finally:
        state["stop"] = True
        result.update(max_temp=state["max_temp"], xid=state["xid"], finished=datetime.now().strftime("%H:%M:%S"))
        (OUT / f"gpu573_test_{STAMP}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
        log(f"結束：最高溫 {state['max_temp']}°C；Xid {len(state['xid'])} 筆；結果 {OUT}/gpu573_test_{STAMP}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
