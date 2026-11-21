# -*- coding: utf-8 -*-
"""
巡检车车端 MQTT 客户端（第二阶段：华为云 IoTDA 接入）

替代第一阶段的 TCP 服务端：小车以 MQTT 设备身份接入华为云 IoTDA。

职责：
  1. MQTT 接入（8883/TLS）：设备身份认证上线
  2. 订阅云端命令（$oc/.../commands/#）：处理 move / stop，驱动小车状态机
  3. 上报属性（$oc/.../properties/report）：把 x/y/theta/speed/battery 报给平台
  4. 命令响应（$oc/.../commands/response/...）：向平台确认命令已执行
  5. HTTP 服务（8080）：本地俯视图（与第一阶段共用 topview.html，接口格式不变）

IoTDA 接入参数：
  clientId  = {device_id}_0_0_{timestamp}        timestamp 格式 YYYYMMDDHH（UTC）
  username  = {device_id}
  password  = HMAC-SHA256(secret, timestamp) 的十六进制

运行：python car_mqtt_client.py
"""

import os
import hashlib
import hmac
import json
import math
import ssl
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paho.mqtt.client as mqtt

# ---------------- 配置（按你的实例填写） ----------------
CONFIG = {
    "host": "YOUR_MQTT_HOST",
    "port": 8883,
    "device_id": "YOUR_PRODUCT_ID_car_001",
    "secret": os.environ.get("IOTDA_DEVICE_SECRET_001", "YOUR_DEVICE_SECRET"),
    "http_port": 8080,
}

REPORT_INTERVAL_MOVING = 0.5   # 行驶时属性上报间隔（秒）
REPORT_INTERVAL_IDLE = 5.0     # 静止时上报间隔（省消息量）
TICK_HZ = 20                   # 运动模拟频率
MAX_SPEED = 1.5                # 满摇杆最大速度（米/秒）
BATTERY_DRAIN_PER_SEC = 0.05

HTML_PATH = Path(__file__).with_name("topview.html")


# ---------------- 小车状态机（与第一阶段 CarSim 逻辑一致） ----------------
class CarSim:
    def __init__(self):
        self.lock = threading.Lock()
        self.x, self.y = 0.0, 0.0
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


CAR = CarSim()


# ---------------- MQTT 接入 ----------------
def iotda_credentials(device_id: str, secret: str):
    """生成 IoTDA 的 MQTT 三元组"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    client_id = f"{device_id}_0_0_{ts}"
    username = device_id
    password = hmac.new(ts.encode(), secret.encode(), hashlib.sha256).hexdigest()
    return client_id, username, password


CMD_TOPIC = f"$oc/devices/{CONFIG['device_id']}/sys/commands/#"
CMD_RESP_PREFIX = f"$oc/devices/{CONFIG['device_id']}/sys/commands/response/request_id="
REPORT_TOPIC = f"$oc/devices/{CONFIG['device_id']}/sys/properties/report"

mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id="pending", clean_session=True)
connected = threading.Event()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("[MQTT] 设备上线成功！")
        client.subscribe(CMD_TOPIC, qos=1)
        print(f"[MQTT] 已订阅命令主题: {CMD_TOPIC}")
        connected.set()
    else:
        print(f"[MQTT] 连接失败: {reason_code}")


def on_disconnect(client, userdata, flags, reason_code, properties):
    connected.clear()
    print(f"[MQTT] 连接断开: {reason_code}（自动重连中）")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return
    # 主题形如 $oc/devices/{id}/sys/commands/request_id={rid}
    rid = ""
    if "request_id=" in msg.topic:
        rid = msg.topic.split("request_id=", 1)[1]
    name = payload.get("command_name")
    paras = payload.get("paras") or {}
    if name == "move":
        CAR.on_move(float(paras.get("vx", 0)), float(paras.get("vy", 0)))
        print(f"[命令] move vx={paras.get('vx')} vy={paras.get('vy')}")
    elif name == "stop":
        CAR.on_stop()
        print("[命令] stop")
    else:
        print(f"[命令] 未识别: {name}")
        return
    # 命令响应
    if rid:
        client.publish(CMD_RESP_PREFIX + rid,
                       json.dumps({"result_code": 0}), qos=1)


def report_properties(client):
    """上报一次属性（service_id: car_status）"""
    snap = CAR.snapshot()
    payload = {"services": [{"service_id": "car_status", "properties": snap}]}
    client.publish(REPORT_TOPIC, json.dumps(payload), qos=1)


# ---------------- 本地 HTTP（俯视图，与第一阶段格式一致） ----------------
last_state = {"type": "state", "x": 0.0, "y": 0.0,
              "theta": 0.0, "speed": 0.0, "battery": 100.0}


def state_refresher():
    """20Hz 运动积分 + 刷新本地俯视图状态"""
    global last_state
    last_t = time.time()
    while True:
        time.sleep(1.0 / TICK_HZ)
        now = time.time()
        CAR.tick(now - last_t)
        last_t = now
        last_state = {"type": "state", **CAR.snapshot()}


class HttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/state"):
            body = json.dumps(last_state).encode()
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
    threading.Thread(target=state_refresher, daemon=True).start()
    threading.Thread(
        target=lambda: ThreadingHTTPServer(
            ("0.0.0.0", CONFIG["http_port"]), HttpHandler).serve_forever(),
        daemon=True).start()
    print(f"[HTTP] 俯视图: http://localhost:{CONFIG['http_port']}")

    cid, user, pwd = iotda_credentials(CONFIG["device_id"], CONFIG["secret"])
    print(f"[MQTT] 连接 {CONFIG['host']}:{CONFIG['port']} 设备 {CONFIG['device_id']}")

    global mqttc
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id=cid, clean_session=True)
    mqttc.username_pw_set(user, pwd)
    try:
        ctx = ssl.create_default_context()
        mqttc.tls_set_context(ctx)
        print("[MQTT] TLS: 使用系统 CA 校验")
    except Exception as e:
        print(f"[MQTT] TLS 配置降级: {e}")
        mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
        mqttc.tls_insecure_set(True)

    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect
    mqttc.on_message = on_message
    mqttc.reconnect_delay_set(min_delay=1, max_delay=30)
    mqttc.connect(CONFIG["host"], CONFIG["port"], keepalive=60)
    mqttc.loop_start()

    # 属性上报循环：行驶中 2Hz，静止 0.2Hz（省消息量）
    last_report = 0.0
    last_snap = None
    while True:
        time.sleep(0.1)
        if not connected.is_set():
            continue
        snap = CAR.snapshot()
        moving = snap["speed"] > 0.01
        interval = REPORT_INTERVAL_MOVING if moving else REPORT_INTERVAL_IDLE
        changed = snap != last_snap
        now = time.time()
        if now - last_report >= interval or (changed and moving):
            report_properties(mqttc)
            last_report = now
            last_snap = snap


if __name__ == "__main__":
    main()
