# -*- coding: utf-8 -*-
"""
多车模拟底座：单进程模拟 N 辆巡检车（第四阶段）

在 car_mqtt_client_001.py 单车版基础上升级：
  1. 一进程多车：每辆车独立 MQTT 连接 + 独立运动学状态（线程安全）
  2. 自动注册：云端不存在的设备自动通过 IoTDA API 创建（用 AK/SK）
     —— "批量加车" 只需改一个数字：python car_simulator.py --count 6
  3. 共享俯视图：一个 HTTP 端口(8085)同屏显示所有车，消灭"每车一个端口"

运行：python car_simulator.py            （默认按 CONFIG["count"] 起车）
      python car_simulator.py --count 6  （临时起 6 辆）
"""

import os
import argparse
import hashlib
import hmac
import json
import math
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paho.mqtt.client as mqtt

# ---------------- 配置 ----------------
CONFIG = {
    "host": "YOUR_MQTT_HOST",
    "port": 8883,
    # 设备ID前缀（与控制台已有设备一致：{app_id}_car_001）
    "prefix": "YOUR_PRODUCT_ID",
    # 模拟车辆数量：car_001 ~ car_00N
    "count": 4,
    # 已有设备的密钥（新注册的车默认用 default）
    "secrets": {
        "car_001": os.environ.get("IOTDA_DEVICE_SECRET_001", "YOUR_DEVICE_SECRET"),
        "car_002": os.environ.get("IOTDA_DEVICE_SECRET_002", "YOUR_DEVICE_SECRET"),
    },
    "default_secret": os.environ.get("IOTDA_DEVICE_SECRET", "YOUR_DEVICE_SECRET"),
    "http_port": 8085,     # 车队共享俯视图端口
}

REPORT_INTERVAL_MOVING = 0.5   # 行驶时属性上报间隔（秒）
REPORT_INTERVAL_IDLE = 5.0     # 静止时上报间隔
TICK_HZ = 20
MAX_SPEED = 1.5
BATTERY_DRAIN_PER_SEC = 0.05

HTML_PATH = Path(__file__).with_name("topview_multi.html")

# ---------------- 小车状态机（与单车版一致） ----------------
class CarSim:
    def __init__(self, x: float = 0.0, y: float = 0.0):
        self.lock = threading.Lock()
        self.x, self.y = x, y
        self.theta = 0.0
        self.vx, self.vy = 0.0, 0.0
        self.battery = 100.0

    def on_move(self, vx: float, vy: float) -> None:
        mag = math.hypot(vx, vy)
        if mag > 1.0:
            vx, vy = vx / mag, vy / mag
        with self.lock:
            self.vx, self.vy = vx, vy

    def on_stop(self) -> None:
        with self.lock:
            self.vx, self.vy = 0.0, 0.0

    def tick(self, dt: float) -> None:
        with self.lock:
            speed = math.hypot(self.vx, self.vy) * MAX_SPEED
            if speed > 0.01 and self.battery > 0:
                self.x += self.vx * MAX_SPEED * dt
                self.y += self.vy * MAX_SPEED * dt
                self.theta = math.degrees(math.atan2(self.vx, self.vy)) % 360
                self.battery = max(0.0, self.battery - BATTERY_DRAIN_PER_SEC * dt)
            else:
                self.vx, self.vy = 0.0, 0.0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "x": round(self.x, 3),
                "y": round(self.y, 3),
                "theta": round(self.theta, 1),
                "speed": round(math.hypot(self.vx, self.vy) * MAX_SPEED, 3),
                "battery": round(self.battery, 1),
            }


def iotda_credentials(device_id: str, secret: str):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    return (f"{device_id}_0_0_{ts}", device_id,
            hmac.new(ts.encode(), secret.encode(), hashlib.sha256).hexdigest())


# ---------------- 单辆车的 MQTT 生命周期（跑在自己的线程里） ----------------
def run_one_car(node: str, secret: str, car: CarSim):
    device_id = f"{CONFIG['prefix']}_{node}"
    cmd_topic = f"$oc/devices/{device_id}/sys/commands/#"
    resp_prefix = f"$oc/devices/{device_id}/sys/commands/response/request_id="
    report_topic = f"$oc/devices/{device_id}/sys/properties/report"
    connected = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            print(f"[MQTT] {node} 上线成功")
            client.subscribe(cmd_topic, qos=1)
            connected.set()
        else:
            print(f"[MQTT] {node} 连接失败: {reason_code}")

    def on_disconnect(client, userdata, flags, reason_code, properties):
        connected.clear()
        print(f"[MQTT] {node} 断线（自动重连中）")

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        rid = ""
        if "request_id=" in msg.topic:
            rid = msg.topic.split("request_id=", 1)[1]
        name = payload.get("command_name")
        paras = payload.get("paras") or {}
        if name == "move":
            car.on_move(float(paras.get("vx", 0)), float(paras.get("vy", 0)))
            print(f"[命令] {node} move vx={paras.get('vx')} vy={paras.get('vy')}")
        elif name == "stop":
            car.on_stop()
            print(f"[命令] {node} stop")
        else:
            return
        if rid:
            client.publish(resp_prefix + rid,
                           json.dumps({"result_code": 0}), qos=1)

    cid, user, pwd = iotda_credentials(device_id, secret)
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=cid, clean_session=True)
    c.username_pw_set(user, pwd)
    try:
        ctx = ssl.create_default_context()
        c.tls_set_context(ctx)
    except Exception:
        c.tls_set(cert_reqs=ssl.CERT_NONE)
        c.tls_insecure_set(True)
    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.on_message = on_message
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    c.connect(CONFIG["host"], CONFIG["port"], keepalive=60)
    c.loop_start()

    # 属性上报循环（每车独立）
    last_report = 0.0
    last_snap = None
    while True:
        time.sleep(0.1)
        if not connected.is_set():
            continue
        snap = car.snapshot()
        moving = snap["speed"] > 0.01
        interval = REPORT_INTERVAL_MOVING if moving else REPORT_INTERVAL_IDLE
        now = time.time()
        if now - last_report >= interval or (snap != last_snap and moving):
            payload = {"services": [{"service_id": "car_status",
                                     "properties": snap}]}
            c.publish(report_topic, json.dumps(payload), qos=1)
            last_report = now
            last_snap = snap


# ---------------- 自动注册：云端没有的车用 API 创建 ----------------
def provision_missing(wanted_nodes):
    """用 IoTDA 应用侧 API 创建缺失设备；返回 (成功的注册数, 产品ID)"""
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        import cloud_backend as backend
        backend.build_client()
        client = backend.client
        if client is None:
            raise RuntimeError("IoTDAClient 未初始化")
    except Exception as e:
        print(f"[注册] 跳过（SDK/账号不可用: {e}），将只连接已存在的设备")
        return 0, None

    from huaweicloudsdkiotda.v5 import ListDevicesRequest, AddDeviceRequest
    from huaweicloudsdkiotda.v5.model.add_device import AddDevice
    from huaweicloudsdkiotda.v5.model.auth_info import AuthInfo

    try:
        req = ListDevicesRequest()
        req.limit = 50
        resp = client.list_devices(req)
    except Exception as e:
        print(f"[注册] 设备列表查询失败: {e}")
        return 0, None

    existing = {}
    product_id = None
    app_id = None
    for d in (resp.devices or []):
        existing[d.device_id] = d
        if d.device_id == f"{CONFIG['prefix']}_car_001":
            product_id = d.product_id
            app_id = d.app_id
    if product_id is None:
        print("[注册] 未找到 car_001，无法确定产品ID，跳过注册")
        return 0, None

    created = 0
    for node in wanted_nodes:
        device_id = f"{CONFIG['prefix']}_{node}"
        if device_id in existing:
            continue
        try:
            req = AddDeviceRequest()
            req.body = AddDevice(
                device_id=device_id,
                node_id=node,
                device_name=f"巡检车{node}",
                product_id=product_id,
                app_id=app_id,
                auth_info=AuthInfo(secret=CONFIG["default_secret"],
                                   secure_access=True),
            )
            client.add_device(req)
            created += 1
            print(f"[注册] 已在云端创建 {node}（密钥 {CONFIG['default_secret']}）")
        except Exception as e:
            print(f"[注册] 创建 {node} 失败: {e}")
    return created, product_id


# ---------------- 20Hz 全局运动积分 ----------------
def tick_loop(cars):
    last_t = time.time()
    while True:
        time.sleep(1.0 / TICK_HZ)
        now = time.time()
        dt = now - last_t
        last_t = now
        for car in cars.values():
            car.tick(dt)


# ---------------- 车队共享俯视图 HTTP ----------------
class FleetHttp(BaseHTTPRequestHandler):
    def handle(self):
        # 客户端主动断开（关页面）是正常现象，静默处理不刷屏
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError, TimeoutError):
            pass

    cars: dict = {}

    def do_GET(self):
        if self.path.startswith("/api/cars"):
            body = json.dumps({
                "ts": time.time(),
                "cars": {n: c.snapshot() for n, c in self.cars.items()},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html"):
            body = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=CONFIG["count"],
                    help="模拟车辆数量")
    args = ap.parse_args()

    nodes = [f"car_{i:03d}" for i in range(1, args.count + 1)]
    print(f"{'=' * 50}\n 多车模拟底座：{len(nodes)} 辆车 "
          f"({nodes[0]} ~ {nodes[-1]})\n{'=' * 50}")

    created, _ = provision_missing(nodes)

    def spawn_pos(i: int):
        """出生点：2米网格排开，避免多车堆在原点触发防撞"""
        return (-2.0 * ((i - 1) % 2), -2.0 * ((i - 1) // 2))

    cars = {n: CarSim(*spawn_pos(i)) for i, n in enumerate(nodes, 1)}
    FleetHttp.cars = cars
    threading.Thread(target=tick_loop, args=(cars,), daemon=True).start()
    threading.Thread(
        target=lambda: ThreadingHTTPServer(
            ("0.0.0.0", CONFIG["http_port"]), FleetHttp).serve_forever(),
        daemon=True).start()
    print(f"[HTTP] 车队俯视图: http://localhost:{CONFIG['http_port']} "
          f"（同屏 {len(nodes)} 辆车）")
    if created:
        print(f"[注册] 本次新注册 {created} 辆（新设备密钥统一为 "
              f"{CONFIG['default_secret']}）")

    threads = []
    for n in nodes:
        secret = CONFIG["secrets"].get(n, CONFIG["default_secret"])
        t = threading.Thread(target=run_one_car, args=(n, secret, cars[n]),
                             daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.3)  # 错峰连接，避免同时握手上限

    print("[系统] 全部车辆线程已启动，Ctrl+C 退出")
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
