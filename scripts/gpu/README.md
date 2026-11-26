# GPU 與 ollama 維運腳本

本機（192.168.1.88）有兩張 Tesla V100 32GB，各跑一個 ollama 實例：

| 實例 | 卡（序號） | 常駐模型 | 誰在用 |
|---|---|---|---|
| `:11434` | 好卡 **0002** | `qwen3-vl:32b-instruct`（23GB） | y1crm 表單 OCR、LINE 客戶圖片 |
| `:11435` | **573**（⚠ 只能純文字） | `qwen3:32b`（21GB） | y1crm LINE 客服 AI、TWIII telegram bot |

兩個容器都設 `MAX_LOADED_MODELS=1`、`KEEP_ALIVE=-1`、`NUM_PARALLEL=1`，
各自用 UUID 釘死一張卡（定義在 `/home/john/workspace/docker-compose.yml`）。

## 多個專案共用這兩張卡的三條規矩

可以共用，但這是「共用印表機」不是「無限資源」，違反哪一條就會出事：

1. **每個 port 只用它已經常駐的那顆模型。**
   不是慣例，是硬體限制：32GB 的卡放了 23GB 的模型只剩 9GB，第二顆 32B 塞不下。
   要求別的模型 → ollama 先卸掉常駐那顆 → 冷載入（HDD RAID1 約 455s）若超過你的
   timeout 就被取消 → **兩顆都不在卡上**。需要別的模型時，正解是改用同一顆或走雲端 API。
2. **絕對不要送圖片到 `:11435`。**
   573 那張卡做影像推論會**靜默打崩** runner（dmesg 連 Xid 都沒有）。
   沒有任何軟體會擋你，只能靠每個專案自己遵守。影像一律走 `:11434`。
3. **請求會排隊（`NUM_PARALLEL=1`）。**
   表單 OCR 約 13s、LINE 圖片約 5–10s，偶爾撞在一起還好；但**批次工作會餓死即時服務**
   （LINE 的 reply token 只有約 1 分鐘、客服 AI 預算 30 秒）。要跑大批量請挑離峰。

第 1 條現在有看門狗兜底：最壞情況是 10 分鐘內自動載回並告警，
而不是像 2026-08-07 那樣靜默死掉 7 小時（TWIII 的 bot 要了 `deepseek-r1:32b`，
把 LINE 客服的模型擠掉後自己也載入失敗，客戶每則訊息都收到道歉文）。

## 檔案

| 檔案 | 用途 | 觸發方式 |
|---|---|---|
| `ollama_warmup.sh` | 開機後**並行**預載兩個模型（各約 7.5 分鐘） | `ollama-warmup.service`（oneshot） |
| `ollama_watchdog.py` | 每 10 分鐘確認常駐模型還在，掉了自動載回 | `ollama-watchdog.timer` |
| `gpu_thermal_guard.py` | 80°C 降功耗上限、70°C 恢復 | root crontab 每分鐘 |
| `watercool_guard.py` | 5717 水冷：泵速曲線、停轉偵測、HBM／GPU 分級降功耗 | `watercool-guard.service`（常駐，每 2 秒） |
| `*.service` / `*.timer` | 上面三者的 systemd 單元 | — |

- 為什麼要預熱：`KEEP_ALIVE=-1` 只保證「已載入的不卸載」，重開機後 VRAM 是空的。
  沒有它，店員第一次 OCR 要乾等 7 分鐘，LINE 客服則是第一位客戶必收道歉文並被轉人工 4 小時。
- 看門狗的告警＝旗標檔 `/tmp/ollama_watchdog_failing` ＋ unit 變 failed
  （`systemctl --failed`）＋ journal 訊息；log 在 `/mnt/raid1/monitoring/ollama_watchdog.log`。
  卡上是**別的**模型時它**不驅逐**（可能有人正在用），只告警。
- 溫控失效的徵兆：`/tmp/gpu_thermal_failing` 旗標與 log 裡的 `QUERY_FAILED`（全部卡都讀不到）。
- 單卡失效：log 的 `CARD_ERROR`/`CARD_MISSING` 與 `/tmp/gpu_thermal_card_missing` 旗標；應在線的卡以序號列在腳本 `EXPECTED_CARDS`，換卡/拆卡要同步改。一張卡掉線時整批查詢會失敗，腳本會退回逐卡（依 bus）查詢，其餘好卡照樣受保護。

## 5717 水冷守衛（2026-11-11）

- 泵接主機板 **SYS_FAN1**＝nct6779 `pwm1`／`fan1`。**一定要 PWM 模式**（`pwm1_mode=1`），BIOS 預設的 DC 模式對這顆泵無效。
- 冷排風扇 **2026-11-14 裝上**。裝前滿載 ~290W 時 HBM 約 3.5 分鐘到 85°C 上限；裝後（11-15～11-24）滿載 ~330W，HBM 最高 68–74°C，離第一級降功耗（82°C）還有 8°C 以上。下面的泵速曲線與降功耗門檻仍是沒風扇時訂的，還沒重新評估。
- 冷排風扇的轉速**主機板讀不到**（11-26 查：fan3～5 為 0、fan2 是亂數）。風扇壞了守衛不會知道，只能靠溫度門檻兜底。
- 泵速：閒置 30%（≈1440 rpm）；`max(GPU,HBM)` 50→65°C 線性加速；功耗 ≥120W 直接全速。加速立即，降速先等 2 分鐘再每 30 秒降 10%。
- 智能試停：**2026-11-26 已關閉**（`PROBE_ENABLE = False`），泵閒置固定 30%。11-11～11-26 試 70 次、成功 0 次（卡被程式佔著、P0 ~36W，停泵 4～8 秒就升 3～7°C）；而且有 3 次（11-13、11-23、11-24）停泵後恢復 30% 泵沒轉起來，6 秒後被停轉偵測拉全速才救回，發出了停轉推播。
  原本的規則：30% 檔＋`max(GPU,HBM)` ≤45°C＋功耗 <60W 穩定 10 分鐘才試停；停泵後升 3°C 或功耗 ≥60W 就恢復 30%；停滿 60 秒沒升溫＝成功；失敗則 30 分→1→2→4 小時（封頂）再試。**重新打開前**要先把恢復改成「全速衝 1～2 輪再降回 30%」。
- 功耗：GPU 78／HBM 82°C → 200W；GPU 82／HBM 84°C → 150W；兩者 ≤70／75°C 穩定 60 秒才逐級恢復。
- 推播（旗標 `/tmp/watercool_alert` → ops healthcheck，狀態改變才推）：泵停轉、150W 仍過熱 60 秒、無法控制泵 60 秒。
  - **泵停轉另由守衛直接推**（2026-11-26 起，`DIRECT_PUSH`）：停轉約 6 秒內發 🔴、恢復穩定 2 分鐘發 🟢；走 `ops/notify.py`、背景執行緒送，開機網路還沒好就每 30 秒補送（最多 10 分鐘，之後留佇列給 healthcheck）。起因：11-26 15:02 開機線沒接好、泵 0 rpm 5 分鐘、閒置升到 82°C，healthcheck 每 10 分鐘才跑、那次開機沒撐到它跑，一則都沒發。停轉超過 10 分鐘 healthcheck 會再推一次（＝還沒修好的提醒）。其餘只記 `/mnt/raid1/monitoring/watercool_guard.log`（事件＋每小時一行）。
- 看狀態：`sudo python3 scripts/gpu/watercool_guard.py --status`
- 與 `gpu_thermal_guard.py` 分工：水冷守衛心跳（`/run/watercool_guard.json`）新鮮時，溫控守護不碰 5717 的功耗；心跳停 30 秒就接手。
- 停止或當掉時泵自動回 BIOS 全速（`ExecStopPost=--restore`）。若停止時正在降功耗，上限會留在 200／150W，log 的 `STOP` 那行會寫。

## 安裝／還原（重灌或換機後）

```bash
sudo cp scripts/gpu/*.service scripts/gpu/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ollama-warmup.service ollama-watchdog.timer
# 溫控（root crontab 每分鐘）
sudo crontab -l | grep -q gpu_thermal_guard || \
  ( sudo crontab -l; echo '* * * * * /usr/bin/python3 /home/john/y1crm/scripts/gpu/gpu_thermal_guard.py >> /mnt/raid1/monitoring/gpu_thermal_cron.log 2>&1' ) | sudo crontab -
# 水冷守衛（風扇晶片驅動開機自動載入）
echo nct6775 | sudo tee /etc/modules-load.d/nct6775.conf
sudo systemctl enable --now watercool-guard.service
```

驗證：

```bash
sudo systemctl restart ollama-warmup.service   # ⚠ RemainAfterExit=yes，用 start 是 no-op
sudo systemctl start ollama-watchdog.service
tail -3 /mnt/raid1/monitoring/{ollama_warmup,ollama_watchdog,gpu_thermal}.log
```

**`/etc/systemd/system/` 與 root crontab 裡的是執行中的副本，這個資料夾才是正本**；
改了要兩邊同步。

## 待辦

- **模型清單有兩份**（`ollama_warmup.sh` 與 `ollama_watchdog.py` 各寫一次），換模型兩邊都要改。
  合併的正解是讓 warmup 直接呼叫 watchdog，但 watchdog 目前是**序列**載入、warmup 是並行，
  冷開機會從 8 分鐘變 15 分鐘，要先幫 watchdog 加上並行才能合。
- `/usr/local/bin/gpu573_{sleep,wake}.sh`（573 斷電/喚醒，`gpu573-sleep.service` 目前 disabled）
  還沒進版控。
- 機器層級的完整說明（認卡、驅動只能用 apt、事故記錄）見 `/home/john/SYSTEM_NOTES.md`。
