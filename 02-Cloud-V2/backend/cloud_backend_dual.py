# -*- coding: utf-8 -*-
"""
双车防撞后端（扩展实验：两辆小车同屏 + 碰撞自动停车）

与 cloud_backend.py 的区别：
  1. 端口 9091（与单车的 9090 并存，互不影响）
  2. 内部维护两辆车的"预演"运动学状态（20Hz 积分），周期性用云端影子校正
  3. 收到 move 指令时先预测未来 3 秒两车轨迹：
       * 若预测最近距离 < 安全距离 -> 拒绝该指令 + 向两辆车同时下发 stop
       * APP 通过 /api/pair 看到预警状态

运行：python cloud_backend_dual.py   （车端两个 MQTT 客户端需同时在跑）
"""

import os
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.auth.credentials import DerivedCredentials
from huaweicloudsdkiotda.v5 import *

# ---------------- 配置（与 cloud_backend.py 相同的账号信息） ----------------
CONFIG = {
    "ak": os.environ.get("HUAWEICLOUD_SDK_AK", "YOUR_HUAWEI_AK"),
    "sk": os.environ.get("HUAWEICLOUD_SDK_SK", "YOUR_HUAWEI_SK"),
    "project_id": "YOUR_PROJECT_ID",
    "region_id": "cn-north-4",
    "endpoint": "https://YOUR_APP_ENDPOINT",

    "devices": {
        "car_001": "YOUR_PRODUCT_ID_car_001",
        "car_002": "YOUR_PRODUCT_ID_car_002",
    },

    "http_port": 9091,        # 双车后端端口（区别于单车后端 9090）

    "poll_interval": 2.0,     # 影子校正周期
    "cmd_service_id": "car_control",
}

# ---------------- 防撞参数 ----------------
TICK_HZ = 20               # 内部预演频率
MAX_SPEED = 1.5            # 与车端 car_mqtt_client.py 保持一致（米/秒）
SAFE_DIST = 1.0            # 安全距离（米）：两车中心小于该值视为危险
PREDICT_HORIZON = 3.0      # move 预演时长（秒）
PREDICT_DT = 0.1           # 预演步长（秒）
COOLDOWN_SEC = 3.0         # 触发碰撞停车后的指令冷却时间（秒）

# ---------------- IoTDA 客户端（官方模板写法，同单车后端） ----------------
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


# ---------------- 双车预演状态 ----------------
class CarMirror:
    """每辆车的内部镜像：积分指令得到 20Hz 位置，周期性用影子校正"""

    def __init__(self):
        self.lock = threading.Lock()
        self.x, self.y = 0.0, 0.0
        self.theta = 0.0
        self.vx, self.vy = 0.0, 0.0
        self.speed = 0.0
        self.battery = 100.0
        self.online = False
        self.shadow_synced = False   # 是否已从影子拿到过真实位置

    def snapshot(self) -> dict:
        with self.lock:
            return {"x": round(self.x, 3), "y": round(self.y, 3),
                    "theta": round(self.theta, 1), "speed": round(self.speed, 3),
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
            # 行驶中不强行校正（影子最多滞后2秒，会造成位置回跳），
            # 只在停止或偏差过大时对齐
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


MIRRORS = {short: CarMirror() for short in CONFIG["devices"]}
WARN_LOCK = threading.Lock()
WARN_STATE = {"warning": False, "locked": False, "locked_until": 0.0,
              "message": ""}


def dist_between() -> float:
    a = MIRRORS["car_001"].snapshot()
    b = MIRRORS["car_002"].snapshot()
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def predict_min_dist(new_vx: float, new_vy: float, mover: str) -> float:
    """预演未来 PREDICT_HORIZON 秒：mover 用新指令，另一辆按当前速度（或静止）"""
    a = MIRRORS["car_001"].snapshot()
    b = MIRRORS["car_002"].snapshot()
    # 内部真实速度矢量（快照里只有标量速度，从镜像取矢量）
    av = MIRRORS["car_001"]
    bv = MIRRORS["car_002"]
    with av.lock:
        avx, avy = av.vx, av.vy
    with bv.lock:
        bvx, bvy = bv.vx, bv.vy

    ax, ay = a["x"], a["y"]
    bx, by = b["x"], b["y"]
    if mover == "car_001":
        avx, avy = new_vx, new_vy
    else:
        bvx, bvy = new_vx, new_vy

    min_d = float("inf")
    t = PREDICT_DT
    while t <= PREDICT_HORIZON + 1e-9:
        ax2 = ax + avx * MAX_SPEED * t
        ay2 = ay + avy * MAX_SPEED * t
        bx2 = bx + bvx * MAX_SPEED * t
        by2 = by + bvy * MAX_SPEED * t
        d = math.hypot(ax2 - bx2, ay2 - by2)
        if d < min_d:
            min_d = d
        t += PREDICT_DT
    return min_d


def _confirm_stopped():
    """停车确认：复查影子车速，没停住的补发 stop（最多 3 次，防命令丢失）"""
    for attempt in range(3):
        time.sleep(1.5)
        still_moving = []
        for short in CONFIG["devices"]:
            m = MIRRORS[short]
            if not m.online:
                continue
            with m.lock:
                spd = m.speed     # 该值周期性被影子校正，反映真实车速
            if spd > 0.01:
                still_moving.append(short)
        if not still_moving:
            return
        print(f"[防撞] 复查第{attempt + 1}次：{still_moving} 仍在移动，补发停车")
        for short in still_moving:
            try:
                send_command(CONFIG["devices"][short], "stop", {})
            except Exception as e:
                print(f"[防撞] 补发 stop {short} 失败: {e}")
            MIRRORS[short].set_stop()


def trigger_collision_stop(reason: str):
    """双车停车 + 进入冷却锁 + 后台确认补发"""
    now = time.time()
    with WARN_LOCK:
        if now < WARN_STATE["locked_until"]:
            return
        WARN_STATE["warning"] = True
        WARN_STATE["locked"] = True
        WARN_STATE["locked_until"] = now + COOLDOWN_SEC
        WARN_STATE["message"] = reason
    print(f"[防撞] {reason} -> 双车停车，冷却 {COOLDOWN_SEC}s")
    for short, dev in CONFIG["devices"].items():
        try:
            send_command(dev, "stop", {})
        except Exception as e:
            print(f"[防撞] stop {short} 失败: {e}")
    MIRRORS["car_001"].set_stop()
    MIRRORS["car_002"].set_stop()
    threading.Thread(target=_confirm_stopped, daemon=True).start()


def try_move(short: str, vx: float, vy: float):
    """带防撞检查的移动指令，抛 RuntimeError 表示被拦截"""
    now = time.time()
    with WARN_LOCK:
        locked = now < WARN_STATE["locked_until"]

    cur_dist = dist_between()
    min_d = predict_min_dist(vx, vy, short)

    if min_d < SAFE_DIST:
        # 驶离豁免：已在危险区内且预演显示正在远离 -> 放行
        escaping = cur_dist < SAFE_DIST and min_d > cur_dist + 1e-6
        if not escaping:
            trigger_collision_stop(
                f"{short} 的移动将使两车距离缩至 {min_d:.2f}m（< {SAFE_DIST}m）")
            raise RuntimeError(
                f"碰撞预警：两车最近距离将仅 {min_d:.2f}m，已双双停车，"
                f"{COOLDOWN_SEC} 秒后可重新操作")

    if locked:
        raise RuntimeError(f"碰撞冷却中（{WARN_STATE['locked_until'] - now:.0f}s），请稍后再试")

    # 通过检查，更新镜像并转发云端
    MIRRORS[short].set_move(vx, vy)
    send_command(CONFIG["devices"][short], "move",
                 {"vx": round(vx, 2), "vy": round(vy, 2)})


def try_stop(short: str):
    MIRRORS[short].set_stop()
    send_command(CONFIG["devices"][short], "stop", {})
    # 若两车都停了，解除预警显示
    with WARN_LOCK:
        if (MIRRORS["car_001"].speed < 0.001
                and MIRRORS["car_002"].speed < 0.001
                and time.time() >= WARN_STATE["locked_until"]):
            WARN_STATE["warning"] = False
            WARN_STATE["message"] = ""


# ---------------- 后台循环 ----------------
def closing_speed_mps() -> float:
    """两车接近速度（米/秒，>0 = 正在接近，<=0 = 远离或静止）"""
    a = MIRRORS["car_001"]
    b = MIRRORS["car_002"]
    with a.lock:
        ax, ay, avx, avy = a.x, a.y, a.vx, a.vy
    with b.lock:
        bx, by, bvx, bvy = b.x, b.y, b.vx, b.vy
    dx, dy = ax - bx, ay - by
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return 0.0
    rel_vx = (avx - bvx) * MAX_SPEED
    rel_vy = (avy - bvy) * MAX_SPEED
    # 间距变化率 d|a-b|/dt 的相反数：>0 = 正在接近
    return -(rel_vx * dx + rel_vy * dy) / d


def monitor_collision():
    """连续监测：两车接近且当前距离小于『安全距离+刹车余量』时立即双车停车。

    停车命令经云端到达车端有约 0.5~1.5s 延迟，所以提前量与接近速度成正比：
    相向行驶（接近 3 m/s）时约在 4~5 米外就触发，保证停住后仍有安全间隙。
    """
    d = dist_between()
    if d > 10.0:
        return
    closing = closing_speed_mps()
    if closing <= 0:
        return  # 正在远离或都没动
    brake_dist = SAFE_DIST + closing * 2.0
    if d < brake_dist:
        trigger_collision_stop(
            f"两车相距 {d:.2f}m 且以约 {closing:.1f} m/s 接近，自动双车停车")


def tick_loop():
    """20Hz 积分镜像位置 + 连续防撞监测 + 冷却解除"""
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
        # 冷却结束后解除锁定
        with WARN_LOCK:
            if (WARN_STATE["locked"]
                    and now >= WARN_STATE["locked_until"]
                    and MIRRORS["car_001"].speed < 0.001
                    and MIRRORS["car_002"].speed < 0.001):
                WARN_STATE["locked"] = False
                WARN_STATE["warning"] = False
                WARN_STATE["message"] = ""


def poll_loop():
    """影子校正 + 在线状态刷新"""
    while True:
        for short, dev in CONFIG["devices"].items():
            m = MIRRORS[short]
            m.online = (get_device_status(dev) == "ONLINE")
            if m.online:
                try:
                    m.sync_shadow(get_shadow(dev))
                except Exception as e:
                    print(f"[轮询] {short} 影子失败: {e}")
        time.sleep(CONFIG["poll_interval"])


# ---------------- 对 APP 的 HTTP 服务 ----------------
def resolve_dev(query: dict) -> str:
    name = (query.get("dev") or [""])[0]
    if name in CONFIG["devices"]:
        return name
    for short, full in CONFIG["devices"].items():
        if name == full:
            return short
    return name


class ApiHandler(BaseHTTPRequestHandler):
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
            if url.path == "/api/pair":
                with WARN_LOCK:
                    warn = dict(WARN_STATE)
                    warn.pop("locked_until", None)
                self._send(200, {
                    "cars": {k: v.snapshot() for k, v in MIRRORS.items()},
                    "distance": round(dist_between(), 2),
                    "safe_dist": SAFE_DIST,
                    **warn,
                })
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

        short = resolve_dev(query)
        if short not in CONFIG["devices"]:
            self._send(400, {"error": f"未知设备 {short}"})
            return
        try:
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
    threading.Thread(target=tick_loop, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG["http_port"]), ApiHandler)
    print(f"[HTTP] 双车防撞后端已就绪: http://0.0.0.0:{CONFIG['http_port']}")
    print(f"       /api/pair  /api/state?dev=car_001")
    print(f"       /api/move?dev=car_001  /api/stop?dev=car_001")
    print(f"[参数] 安全距离 {SAFE_DIST}m | 预演 {PREDICT_HORIZON}s | 冷却 {COOLDOWN_SEC}s")
    server.serve_forever()


if __name__ == "__main__":
    main()
