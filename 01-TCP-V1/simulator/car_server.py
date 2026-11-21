# -*- coding: utf-8 -*-
"""
模拟小车服务端（无真实小车时的第一阶段方案）

职责：
  1. TCP 服务端（端口 9000）：常驻监听，扮演"小车"，接收鸿蒙 APP 的控制指令
  2. 小车状态模拟：根据 move 指令更新速度，按 20Hz 积分位置，模拟电量消耗
  3. HTTP 服务（端口 8080）：为浏览器俯视图提供 /state 状态接口和俯视图页面

协议（与鸿蒙端 Protocol.ets 一致，每条消息一行 JSON）：
  收: {"type":"cmd","action":"move","vx":0.5,"vy":-1.0} / {"type":"cmd","action":"stop"} / {"type":"ping"}
  发: {"type":"pong"} / {"type":"state","x":..,"y":..,"theta":..,"speed":..,"battery":..}

运行：python car_server.py
"""

import json
import math
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TCP_PORT = 9000
HTTP_PORT = 8082
TICK_HZ = 20                      # 模拟频率
MAX_SPEED = 1.5                   # 满摇杆最大速度（米/秒）
BATTERY_DRAIN_PER_SEC = 0.05      # 移动时每秒耗电百分比

HTML_PATH = Path(__file__).with_name("topview.html")


class CarSim:
    """小车状态机：接收指令 → 更新速度 → 定时积分位置"""

    def __init__(self):
        self.lock = threading.Lock()
        self.x, self.y = 0.0, 0.0
        self.theta = 0.0            # 朝向角（度，0 = 正北，顺时针为正）
        self.vx, self.vy = 0.0, 0.0  # 当前摇杆矢量
        self.battery = 100.0

    def on_cmd(self, msg: dict) -> None:
        if msg.get("type") != "cmd":
            return
        with self.lock:
            if msg.get("action") == "move":
                vx = float(msg.get("vx", 0))
                vy = float(msg.get("vy", 0))
                # 摇杆矢量限幅（防止对角线超速）
                mag = math.hypot(vx, vy)
                if mag > 1.0:
                    vx, vy = vx / mag, vy / mag
                self.vx, self.vy = vx, vy
            elif msg.get("action") == "stop":
                self.vx, self.vy = 0.0, 0.0

    def tick(self, dt: float) -> None:
        with self.lock:
            speed = math.hypot(self.vx, self.vy) * MAX_SPEED
            if speed > 0.01 and self.battery > 0:
                # 位置积分：摇杆方向即小车运动方向
                self.x += self.vx * MAX_SPEED * dt
                self.y += self.vy * MAX_SPEED * dt
                # 朝向取运动方向（y 轴向上为北）
                self.theta = math.degrees(math.atan2(self.vx, self.vy)) % 360
                self.battery = max(0.0, self.battery - BATTERY_DRAIN_PER_SEC * dt)
            else:
                self.vx, self.vy = 0.0, 0.0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "type": "state",
                "x": round(self.x, 3),
                "y": round(self.y, 3),
                "theta": round(self.theta, 1),
                "speed": round(math.hypot(self.vx, self.vy) * MAX_SPEED, 3),
                "battery": round(self.battery, 1),
            }


CAR = CarSim()


# ---------------- TCP：扮演小车，监听 APP 连接 ----------------

TCP_CLIENTS = set()          # 所有已连接的 TCP 客户端（用于状态广播）
CLIENTS_LOCK = threading.Lock()


class CarTcpHandler(socketserver.StreamRequestHandler):
    def handle(self):
        addr = self.client_address
        print(f"[TCP] APP 已连接: {addr[0]}:{addr[1]}")
        with CLIENTS_LOCK:
            TCP_CLIENTS.add(self.request)
        buf = b""
        try:
            while True:
                data = self.request.recv(1024)
                if not data:
                    break
                buf += data
                # 按换行分帧，处理粘包/拆包
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if msg.get("type") == "ping":
                        self.request.sendall(b'{"type":"pong"}\n')
                    else:
                        CAR.on_cmd(msg)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            with CLIENTS_LOCK:
                TCP_CLIENTS.discard(self.request)
            print(f"[TCP] APP 断开: {addr[0]}:{addr[1]}")
            CAR.on_cmd({"type": "cmd", "action": "stop"})  # 连接断开立即停车（安全措施）


def broadcast_states():
    """每 200ms 向所有 TCP 客户端广播一次小车状态（5Hz，控制场景足够流畅）"""
    interval = 0.2
    while True:
        payload = (json.dumps(CAR.snapshot()) + "\n").encode("utf-8")
        with CLIENTS_LOCK:
            dead = []
            for sock in TCP_CLIENTS:
                try:
                    sock.sendall(payload)
                except OSError:
                    dead.append(sock)
            for sock in dead:
                TCP_CLIENTS.discard(sock)
        time.sleep(interval)


def start_tcp_server():
    srv = socketserver.ThreadingTCPServer(("0.0.0.0", TCP_PORT), CarTcpHandler)
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=broadcast_states, daemon=True).start()
    print(f"[TCP] 模拟小车已上线，监听端口 {TCP_PORT}（等待鸿蒙 APP 连接，状态广播 5Hz）")


# ---------------- HTTP：为浏览器俯视图提供状态与页面 ----------------

class TopViewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/state":
            body = json.dumps(CAR.snapshot()).encode("utf-8")
            self._send(200, "application/json", body)
        elif self.path in ("/", "/index.html"):
            body = HTML_PATH.read_bytes()
            self._send(200, "text/html; charset=utf-8", body)
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 屏蔽 HTTP 访问日志刷屏


def start_http_server():
    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), TopViewHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[HTTP] 俯视图已就绪，浏览器访问 http://localhost:{HTTP_PORT}")


# ---------------- 模拟主循环 ----------------

def start_simulation():
    def loop():
        dt = 1.0 / TICK_HZ
        while True:
            CAR.tick(dt)
            time.sleep(dt)
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    print("=" * 50)
    print(" 智能巡检车 · 模拟小车服务端")
    print("=" * 50)
    start_simulation()
    start_http_server()
    start_tcp_server()
    print("按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n服务端已退出")
