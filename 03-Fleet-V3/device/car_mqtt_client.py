# -*- coding: utf-8 -*-
"""
巡检车车端 MQTT 客户端（V3 车队版 · 设备模拟器）

从 V2 验证过的 car_mqtt_client_001/002.py 收编改造为一台通用脚本：
  python car_mqtt_client.py --device car_001
  python car_mqtt_client.py --device car_002

与 V2 的差异（其余逻辑保持一致）：
  1. 设备身份（完整 device_id / 密钥 / 出生点）从 server/devices.json 读取，
     不再写死在代码里 —— 新增模拟车只需在 devices.json 登记
  2. MQTT 接入地址从 server/cloud_config.json 读取
  3. 属性上报节奏按路线图硬约束：行驶 5 秒 / 空闲 60 秒（IoTDA S0 免费单元
     每日约 1 万条消息，多车环境下不可调快）

职责（与 V2 相同）：
  1. MQTT 接入（8883/TLS）：设备身份认证上线
  2. 订阅云端命令：处理 move / stop，驱动小车状态机
  3. 上报属性（service_id: car_status）：x/y/theta/speed/battery
  4. 命令响应：向平台确认命令已执行
  5. HTTP 服务（本地调试）：/state 返回本车实时状态 JSON
"""

import argparse
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

# ---------------- 路径与节奏 ----------------
ROOT = Path(__file__).resolve().parent.parent          # v3-fleet/
DEVICES_JSON = ROOT / "server" / "devices.json"
CLOUD_CONFIG_JSON = ROOT / "server" / "cloud_config.json"

REPORT_INTERVAL_MOVING = 5.0    # 行驶时属性上报间隔（路线图硬约束，勿调快）
REPORT_INTERVAL_IDLE = 60.0     # 静止时上报间隔（省消息量）
EVENT_REPORT_GAP = 2.0          # 命令事件上报最小间隔（秒）：命令执行后尽快补报一次，
                                # 但限流防 joystick 连发把消息配额打爆
TICK_HZ = 20                    # 运动模拟频率
MAX_SPEED = 1.5                 # 满摇杆最大速度（米/秒）
BATTERY_DRAIN_PER_SEC = 0.05

# 与物模型一致：move/stop 命令挂在 car_control 服务下（属性上报在 car_status）
SERVICE_ID_CONTROL = "car_control"
SERVICE_ID_STATUS = "car_status"


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------- 小车状态机（与 V2 CarSim 逻辑一致） ----------------
class CarSim:
    def __init__(self, spawn_x: float, spawn_y: float):
        self.lock = threading.Lock()
        self.x, self.y = spawn_x, spawn_y
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


CAR: CarSim = None          # main() 里初始化
HTTP_PORT = 0               # main() 里初始化
last_report = 0.0           # 上次属性上报时刻（模块级，供命令回调与上报循环共享）
CMD_DIRTY = threading.Event()   # 有命令执行 → 上报循环尽快补报一次
last_state = {"type": "state", "device_id": "", "x": 0.0, "y": 0.0,
              "theta": 0.0, "speed": 0.0, "battery": 100.0}


def state_refresher():
    """20Hz 运动积分 + 刷新本地调试状态"""
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
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


# ---------------- MQTT 接入（与 V2 完全一致的协议） ----------------
def iotda_credentials(device_id: str, secret: str):
    """生成 IoTDA 的 MQTT 三元组"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    client_id = f"{device_id}_0_0_{ts}"
    username = device_id
    password = hmac.new(ts.encode(), secret.encode(), hashlib.sha256).hexdigest()
    return client_id, username, password


mqttc = None
connected = threading.Event()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("[MQTT] 设备上线成功！")
        client.subscribe(f"$oc/devices/{DEV['device_id']}/sys/commands/#", qos=1)
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
    CMD_DIRTY.set()   # 命令已改变运动状态 → 触发事件上报（上报循环限流执行）
    if rid:
        client.publish(
            f"$oc/devices/{DEV['device_id']}/sys/commands/response/request_id={rid}",
            json.dumps({"result_code": 0}), qos=1)


def report_properties(client):
    """上报一次属性（service_id: car_status）"""
    snap = CAR.snapshot()
    payload = {"services": [{"service_id": SERVICE_ID_STATUS, "properties": snap}]}
    client.publish(
        f"$oc/devices/{DEV['device_id']}/sys/properties/report",
        json.dumps(payload), qos=1)


def resolve_device(devices: dict, name_or_id: str) -> dict:
    """按短名（node_id）或完整 device_id 在注册表（devices 列表）中查找"""
    for dev in devices.get("devices", []):
        if name_or_id in (dev["device_id"], dev["device_id"].split("_", 1)[-1]):
            return dev
    return None


def main():
    global CAR, HTTP_PORT, mqttc, last_state, DEV, last_report

    ap = argparse.ArgumentParser(description="V3 巡检车设备模拟器")
    ap.add_argument("--device", required=True,
                    help="设备短名或完整 device_id，须已在 server/devices.json 登记，如 car_001")
    args = ap.parse_args()

    registry = load_json(DEVICES_JSON)
    DEV = resolve_device(registry, args.device)
    if DEV is None:
        known = [d["device_id"] for d in registry.get("devices", [])]
        print(f"[错误] {args.device} 未在 {DEVICES_JSON} 登记")
        print(f"       已登记: {', '.join(known)}")
        raise SystemExit(1)
    HTTP_PORT = DEV.get("http_port", 0)
    cloud_cfg = load_json(CLOUD_CONFIG_JSON)

    CAR = CarSim(DEV["spawn"]["x"], DEV["spawn"]["y"])
    last_state["device_id"] = DEV["device_id"]

    threading.Thread(target=state_refresher, daemon=True).start()
    if HTTP_PORT:
        # 防呆：调试端口已被占用 = 已有同车实例在跑（MQTT 会互踢），立即退出
        import socket
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("0.0.0.0", HTTP_PORT))
        except OSError:
            print(f"[错误] 端口 {HTTP_PORT} 已被占用 —— {args.device} 很可能已有实例在运行！")
            print("       请先用 taskkill 清理旧进程，勿重复启动（MQTT 同设备互踢会乱套）")
            probe.close()
            raise SystemExit(1)
        probe.close()
        threading.Thread(
            target=lambda: ThreadingHTTPServer(
                ("0.0.0.0", HTTP_PORT), HttpHandler).serve_forever(),
            daemon=True).start()
        print(f"[HTTP] 本车调试状态: http://localhost:{HTTP_PORT}/state")

    full_id = DEV["device_id"]
    cid, user, pwd = iotda_credentials(full_id, DEV["secret"])
    print(f"[MQTT] 连接 {cloud_cfg['mqtt_host']}:{cloud_cfg['mqtt_port']} "
          f"设备 {full_id}（{DEV['name']}）")
    print(f"[MQTT] 上报节奏: 行驶 {REPORT_INTERVAL_MOVING:.0f}s / 空闲 {REPORT_INTERVAL_IDLE:.0f}s")

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
    mqttc.connect(cloud_cfg["mqtt_host"], cloud_cfg["mqtt_port"], keepalive=60)
    mqttc.loop_start()

    # 属性上报循环：行驶 5s / 空闲 60s（路线图硬约束）；
    # 有命令执行（CMD_DIRTY）时按 EVENT_REPORT_GAP 限流尽快补报，让云端尽快看到新状态
    last_report = 0.0
    while True:
        time.sleep(0.1)
        if not connected.is_set():
            continue
        snap = CAR.snapshot()
        moving = snap["speed"] > 0.01
        interval = REPORT_INTERVAL_MOVING if moving else REPORT_INTERVAL_IDLE
        now = time.time()
        due = now - last_report >= interval
        event = CMD_DIRTY.is_set() and now - last_report >= EVENT_REPORT_GAP
        if due or event:
            report_properties(mqttc)
            CMD_DIRTY.clear()
            last_report = now


if __name__ == "__main__":
    main()
