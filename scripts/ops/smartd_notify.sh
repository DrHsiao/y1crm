#!/bin/bash
# smartd 告警 → Telegram（由 /etc/smartmontools/run.d/20ops-notify 呼叫），2026-09-26
# smartd 透過環境變數傳入 SMARTD_DEVICE / SMARTD_FAILTYPE / SMARTD_MESSAGE / SMARTD_DEVICEINFO
# 已知壞碟（與 healthcheck.py 的 KNOWN_ISSUES 一致）不重複告警；拆掉後把序號移除。
KNOWN_SERIALS="57JEK0PAFPBE"
for sn in $KNOWN_SERIALS; do
  case "$SMARTD_DEVICEINFO" in *"$sn"*) exit 0 ;; esac
done
MSG="🔴 硬碟 SMART 告警（smartd）
裝置：${SMARTD_DEVICE:-?}　類型：${SMARTD_FAILTYPE:-?}
${SMARTD_DEVICEINFO}
${SMARTD_MESSAGE}"
exec /usr/bin/python3 "$(dirname "$(readlink -f "$0")")/notify.py" "$MSG"
