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
| `*.service` / `*.timer` | 上面三者的 systemd 單元 | — |

- 為什麼要預熱：`KEEP_ALIVE=-1` 只保證「已載入的不卸載」，重開機後 VRAM 是空的。
  沒有它，店員第一次 OCR 要乾等 7 分鐘，LINE 客服則是第一位客戶必收道歉文並被轉人工 4 小時。
- 看門狗的告警＝旗標檔 `/tmp/ollama_watchdog_failing` ＋ unit 變 failed
  （`systemctl --failed`）＋ journal 訊息；log 在 `/mnt/raid1/monitoring/ollama_watchdog.log`。
  卡上是**別的**模型時它**不驅逐**（可能有人正在用），只告警。
- 溫控失效的徵兆：`/tmp/gpu_thermal_failing` 旗標與 log 裡的 `QUERY_FAILED`（全部卡都讀不到）。
- 單卡失效：log 的 `CARD_ERROR`/`CARD_MISSING` 與 `/tmp/gpu_thermal_card_missing` 旗標；應在線的卡以序號列在腳本 `EXPECTED_CARDS`，換卡/拆卡要同步改。一張卡掉線時整批查詢會失敗，腳本會退回逐卡（依 bus）查詢，其餘好卡照樣受保護。

## 安裝／還原（重灌或換機後）

```bash
sudo cp scripts/gpu/*.service scripts/gpu/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ollama-warmup.service ollama-watchdog.timer
# 溫控（root crontab 每分鐘）
sudo crontab -l | grep -q gpu_thermal_guard || \
  ( sudo crontab -l; echo '* * * * * /usr/bin/python3 /home/john/y1crm/scripts/gpu/gpu_thermal_guard.py >> /mnt/raid1/monitoring/gpu_thermal_cron.log 2>&1' ) | sudo crontab -
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
