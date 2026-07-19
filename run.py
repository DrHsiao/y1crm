#!/usr/bin/env python3
"""
run.py — y1crm 入口（比照 TWIII）

助聽器門市 CRM。原 Blazor 版封存於 archive/y1crm-blazor/，
本服務為 TWIII 式架構重寫：FastAPI + 單檔 webui，直連本機 crm1 MySQL。

連接埠 8004（TWII=8002、TWIII=8003，互不衝突）。

啟動：/home/john/y1crm/venv/bin/python run.py
"""
from __future__ import annotations

import logging

import uvicorn

import config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("y1crm")

HOST = config.SERVER["host"]
PORT = int(config.SERVER["port"])

if __name__ == "__main__":
    from api.app import app
    log.info(f"啟動 y1crm API → http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
