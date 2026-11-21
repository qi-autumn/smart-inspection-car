# -*- coding: utf-8 -*-
"""
V3 轨迹落库（第③步）：SQLite 存储与治理

设计（对照路线图 ③ 与协议 §7.3 v1.1 勘误）：
  - 表 track_points(device_id, x, y, heading, speed, battery, ts)
  - 插入时机：挂在业务后端状态缓存刷新处，由调用方判断"状态有变化才插行"
    （拉模式收不到推送，空闲 60s 的重复上报不产生重复点）
  - 数据治理：7 天过期 + 单车 20000 条上限（启动时与每 6 小时自动清理）
  - 并发：单后端进程内一把锁串行写（路线图风险表决策，不上多进程）
"""

import sqlite3
import threading
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
DB_PATH = SERVER_DIR / "fleet.db"

EXPIRE_DAYS = 7
MAX_PER_CAR = 20000

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None


def init() -> None:
    """建库建表（幂等），WAL + busy_timeout 防锁死"""
    global _CONN
    _CONN = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15)
    _CONN.execute("PRAGMA journal_mode=WAL")
    _CONN.execute("PRAGMA busy_timeout=15000")
    _CONN.execute("""
        CREATE TABLE IF NOT EXISTS track_points (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            x         REAL,
            y         REAL,
            heading   REAL,
            speed     REAL,
            battery   REAL,
            ts        INTEGER NOT NULL
        )""")
    _CONN.execute(
        "CREATE INDEX IF NOT EXISTS idx_track_dev_ts ON track_points(device_id, ts)")
    _CONN.commit()
    print(f"[轨迹] SQLite 就绪: {DB_PATH}")


def cleanup() -> int:
    """数据治理：7 天过期 + 单车上限。返回删除总行数"""
    now = int(time.time())
    cutoff = now - EXPIRE_DAYS * 86400
    removed = 0
    with _LOCK:
        cur = _CONN.execute("DELETE FROM track_points WHERE ts < ?", (cutoff,))
        removed += cur.rowcount
        devs = [r[0] for r in _CONN.execute(
            "SELECT DISTINCT device_id FROM track_points")]
        for dev in devs:
            # 保留该设备最近 MAX_PER_CAR 条（按 ts 降序），其余删除
            cur = _CONN.execute("""
                DELETE FROM track_points
                WHERE device_id = ? AND ts <= (
                    SELECT ts FROM track_points WHERE device_id = ?
                    ORDER BY ts DESC LIMIT 1 OFFSET ?)""",
                (dev, dev, MAX_PER_CAR - 1))
            removed += cur.rowcount
        _CONN.commit()
    return removed


def cleanup_loop(interval_hours: float = 6.0) -> None:
    """周期清理（后台线程）"""
    while True:
        time.sleep(interval_hours * 3600)
        try:
            n = cleanup()
            if n:
                print(f"[轨迹] 定时清理完成，删除 {n} 行")
        except Exception as e:
            print(f"[轨迹] 定时清理失败: {e}")


def insert_point(device_id: str, x: float, y: float, heading: float,
                 speed: float, battery: float, ts: int | None = None) -> None:
    """插一个轨迹点（调用方保证"变化才插"）"""
    with _LOCK:
        _CONN.execute(
            "INSERT INTO track_points(device_id,x,y,heading,speed,battery,ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (device_id, x, y, heading, speed, battery, ts or int(time.time())))
        _CONN.commit()


def query(device_id: str, from_ts: int, to_ts: int) -> list[dict]:
    """按时间窗查轨迹点，时间升序"""
    with _LOCK:
        rows = _CONN.execute(
            "SELECT x, y, heading, speed, ts FROM track_points "
            "WHERE device_id = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
            (device_id, from_ts, to_ts)).fetchall()
    return [{"x": r[0], "y": r[1], "heading": r[2], "speed": r[3], "ts": r[4]}
            for r in rows]


def count(device_id: str | None = None) -> int:
    with _LOCK:
        if device_id is None:
            return _CONN.execute("SELECT COUNT(*) FROM track_points").fetchone()[0]
        return _CONN.execute(
            "SELECT COUNT(*) FROM track_points WHERE device_id = ?",
            (device_id,)).fetchone()[0]
