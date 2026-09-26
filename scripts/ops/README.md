# 主機維運通知（ops）

2026-09-26 建立。主機層的告警集中送到 Telegram，**只發給 John 一人**
（沿用 openclaw bot：`/home/john/.openclaw.env` 的 `TELEGRAM_BOT_TOKEN`＋`TELEGRAM_CHAT_ID`）。

## 推播原則
- 只推**需要人處理**的事，而且只在**狀態改變**時推：新問題一次、恢復一次。
- 持續中的問題不重複推，列在每天 08:00 的摘要。**摘要沒收到＝監控本身壞了。**
- 已知且已決定處理方式的問題放 `healthcheck.py` 的 `KNOWN_ISSUES`（與 `smartd_notify.sh` 的
  `KNOWN_SERIALS`），不告警、只列摘要。**問題解決（例如壞碟拆掉）後要移除。**

## 元件
| 檔案 | 作用 | 由誰呼叫 |
|---|---|---|
| `notify.py` | 唯一發送點；失敗進佇列（`/var/lib/ops/notify_spool.jsonl`，48h）、立旗標 `/tmp/ops_notify_failing` | 下列全部 |
| `healthcheck.py` | RAID、SMART、磁碟空間、GPU 溫度／Xid／溫控旗標、服務、容器、y1crm health、記憶體／OOM、備份新舊 | `ops-healthcheck.timer`（每 10 分） |
| `healthcheck.py --summary` | 每日摘要（心跳） | `ops-summary.timer`（08:00） |
| `md_notify.sh` | mdadm 事件（已濾掉開機時 `/dev/md/md0` 別名誤報與 NewArray） | `/etc/mdadm/mdadm.conf` 的 `PROGRAM` |
| `smartd_notify.sh` | smartd 告警 | `/etc/smartmontools/run.d/20ops-notify` |

log：`/var/log/ops/`（放 SSD，不放單碟的 /mnt/raid1）；狀態：`/var/lib/ops/health_state.json`。

## 常用
```bash
sudo python3 healthcheck.py --dry-run          # 看目前狀態，不發送
sudo python3 notify.py "訊息"                   # 手動發一則
sudo python3 notify.py --flush                  # 補送佇列
systemctl list-timers | grep ops-
```

## 還原安裝（重灌或 /etc 遺失時）
```bash
cd /home/john/y1crm/scripts/ops
sudo cp ops-healthcheck.{service,timer} ops-summary.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ops-healthcheck.timer ops-summary.timer
echo 'PROGRAM /home/john/y1crm/scripts/ops/md_notify.sh' | sudo tee -a /etc/mdadm/mdadm.conf
sudo systemctl restart mdmonitor
printf '#!/bin/bash\nexec /home/john/y1crm/scripts/ops/smartd_notify.sh\n' | sudo tee /etc/smartmontools/run.d/20ops-notify
sudo chmod 755 /etc/smartmontools/run.d/20ops-notify
```
