#!/bin/bash
# 停止 y1crm（只殺本專案的 run.py，不影響 TWII/TWIII）
pkill -f "/home/john/y1crm/venv/bin/python run.py" && echo "y1crm 已停止" || echo "y1crm 未在執行"
