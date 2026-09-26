#!/bin/bash
# mdadm --monitor 的 PROGRAM（/etc/mdadm/mdadm.conf），2026-09-26
# mdadm 呼叫方式：md_notify.sh <事件> <陣列> [成員碟]
EV="$1"; MD="$2"; DEV="$3"
LOGF=/var/log/ops/md_events.log
mkdir -p /var/log/ops; echo "$(date '+%F %T') $EV $MD $DEV" >> "$LOGF"
# 誤報過濾（2026-09-26 實測）：mdadm 啟動時會對不存在的別名 /dev/md/md0 報 DeviceDisappeared，
#   同時對真正的 /dev/md0 報 NewArray。陣列其實好好的——不發，只記 log。
case "$EV" in
  NewArray) exit 0 ;;
  DeviceDisappeared)
    base=$(basename "$MD")
    if grep -qE "^${base} : active" /proc/mdstat 2>/dev/null; then
      echo "  └ 略過：$base 仍在 /proc/mdstat 且 active（別名誤報）" >> "$LOGF"; exit 0
    fi ;;
esac
case "$EV" in
  Fail|FailSpare)      L="🔴 RAID 硬碟故障，已踢出陣列" ;;
  DegradedArray)       L="🔴 RAID 降級（少了成員）" ;;
  DeviceDisappeared)   L="🔴 RAID 陣列消失" ;;
  SparesMissing)       L="🟠 RAID 缺少備援碟" ;;
  RebuildStarted)      L="🟡 RAID 開始重建" ;;
  RebuildFinished)     L="🟢 RAID 重建完成" ;;
  SpareActive)         L="🟢 備援碟已接手" ;;
  TestMessage)         L="🧪 RAID 通知測試" ;;
  *)                   L="ℹ️ RAID 事件" ;;
esac
MSG="$L
事件：$EV　陣列：$MD${DEV:+　成員：$DEV}
$(cat /proc/mdstat 2>/dev/null | sed -n '2,3p')"
exec /usr/bin/python3 "$(dirname "$(readlink -f "$0")")/notify.py" "$MSG"
