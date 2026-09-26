# 每日備份（raid1-backup）

RAID1（md0，/mnt/raid1）自 2026-09-15 單碟運作，這份備份是 sda 壞掉時的退路。

- 來源：`/mnt/raid1` 上無法重新產生的資料（約 1.1G，已排除模型、容器映像、快取、swap）＋ MySQL 全部使用者資料庫。
- 目的地：`/mnt/backup`＝NVMe 上的 LV `ubuntu-vg/backup`（80G），和 RAID 是不同實體碟。
- 每日 03:30，保留 7 天。RAID 快照用硬連結，未變動的檔案不重複佔空間。
- log：`/mnt/backup/backup.log`；失敗時建立 `/tmp/backup_failing` 旗標。
- ⚠️ 這份備份和主機在同一台機器上，`y1crm_uploads`（客戶個資）與 MySQL 仍需另存一份到機器外。

## 安裝（一次性）

```bash
sudo lvcreate -L 80G -n backup ubuntu-vg
sudo mkfs.ext4 -L backup /dev/ubuntu-vg/backup
sudo mkdir -p /mnt/backup
echo "UUID=$(sudo blkid -s UUID -o value /dev/ubuntu-vg/backup) /mnt/backup ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount /mnt/backup
sudo cp /home/john/y1crm/scripts/backup/raid1-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now raid1-backup.timer
sudo systemctl start raid1-backup    # 立刻先跑第一次
```

## 還原

```bash
sudo rsync -a /mnt/backup/raid1/latest/ /mnt/raid1/          # 檔案
zcat /mnt/backup/mysql/<日期>.sql.gz | sudo mysql --defaults-file=/etc/mysql/debian.cnf   # 資料庫
```
