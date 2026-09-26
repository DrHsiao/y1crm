# -*- coding: utf-8 -*-
"""MySQL 連線池（2026-09-26 新增，取代各處的 pymysql.connect）

用法與 pymysql.connect 完全相同，只換函式名：

    import dbpool
    conn = dbpool.connect(**DB, connect_timeout=5)   # 參數照舊
    ...
    conn.close()                                     # 歸還連線池，不是真的斷線

## 設計
- 每一組「連線參數」各自一個池（DBUtils PooledDB）：逾時、DictCursor、
  autocommit 等設定各呼叫點原本怎麼寫就怎麼生效，不會互相污染。
- 不設連線上限（maxconnections=0）、不阻塞：最壞情況＝舊行為（每次開新連線），
  絕不會因為池滿而卡住（實盤引擎熱迴圈不能等）。閒置最多留 MAXCACHED 條。
- 取出時先 ping，斷線（MySQL wait_timeout、重啟）自動重連。
- 歸還時一律 rollback（reset=True）：沒 commit 的交易和原本 close() 一樣被丟棄，
  下一個使用者拿到的是乾淨連線。
- fork 安全：池以行程 PID 區分，子行程（ProcessPoolExecutor 等）自建新池，
  不會共用父行程的 socket。
- 沒裝 DBUtils 時自動退回 pymysql.connect（stderr 警告一次），服務不會因此起不來。

⚠ 本檔在 TWII、TWIII（scripts/）、y1crm（api/）各有一份相同副本，改動請三處同步。
"""
import os
import sys
import threading

import pymysql

try:
    from dbutils.pooled_db import PooledDB
except ImportError:          # pragma: no cover
    PooledDB = None

MAXCACHED = 5                # 每個池最多保留的閒置連線數

_pools = {}
_lock = threading.Lock()
_orphans = []                # fork 後繼承自父行程的池：保留參照、永不關閉（關了會送 QUIT 到共用 socket）
_warned = False


def _after_fork():
    global _pools, _lock
    _orphans.append(_pools)
    _pools = {}
    _lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _fallback(args, kw):
    global _warned
    if not _warned:
        _warned = True
        print("[dbpool] ⚠ 未安裝 DBUtils，改用 pymysql.connect（沒有連線池）", file=sys.stderr)
    return pymysql.connect(*args, **kw)


def connect(*args, **kw):
    """取得池化連線；參數同 pymysql.connect。"""
    if args or PooledDB is None:
        return _fallback(args, kw) if PooledDB is None else pymysql.connect(*args, **kw)
    try:
        key = tuple(sorted(kw.items()))
        hash(key)
    except TypeError:        # 參數含不可雜湊的值（極少見）→ 不池化
        return pymysql.connect(**kw)
    pool = _pools.get(key)
    if pool is None:
        with _lock:
            pool = _pools.get(key)
            if pool is None:
                pool = PooledDB(pymysql, mincached=0, maxcached=MAXCACHED,
                                maxshared=0, maxconnections=0, blocking=False,
                                reset=True, ping=1, **kw)
                _pools[key] = pool
    return _Conn(pool.connection())


class _Conn:
    """池化連線的外殼：DBUtils 只轉發 DB-API 標準方法，pymysql 特有的
    （insert_id、escape、open、select_db…）改直接轉給底層 pymysql 連線，
    讓既有程式碼不必改寫。close() 與 with 區塊結束＝歸還連線池。"""
    __slots__ = ("_pooled",)

    def __init__(self, pooled):
        self._pooled = pooled

    def __getattr__(self, name):
        try:
            return getattr(self._pooled, name)
        except AttributeError:
            raw = getattr(getattr(self._pooled, "_con", None), "_con", None)
            if raw is None:
                raise
            return getattr(raw, name)

    def close(self):
        self._pooled.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._pooled.close()
        return False


def stats():
    """各池閒置連線數（除錯用，不含密碼）。"""
    out = []
    for key, pool in list(_pools.items()):
        d = dict(key)
        out.append({"db": d.get("database") or d.get("db"), "user": d.get("user"),
                    "idle": len(getattr(pool, "_idle_cache", []))})
    return out
