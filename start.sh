#!/bin/bash
# y1crm — 助聽器門市 CRM（TWIII 式架構，port 8004，獨立 venv 不動 TWII/TWIII）
cd "$(dirname "$0")"
PY=/home/john/y1crm/venv/bin/python
echo "啟動 y1crm → http://localhost:8004"
nohup "$PY" run.py > /home/john/y1crm/y1crm.log 2>&1 &
echo "PID $!  （日誌：/home/john/y1crm/y1crm.log）"
