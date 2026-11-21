# -*- coding: utf-8 -*-
"""
车队多车化后端（第四阶段）

在 cloud_backend_dual.py 基础上推广到 N 辆车：
  1. 动态发现：启动时用 ListDevices 自动发现 car_* 设备，不再硬编码；
     每 30 秒刷新一次（新注册的车自动进入车队）
  2. N 车镜像：每辆车独立位置预演（20Hz），轮询影子校正（每车 0.3s 轮转，
     总 API 频率恒定，不随车辆数增长）
  3. 全对全防撞：任意两车接近都自动双车停车（刹车余量随接近速度加大）
  4. 兼容旧接口：/api/state、/api/devices（Car APP）、/api/pair（CarDual）
     均可用，老 APP 无需任何改动；新增 /api/cars、/api/stopall

运行：python cloud_backend_fleet.py   （车端 car_simulator.py 需同时在跑）
"""

import os
import json
import math
import queue
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.auth.credentials import DerivedCredentials
from huaweicloudsdkiotda.v5 import (
    IoTDAClient, ListDevicesRequest, ShowDeviceShadowRequest,
    CreateCommandRequest, DeviceCommandRequest)

# ---------------- 配置 ----------------
CONFIG = {
    "ak": os.environ.get("HUAWEICLOUD_SDK_AK", "YOUR_HUAWEI_AK"),
    "sk": os.environ.get("HUAWEICLOUD_SDK_SK", "YOUR_HUAWEI_SK"),
    "project_id": "YOUR_PROJECT_ID",
    "region_id": "cn-north-4",
    "endpoint": "https://YOUR_APP_ENDPOINT",
    "prefix": "YOUR_PRODUCT_ID",   # 设备ID前缀（发现 car_* 用）
    "node_pattern": "car_",                 # 车队节点名前缀
    "http_port": 9090,
    "poll_step": 0.3,        # 轮转轮询间隔（每步查一辆车，频率恒定）
    "refresh_sec": 30.0,     # 车队发现刷新周期
    "cmd_service_id": "car_control",
}

# ---------------- 防撞参数 ----------------
TICK_HZ = 20
MAX_SPEED = 1.5
SAFE_DIST = 1.0
PREDICT_HORIZON = 3.0
PREDICT_DT = 0.1
COOLDOWN_SEC = 3.0

# ---------------- IoTDA 客户端 ----------------
client = None


def build_client():
    global client
    credentials = BasicCredentials(
        CONFIG["ak"], CONFIG["sk"], CONFIG["project_id"])
    credentials.with_derived_predicate(
        DerivedCredentials.get_default_derived_predicate())
    region = Region(CONFIG["region_id"], CONFIG["endpoint"])
    client = IoTDAClient.new_builder() \
        .with_credentials(credentials) \
        .with_region(region) \
        .build()
    print(f"[SDK] IoTDAClient 初始化完成（{CONFIG['region_id']}）")


def send_command(device_id: str, name: str, paras: dict):
    request = CreateCommandRequest()
    request.device_id = device_id
    request.body = DeviceCommandRequest(
        service_id=CONFIG["cmd_service_id"], command_name=name, paras=paras)
    try:
        client.create_command(request)
    except exceptions.ClientRequestException as e:
        raise RuntimeError(
            f"命令下发失败 {e.status_code} {e.error_code}: {e.error_msg}")


def get_shadow(device_id: str) -> dict:
    request = ShowDeviceShadowRequest()
    request.device_id = device_id
    response = client.show_device_shadow(request)
    props: dict = {}
    for svc in getattr(response, "shadow", None) or []:
        reported = getattr(svc, "reported", None)
        if reported is None:
            continue
        p = getattr(reported, "properties", None)
        if isinstance(p, dict):
            props.update(p)
    return props


def get_device_status(device_id: str) -> str:
    try:
        request = ListDevicesRequest()
        request.device_id = device_id
        request.limit = 1
        response = client.list_devices(request)
        devices = getattr(response, "devices", None) or []
        if devices:
            return getattr(devices[0], "status", "UNKNOWN")
    except exceptions.ClientRequestException as e:
        print(f"[SDK] 状态查询失败 {e.status_code}: {e.error_msg}")
    return "UNKNOWN"


def discover_fleet() -> dict:
    """发现车队：{短名: 完整设备ID}，按 node_id 前缀 car_ 过滤"""
    try:
        request = ListDevicesRequest()
        request.limit = 50
        response = client.list_devices(request)
        fleet = {}
        for d in getattr(response, "devices", None) or []:
            node = getattr(d, "node_id", "") or ""
            if node.startswith(CONFIG["node_pattern"]):
                fleet[node] = d.device_id
        return dict(sorted(fleet.items()))
    except exceptions.ClientRequestException as e:
        print(f"[发现] 车队查询失败 {e.status_code}: {e.error_msg}")
        return {}


# ---------------- 轨迹落库（SQLite，独立写线程） ----------------
DB_PATH = Path(__file__).with_name("fleet.db")
# WAL 模式 + 忙等待：多进程同时写库时不再报 database is locked
db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=15000")
db_lock = threading.Lock()
write_q: queue.Queue = queue.Queue(maxsize=10000)


def init_db():
    with db_lock:
        db.execute("""
            CREATE TABLE IF NOT EXISTS trajectory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                car_id TEXT NOT NULL,
                ts REAL NOT NULL,
                x REAL, y REAL, theta REAL, speed REAL, battery REAL)
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_traj_car_ts "
                   "ON trajectory(car_id, ts)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL, kind TEXT, detail TEXT)
        """)
        db.commit()


def db_writer():
    """后台写线程：攒批提交，避免磁盘 IO 拖慢轮询"""
    pending = 0
    while True:
        try:
            item = write_q.get(timeout=2.0)
        except queue.Empty:
            with db_lock:
                if pending:
                    db.commit()
                    pending = 0
            continue
        kind = item[0]
        with db_lock:
            if kind == "traj":
                _, car_id, ts, snap = item
                db.execute(
                    "INSERT INTO trajectory(car_id, ts, x, y, theta, speed,"
                    " battery) VALUES(?,?,?,?,?,?,?)",
                    (car_id, ts, snap["x"], snap["y"], snap["theta"],
                     snap["speed"], snap["battery"]))
            else:
                _, ts, ev_kind, detail = item
                db.execute("INSERT INTO events(ts, kind, detail) "
                           "VALUES(?,?,?)", (ts, ev_kind, detail))
            pending += 1
            if pending >= 20:
                db.commit()
                pending = 0


def log_event(kind: str, detail: str):
    try:
        write_q.put_nowait(("event", time.time(), kind, detail))
    except queue.Full:
        pass


def get_track(car_id: str, t_from: float, t_to: float, max_pts: int = 800):
    with db_lock:
        rows = db.execute(
            "SELECT ts, x, y, theta, speed, battery FROM trajectory "
            "WHERE car_id=? AND ts>=? AND ts<=? ORDER BY ts",
            (car_id, t_from, t_to)).fetchall()
    if len(rows) > max_pts:
        step = len(rows) / max_pts
        rows = [rows[int(i * step)] for i in range(max_pts)]
    return rows


# ---------------- 车辆镜像 ----------------
class CarMirror:
    def __init__(self):
        self.lock = threading.Lock()
        self.x, self.y = 0.0, 0.0
        self.theta = 0.0
        self.vx, self.vy = 0.0, 0.0
        self.speed = 0.0
        self.battery = 100.0
        self.online = False
        self.shadow_synced = False

    def snapshot(self) -> dict:
        with self.lock:
            return {"x": round(self.x, 3), "y": round(self.y, 3),
                    "theta": round(self.theta, 1),
                    "speed": round(self.speed, 3),
                    "battery": round(self.battery, 1), "online": self.online}

    def set_move(self, vx: float, vy: float):
        with self.lock:
            self.vx, self.vy = vx, vy
            self.speed = math.hypot(vx, vy) * MAX_SPEED

    def set_stop(self):
        with self.lock:
            self.vx, self.vy = 0.0, 0.0
            self.speed = 0.0

    def tick(self, dt: float):
        with self.lock:
            if self.speed > 0.001 and self.battery > 0:
                self.x += self.vx * MAX_SPEED * dt
                self.y += self.vy * MAX_SPEED * dt
                self.theta = math.degrees(math.atan2(self.vx, self.vy)) % 360
                self.battery = max(0.0, self.battery - 0.05 * dt)

    def sync_shadow(self, props: dict):
        with self.lock:
            if not props:
                return
            moving = self.speed > 0.001
            try:
                sx = float(props.get("x", self.x))
                sy = float(props.get("y", self.y))
            except (TypeError, ValueError):
                return
            if not moving or math.hypot(sx - self.x, sy - self.y) > 1.5:
                self.x, self.y = sx, sy
                self.theta = float(props.get("theta", self.theta))
                self.speed = float(props.get("speed", 0.0))
                if not moving:
                    self.vx, self.vy = 0.0, 0.0
                self.battery = float(props.get("battery", self.battery))
            self.shadow_synced = True


# ---------------- 车队状态 ----------------
DEVICES: dict = {}     # short -> device_id
MIRRORS: dict = {}     # short -> CarMirror
DEV_LOCK = threading.Lock()

WARN_LOCK = threading.Lock()
WARN_STATE = {"warning": False, "locked": False, "locked_until": 0.0,
              "message": "", "involved": []}


def add_car(short: str, device_id: str):
    with DEV_LOCK:
        if short not in MIRRORS:
            DEVICES[short] = device_id
            MIRRORS[short] = CarMirror()
            print(f"[发现] 新车辆入队: {short} -> {device_id}")


def refresh_fleet():
    fleet = discover_fleet()
    if fleet:
        for short, dev in fleet.items():
            add_car(short, dev)


def dist_between(a: str, b: str) -> float:
    sa = MIRRORS[a].snapshot()
    sb = MIRRORS[b].snapshot()
    return math.hypot(sa["x"] - sb["x"], sa["y"] - sb["y"])


def vel_of(short: str):
    m = MIRRORS[short]
    with m.lock:
        return m.vx, m.vy


def predict_min_dist(new_vx: float, new_vy: float, mover: str) -> float:
    """mover 用新指令预演未来 PREDICT_HORIZON 秒（从下一步长起算，不含当前时刻），
    返回与所有其他车的最近距离"""
    mx, my = MIRRORS[mover].snapshot()["x"], MIRRORS[mover].snapshot()["y"]
    others = []
    for short in list(MIRRORS):
        if short == mover:
            continue
        s = MIRRORS[short].snapshot()
        vx, vy = vel_of(short)
        others.append((s["x"], s["y"], vx, vy))
    min_d = float("inf")
    t = PREDICT_DT
    while t <= PREDICT_HORIZON + 1e-9:
        ax = mx + new_vx * MAX_SPEED * t
        ay = my + new_vy * MAX_SPEED * t
        for ox, oy, ovx, ovy in others:
            d = math.hypot(ax - (ox + ovx * MAX_SPEED * t),
                           ay - (oy + ovy * MAX_SPEED * t))
            if d < min_d:
                min_d = d
        t += PREDICT_DT
    return min_d


def closing_speed(a: str, b: str) -> float:
    ax, ay = MIRRORS[a].snapshot()["x"], MIRRORS[a].snapshot()["y"]
    bx, by = MIRRORS[b].snapshot()["x"], MIRRORS[b].snapshot()["y"]
    avx, avy = vel_of(a)
    bvx, bvy = vel_of(b)
    dx, dy = ax - bx, ay - by
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return 0.0
    rel_vx = (avx - bvx) * MAX_SPEED
    rel_vy = (avy - bvy) * MAX_SPEED
    # 间距变化率 d|a-b|/dt 的相反数：>0 = 正在接近
    return -(rel_vx * dx + rel_vy * dy) / d


def _confirm_stopped(involved):
    """停车确认补发：1.5s 后复查车速，没停住的补发 stop（最多 3 次）"""
    for attempt in range(3):
        time.sleep(1.5)
        still = []
        for short in involved:
            m = MIRRORS.get(short)
            if m is None or not m.online:
                continue
            with m.lock:
                spd = m.speed
            if spd > 0.01:
                still.append(short)
        if not still:
            return
        print(f"[防撞] 复查第{attempt + 1}次：{still} 仍在移动，补发停车")
        for short in still:
            try:
                send_command(DEVICES[short], "stop", {})
            except Exception as e:
                print(f"[防撞] 补发 stop {short} 失败: {e}")
            MIRRORS[short].set_stop()


def trigger_collision_stop(involved, reason: str):
    now = time.time()
    with WARN_LOCK:
        if now < WARN_STATE["locked_until"]:
            return
        WARN_STATE["warning"] = True
        WARN_STATE["locked"] = True
        WARN_STATE["locked_until"] = now + COOLDOWN_SEC
        WARN_STATE["message"] = reason
        WARN_STATE["involved"] = list(involved)
    log_event("collision", reason)
    print(f"[防撞] {reason} -> {'/'.join(involved)} 停车，冷却 {COOLDOWN_SEC}s")
    for short in involved:
        try:
            send_command(DEVICES[short], "stop", {})
        except Exception as e:
            print(f"[防撞] stop {short} 失败: {e}")
        MIRRORS[short].set_stop()
    threading.Thread(target=_confirm_stopped, args=(involved,),
                     daemon=True).start()


def try_move(short: str, vx: float, vy: float):
    now = time.time()
    with WARN_LOCK:
        locked = now < WARN_STATE["locked_until"]

    min_d = predict_min_dist(vx, vy, short)
    if min_d < SAFE_DIST:
        # 驶离豁免：已在危险区内且预演显示正在远离 -> 放行
        nearest_now = min(
            (dist_between(short, other) for other in MIRRORS
             if other != short), default=float("inf"))
        escaping = nearest_now < SAFE_DIST and min_d > nearest_now + 1e-6
        if not escaping:
            trigger_collision_stop(
                [short],
                f"{short} 的移动将使车队最近间距缩至 {min_d:.2f}m")
            raise RuntimeError(
                f"碰撞预警：最近间距将仅 {min_d:.2f}m，已自动停车，"
                f"{COOLDOWN_SEC} 秒后可重新操作")
    if locked:
        raise RuntimeError(
            f"碰撞冷却中（{WARN_STATE['locked_until'] - now:.0f}s），请稍后再试")

    MIRRORS[short].set_move(vx, vy)
    send_command(DEVICES[short], "move",
                 {"vx": round(vx, 2), "vy": round(vy, 2)})


def try_stop(short: str):
    MIRRORS[short].set_stop()
    send_command(DEVICES[short], "stop", {})
    with WARN_LOCK:
        if (all(m.speed < 0.001 for m in MIRRORS.values())
                and time.time() >= WARN_STATE["locked_until"]):
            WARN_STATE["warning"] = False
            WARN_STATE["message"] = ""
            WARN_STATE["involved"] = []


def monitor_collision():
    """全对全连续监测：任意一对车接近且进入刹车余量内即停车"""
    shorts = sorted(MIRRORS)
    for i in range(len(shorts)):
        for j in range(i + 1, len(shorts)):
            a, b = shorts[i], shorts[j]
            d = dist_between(a, b)
            if d > 10.0:
                continue
            closing = closing_speed(a, b)
            if closing <= 0:
                continue
            if d < SAFE_DIST + closing * 2.0:
                trigger_collision_stop(
                    [a, b],
                    f"{a}/{b} 相距 {d:.2f}m 且以约 {closing:.1f} m/s 接近，"
                    f"自动停车")
                return


def tick_loop():
    last_t = time.time()
    while True:
        time.sleep(1.0 / TICK_HZ)
        now = time.time()
        dt = now - last_t
        last_t = now
        for m in MIRRORS.values():
            m.tick(dt)
        try:
            monitor_collision()
        except Exception as e:
            print(f"[防撞] 监测异常: {e}")
        with WARN_LOCK:
            if (WARN_STATE["locked"] and now >= WARN_STATE["locked_until"]
                    and all(m.speed < 0.001 for m in MIRRORS.values())):
                WARN_STATE["locked"] = False
                WARN_STATE["warning"] = False
                WARN_STATE["message"] = ""
                WARN_STATE["involved"] = []


def traj_recorder():
    """1Hz 从镜像取位置补密轨迹（云端查询有 2~3s 延迟，本地积分点更密）"""
    last_t = 0.0
    while True:
        time.sleep(0.5)
        now = time.time()
        if now - last_t < 1.0:
            continue
        last_t = now
        for short, m in list(MIRRORS.items()):
            if not m.shadow_synced:
                continue
            snap = m.snapshot()
            if not snap["online"]:
                continue
            try:
                write_q.put_nowait(("traj", short, now, snap))
            except queue.Full:
                pass


def poll_loop():
    """轮转轮询：每步查一辆车（状态+影子），总频率恒定不随车队规模增长"""
    idx = 0
    last_refresh = 0.0
    while True:
        if time.time() - last_refresh >= CONFIG["refresh_sec"]:
            try:
                refresh_fleet()
            except Exception as e:
                print(f"[发现] 刷新失败: {e}")
            last_refresh = time.time()
        with DEV_LOCK:
            shorts = sorted(MIRRORS)
        if shorts:
            short = shorts[idx % len(shorts)]
            idx += 1
            m = MIRRORS[short]
            try:
                m.online = (get_device_status(DEVICES[short]) == "ONLINE")
                if m.online:
                    m.sync_shadow(get_shadow(DEVICES[short]))
                    # 轨迹落库（每车每次被轮到时记一个点）
                    try:
                        write_q.put_nowait(
                            ("traj", short, time.time(), m.snapshot()))
                    except queue.Full:
                        pass
            except Exception as e:
                print(f"[轮询] {short}: {e}")
        time.sleep(CONFIG["poll_step"])


# ---------------- HTTP API（兼容旧 APP + 新车队接口） ----------------
def resolve_dev(query: dict) -> str:
    name = (query.get("dev") or [""])[0]
    with DEV_LOCK:
        if name in MIRRORS:
            return name
        for short, full in DEVICES.items():
            if name == full:
                return short
    return name


def warn_public():
    with WARN_LOCK:
        w = {k: v for k, v in WARN_STATE.items() if k != "locked_until"}
    return w


HTML_PLAYBACK = Path(__file__).with_name("playback.html")


class ApiHandler(BaseHTTPRequestHandler):
    def handle(self):
        # 客户端主动断开（关页面/APP断线）是正常现象，静默处理不刷屏
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError, TimeoutError):
            pass

    def _send(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/api/cars":
                self._send(200, {"ts": time.time(),
                                 "cars": {k: v.snapshot()
                                          for k, v in sorted(MIRRORS.items())},
                                 **warn_public()})
            elif url.path == "/api/devices":
                # 兼容旧单车 APP
                self._send(200, {"devices": {k: v.snapshot()
                                             for k, v in sorted(MIRRORS.items())}})
            elif url.path == "/api/pair":
                # 兼容 CarDual：取前两辆
                shorts = sorted(MIRRORS)[:2]
                if len(shorts) == 2:
                    self._send(200, {
                        "cars": {k: MIRRORS[k].snapshot() for k in shorts},
                        "distance": round(dist_between(*shorts), 2),
                        "safe_dist": SAFE_DIST,
                        **warn_public()})
                else:
                    self._send(200, {"cars": {}, "distance": None,
                                     "safe_dist": SAFE_DIST,
                                     **warn_public()})
            elif url.path == "/api/track":
                short = resolve_dev(query)
                if short not in MIRRORS:
                    self._send(404, {"error": f"未知设备 {short}"})
                    return
                now = time.time()
                t_from = float((query.get("from") or [now - 600])[0])
                t_to = float((query.get("to") or [now])[0])
                max_pts = int((query.get("max") or ["800"])[0])
                rows = get_track(short, t_from, t_to, max_pts)
                self._send(200, {"car_id": short, "from": t_from, "to": t_to,
                                 "points": [{"ts": r[0], "x": r[1], "y": r[2],
                                             "theta": r[3], "speed": r[4],
                                             "battery": r[5]} for r in rows]})
            elif url.path == "/api/events":
                n = int((query.get("last") or ["50"])[0])
                with db_lock:
                    rows = db.execute(
                        "SELECT ts, kind, detail FROM events "
                        "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
                self._send(200, {"events": [{"ts": r[0], "kind": r[1],
                                             "detail": r[2]} for r in rows]})
            elif url.path == "/playback":
                body = HTML_PLAYBACK.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/state":
                short = resolve_dev(query)
                m = MIRRORS.get(short)
                if m is None:
                    self._send(404, {"error": f"未知设备 {short}"})
                else:
                    self._send(200, m.snapshot())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            payload = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "bad json"})
            return

        try:
            if url.path == "/api/stopall":
                for short in list(MIRRORS):
                    try:
                        send_command(DEVICES[short], "stop", {})
                        MIRRORS[short].set_stop()
                    except Exception:
                        pass
                self._send(200, {"ok": True, "cmd": "stopall"})
                return

            short = resolve_dev(query)
            if short not in MIRRORS:
                self._send(400, {"error": f"未知设备 {short}"})
                return

            if url.path == "/api/move":
                vx = max(-1.0, min(1.0, float(payload.get("vx", 0))))
                vy = max(-1.0, min(1.0, float(payload.get("vy", 0))))
                try:
                    try_move(short, vx, vy)
                    self._send(200, {"ok": True, "cmd": "move"})
                except RuntimeError as blocked:
                    self._send(409, {"ok": False, "blocked": str(blocked)})
            elif url.path == "/api/stop":
                try_stop(short)
                self._send(200, {"ok": True, "cmd": "stop"})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(502, {"error": str(e)})

    def log_message(self, *args):
        pass


def main():
    print("[启动] 初始化 IoTDA 客户端...")
    build_client()
    init_db()
    threading.Thread(target=db_writer, daemon=True).start()
    threading.Thread(target=traj_recorder, daemon=True).start()
    print(f"[DB] 轨迹库: {DB_PATH}")
    print("[启动] 发现车队...")
    refresh_fleet()
    if not MIRRORS:
        print("[启动] 警告：未发现任何 car_* 设备（车端模拟器未运行？）")
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG["http_port"]), ApiHandler)
    print(f"[HTTP] 车队后端已就绪: http://0.0.0.0:{CONFIG['http_port']}")
    print(f"       车队: {sorted(MIRRORS)}")
    print(f"       /api/cars  /api/state?dev=car_001  /api/pair（兼容）")
    print(f"       /api/move?dev=car_001  /api/stop  /api/stopall")
    print(f"       /api/track?dev=car_001  /api/events  /playback（回放页）")
    server.serve_forever()


if __name__ == "__main__":
    main()
