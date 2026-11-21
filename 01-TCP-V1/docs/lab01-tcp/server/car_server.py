#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab01-TCP · 单元 4：双服务完整版 —— TCP(8888) + WebSocket(8889) 并存
====================================================================
单元 3 遗留的两个问题，本单元一次解决：
  ① "APP 拿到 STATE 只是一堆数字，车长什么样看不见"
  ② "浏览器连不了裸 TCP"（浏览器出于安全只说 HTTP/WebSocket）

解法：车端同时开两个"窗口"——
  APP --TCP:8888----------> car_server --WebSocket:8889(JSON)---> 浏览器俯视图
  （控制端，发指令收状态）                （观察端，只看不控）

本单元的升级点（相对 step3）：
  1. 叠加 websockets.serve 起第二个服务：协议从"一行文本"换成"一帧 JSON"
     —— 同一份运动状态，两种编码、两个端口，同时服务两类客户端。
  2. tick_loop 一个循环喂两张嘴：TCP 发文本 STATE，WebSocket 发 JSON。
  3. 优雅降级：没装 websockets 库也能跑——TCP 功能不受伤，只提示俯视图不可用。

TCP 协议（文本帧，\\n 结尾）—— 与 step3 完全一致，一个字没改：
  上行: PING / CMD:FORWARD[:speed] / CMD:BACK[:speed] / CMD:LEFT / CMD:RIGHT
        / CMD:STOP / CMD:STATUS
  下行: ACK:xxx / PONG / ERR:xxx / STATE:x=..,y=..,heading=..,speed=..,battery=..
WebSocket 协议（JSON 文本，每 100ms 一帧）：
  {"x":..,"y":..,"heading":..,"speed":..,"battery":..}

坐标系约定不变：场地 800×600；(0,0) 左上角；heading 0°=朝上、顺时针增大。

运行：python car_server.py [TCP端口]   （默认 8888，WebSocket 固定 8889）
依赖：websockets 库（pip install websockets）；TCP 部分仍是纯标准库
"""
import asyncio
import json
import math
import socket
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import websockets
except ImportError:
    websockets = None      # 优雅降级：缺库时 TCP 照常跑，只提示俯视图不可用

HOST = "0.0.0.0"
DEFAULT_PORT = 8888
WS_PORT = 8889           # WebSocket 固定端口：网页里写死了，改两端要一起改
TICK_MS = 100
TURN_STEP = 15
DEFAULT_SPEED = 60
FIELD_W, FIELD_H = 800, 600


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def local_ip():
    """探测本机局域网 IP（不真正发包）——启动横幅里提示给 APP/网页连哪个地址"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))     # UDP 不实际发包，只是让系统选路由
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- 运动模型
class Car:
    """小车状态：位置、朝向（0°=朝上，顺时针增）、带符号速度、电量
    （单元 3 原封不动搬过来——这就是"封版"的含义：运动层不再动）"""

    def __init__(self):
        self.x = FIELD_W / 2          # 出生点：场地正中央
        self.y = FIELD_H / 2
        self.heading = 0              # 0° = 朝上
        self.speed = 0                # +前进 / -后退 / 0停止
        self.battery = 87.0

    def step(self):
        """每个 tick 推进一次运动模型"""
        if self.speed == 0:
            return
        rad = math.radians(self.heading)
        self.x += self.speed * 0.02 * math.sin(rad)
        self.y -= self.speed * 0.02 * math.cos(rad)
        self.x = min(max(self.x, 10), FIELD_W - 10)
        self.y = min(max(self.y, 10), FIELD_H - 10)
        self.battery = max(20.0, self.battery - 0.01)

    def state_frame(self):
        """运动状态 -> 一行 STATE 文本帧（给 TCP 客户端：APP / test_client.py）"""
        return (f"STATE:x={self.x:.0f},y={self.y:.0f},"
                f"heading={self.heading % 360},speed={self.speed},"
                f"battery={self.battery:.0f}")


car = Car()
clients = set()       # 在线 TCP 客户端（APP 端 / 测试客户端）
ws_clients = set()    # 在线 WebSocket 客户端（浏览器俯视图页面）——单元 4 新增


# ---------------------------------------------------------------- 协议解析
def handle_command(text):
    """解析一行指令，返回应答帧列表（与 step3 逐字一致——协议契约不动）"""
    if text == "PING":
        return ["PONG"]
    if not text.startswith("CMD:"):
        return [f"ERR:BAD_FRAME:{text}"]

    parts = text.split(":")
    action = parts[1].strip().upper()

    if action in ("FORWARD", "BACK"):
        speed = DEFAULT_SPEED
        if len(parts) >= 3:
            try:
                speed = max(0, min(100, int(parts[2])))
            except ValueError:
                return [f"ERR:BAD_SPEED:{parts[2]}"]
        car.speed = speed if action == "FORWARD" else -speed
        return [f"ACK:{action}:{abs(car.speed)}"]
    if action == "LEFT":
        car.heading = (car.heading - TURN_STEP) % 360
        return [f"ACK:LEFT:{car.heading}"]
    if action == "RIGHT":
        car.heading = (car.heading + TURN_STEP) % 360
        return [f"ACK:RIGHT:{car.heading}"]
    if action == "STOP":
        car.speed = 0
        return ["ACK:STOP"]
    if action == "STATUS":
        return [car.state_frame()]
    return [f"ERR:UNKNOWN_CMD:{action}"]


# ---------------------------------------------------------------- TCP 连接处理
async def client_handler(reader, writer):
    addr = writer.get_extra_info("peername")
    log(f"[连接] TCP 客户端 {addr[0]}:{addr[1]}")
    clients.add(writer)
    try:
        # 接入即推一帧当前状态：APP 一连上画面就有数据
        writer.write((car.state_frame() + "\n").encode("utf-8"))
        await writer.drain()
        while True:
            line = await reader.readline()
            if not line:
                log(f"[断开] {addr[0]}:{addr[1]} 正常退出")
                break
            text = line.decode("utf-8").strip()
            if not text:
                continue
            log(f"[<-APP] {text}   来自 {addr[0]}:{addr[1]}")
            for reply in handle_command(text):
                log(f"[->APP] {reply}")
                writer.write((reply + "\n").encode("utf-8"))
            await writer.drain()
    except (ConnectionError, UnicodeDecodeError) as e:
        log(f"[断开] {addr[0]}:{addr[1]} 异常: {e}")
    finally:
        clients.discard(writer)
        writer.close()


# ---------------------------------------------------------------- WebSocket（单元 4 新增）
def state_json():
    """同一份小车状态 -> JSON 字符串（给浏览器页面）
    注意：两种编码共享同一个 car 对象——数据一份，表示两种"""
    return json.dumps({
        "x": round(car.x, 1),
        "y": round(car.y, 1),
        "heading": car.heading % 360,
        "speed": car.speed,
        "battery": round(car.battery, 1),
    })


async def ws_handler(ws):
    """浏览器俯视图接入。浏览器是"只看不控"的观察端：
    只负责推状态给它，它发来的任何消息都忽略——控制权牢牢在 APP 手里"""
    peer = ws.remote_address
    log(f"[连接] 浏览器俯视图接入 ws://{peer[0]}:{peer[1]}")
    ws_clients.add(ws)
    try:
        await ws.send(state_json())     # 接入即推一帧，页面立刻有画面
        async for _ in ws:              # 挂着收（忽略客户端消息），断了循环自然退出
            pass
    except websockets.ConnectionClosed:
        pass
    finally:
        ws_clients.discard(ws)
        log(f"[断开] 浏览器俯视图离开 {peer[0]}:{peer[1]}")


# ---------------------------------------------------------------- 广播循环
async def tick_loop():
    """每 100ms：推进运动模型，然后"一个循环喂两张嘴"——
    TCP 客户端发文本 STATE，WebSocket 客户端发 JSON"""
    while True:
        await asyncio.sleep(TICK_MS / 1000)
        car.step()

        # ① TCP 文本帧广播
        frame = (car.state_frame() + "\n").encode("utf-8")
        dead = []
        for w in list(clients):
            try:
                w.write(frame)
                await w.drain()
            except (ConnectionError, RuntimeError):
                dead.append(w)
        for w in dead:
            clients.discard(w)

        # ② WebSocket JSON 广播（没人看就不造帧，省一点是一点）
        if ws_clients:
            payload = state_json()
            dead_ws = []
            for ws in list(ws_clients):
                try:
                    await ws.send(payload)
                except websockets.ConnectionClosed:
                    dead_ws.append(ws)
            for ws in dead_ws:
                ws_clients.discard(ws)


# ---------------------------------------------------------------- 启动
async def main():
    port = DEFAULT_PORT
    if len(sys.argv) >= 2 and sys.argv[1].isdigit():
        port = int(sys.argv[1])

    ip = local_ip()
    print("=" * 46)
    print(" Lab01 模拟车服务端（单元 4：TCP + WebSocket 双服务）")
    print(f" 本机局域网 IP : {ip}")
    print(f" TCP 监听      : {HOST}:{port}   <- 鸿蒙 APP（控制端）")
    if websockets is not None:
        print(f" WebSocket 监听: {HOST}:{WS_PORT}   <- 浏览器俯视图（只看不控）")
        print(" 俯视图页面    : 浏览器打开 web/car_view.html 即可看到小车")
    else:
        print(" WebSocket     : 未安装 websockets 库，俯视图不可用")
        print("               : pip install websockets 后重启本脚本")
    print(f" 自测命令      : python test_client.py {ip} --auto")
    print(" 防火墙提示    : 首次运行请允许 Python 访问网络")
    print("=" * 46, flush=True)

    server = await asyncio.start_server(client_handler, HOST, port)
    ws_server = await websockets.serve(ws_handler, HOST, WS_PORT) \
        if websockets is not None else None
    asyncio.create_task(tick_loop())
    async with server:
        if ws_server is not None:
            await ws_server.serve_forever()
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
