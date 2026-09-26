#!/bin/bash
# 每日備份：/mnt/raid1 上無法重新產生的資料 + MySQL，存到 NVMe 上的 /mnt/backup。
# RAID1 自 2026-09-15 降級成單碟，這是 sda 壞掉時的唯一退路。
# 還原步驟見同目錄 README.md。
set -uo pipefail

DEST=/mnt/backup
SRC=/mnt/raid1
KEEP_DAYS=7
TODAY=$(date +%F)
LOG=$DEST/backup.log
FAIL_FLAG=/tmp/backup_failing

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
fail() { log "FAILED $*"; date -Is > "$FAIL_FLAG"; exit 1; }

# 沒掛載就不做，免得寫進根目錄把系統碟塞爆
mountpoint -q "$DEST" || { echo "$DEST 未掛載"; date -Is > "$FAIL_FLAG"; exit 1; }
mkdir -p "$DEST/raid1" "$DEST/mysql"
log "── 開始備份 ──"

# 1) RAID 檔案：每日快照，未變動的檔案以硬連結共用，7 份約只佔 1 份多一點
rsync -a --delete \
  --link-dest="$DEST/raid1/latest" \
  --exclude=/llm_models --exclude=/ollama --exclude=/ollama_data \
  --exclude=/docker_root --exclude=/containerd_data --exclude=/vllm_cache \
  --exclude=/huggingface_cache --exclude=/webui/cache --exclude=/swap.img \
  --exclude=/lost+found \
  "$SRC/" "$DEST/raid1/$TODAY/" || fail "rsync rc=$?"
ln -sfn "$TODAY" "$DEST/raid1/latest"
log "raid1 快照完成 $(du -sh "$DEST/raid1/$TODAY" | cut -f1)"

# 2) MySQL：全部使用者資料庫
DBS=$(mysql --defaults-file=/etc/mysql/debian.cnf -N -e \
  "select schema_name from information_schema.schemata where schema_name not in ('mysql','sys','information_schema','performance_schema')") \
  || fail "列資料庫失敗"
OUT="$DEST/mysql/$TODAY.sql.gz"
mysqldump --defaults-file=/etc/mysql/debian.cnf --single-transaction --quick \
  --routines --events --triggers --databases $DBS | gzip -6 > "$OUT.tmp" \
  && mv "$OUT.tmp" "$OUT" || { rm -f "$OUT.tmp"; fail "mysqldump"; }
log "mysql 完成 ($DBS) $(du -sh "$OUT" | cut -f1)"

# 3) 保留 7 天：依檔名日期判斷（rsync -a 會把快照目錄 mtime 設成來源的，不可靠）
CUTOFF=$(date -d "-$KEEP_DAYS days" +%F)
for d in "$DEST"/raid1/20??-??-??; do
  [ -d "$d" ] && [[ "$(basename "$d")" < "$CUTOFF" ]] && rm -rf "$d"
done
for f in "$DEST"/mysql/20??-??-??.sql.gz; do
  [ -f "$f" ] && [[ "$(basename "$f" .sql.gz)" < "$CUTOFF" ]] && rm -f "$f"
done

rm -f "$FAIL_FLAG"
log "── 備份結束，/mnt/backup 已用 $(df -h --output=pcent "$DEST" | tail -1 | tr -d ' ') ──"
