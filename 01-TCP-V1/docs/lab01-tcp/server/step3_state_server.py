#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab01-TCP · 单元 3：小车动起来 —— 运动模型 + STATE 状态广播
================================================================
教学目标：
  1. 用一个 Car 类把"真实小车"抽象成数据：位置(x,y)、朝向(heading)、
     速度(speed)、电量(battery)——将来这段逻辑搬进真车主控板，协议不变。
  2. 运动模型：每 100ms 一步，按 heading 用 sin/cos 算出 x/y 增量——
     三角函数第一次真正上战场（heading 0°=朝上时 x 不动 y 递减）。
  3. 服务端"主动推"：STATE 帧每 100ms 广播给所有客户端——
     APP 才能持续看到车在哪。推 vs 拉，这里先体验"推"。

坐标系约定（服务端 / APP / 浏览器三处必须一致，改一处就是 bug）：
  场地 800×600；(0,0) 在左上角；heading 0°=朝上、顺时针增大。
  因此 x 增量 = speed·sin(heading)，y 增量 = -speed·cos(heading)（y 向下为正）。

协议（在单元 2 基础上的升级点）：
  ACK:LEFT:<角度> / ACK:RIGHT:<角度>   现在附带真实朝向角
  CMD:STATUS                           回真正的 STATE 帧
  STATE:x=..,y=..,heading=..,speed=..,battery=..   每 100ms 主动广播

运行：python step3_state_server.py
测试：python step1_test_client.py 127.0.0.1
      先发 CMD:STATUS 看静止状态；再 CMD:FORWARD:70 后持续观察 STATE 坐标变化
"""
import asyncio
import math
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = "0.0.0.0"
PORT = 8888
TICK_MS = 100            # 运动更新 / STATE 广播周期
TURN_STEP = 15           # 每次原地转向角度
DEFAULT_SPEED = 60
FIELD_W, FIELD_H = 800, 600   # 场地尺寸（与将来的俯视图页面共用）


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 运动模型
class Car:
    """小车状态：位置、朝向（0°=朝上，顺时针增）、带符号速度、电量"""

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
        self.x += self.speed * 0.02 * math.sin(rad)    # 0.02：像素比例系数
        self.y -= self.speed * 0.02 * math.cos(rad)    # y 向下为正，所以取负
        self.x = min(max(self.x, 10), FIELD_W - 10)    # 撞边界：夹住不越界
        self.y = min(max(self.y, 10), FIELD_H - 10)
        self.battery = max(20.0, self.battery - 0.01)  # 动起来才耗电

    def state_frame(self):
        """运动状态 -> 一行 STATE 帧（协议契约，APP/浏览器都按它解析）"""
        return (f"STATE:x={self.x:.0f},y={self.y:.0f},"
                f"heading={self.heading % 360},speed={self.speed},"
                f"battery={self.battery:.0f}")


car = Car()
clients = set()     # 所有在线 TCP 客户端（STATE 广播要发给每一个人）


# ---------------------------------------------------------------- 协议解析
def handle_command(text):
    """解析一行指令，返回应答帧列表（升级点：ACK 带真实状态、STATUS 回真帧）"""
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
        return [car.state_frame()]     # 单元 2 的占位符在这里转正
    return [f"ERR:UNKNOWN_CMD:{action}"]


# ---------------------------------------------------------------- 连接处理
async def client_handler(reader, writer):
    addr = writer.get_extra_info("peername")
    log(f"[连接] 新客户端 {addr[0]}:{addr[1]}")
    clients.add(writer)                # 登记进广播名单
    try:
        # 接入即推一帧当前状态：APP 一连上画面就有数据，不用先发 STATUS
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
            log(f"[<-] {text}   来自 {addr[0]}:{addr[1]}")
            for reply in handle_command(text):
                log(f"[->] {reply}")
                writer.write((reply + "\n").encode("utf-8"))
            await writer.drain()
    except (ConnectionError, UnicodeDecodeError) as e:
        log(f"[断开] {addr[0]}:{addr[1]} 异常: {e}")
    finally:
        clients.discard(writer)        # 退出广播名单
        writer.close()


# ---------------------------------------------------------------- 广播循环
async def tick_loop():
    """每 100ms：推进运动模型，把 STATE 广播给所有在线客户端"""
    while True:
        await asyncio.sleep(TICK_MS / 1000)
        car.step()
        frame = (car.state_frame() + "\n").encode("utf-8")
        dead = []
        for w in list(clients):        # list() 拷贝：遍历时集合可能被并发修改
            try:
                w.write(frame)
                await w.drain()
            except (ConnectionError, RuntimeError):
                dead.append(w)         # 写失败的（已断开）稍后统一摘除
        for w in dead:
            clients.discard(w)


async def main():
    server = await asyncio.start_server(client_handler, HOST, PORT)
    log(f"[启动] 运动模型版服务端监听 {HOST}:{PORT}")
    log(f"[说明] STATE 每 {TICK_MS}ms 广播一帧；出生点 "
        f"({car.x:.0f},{car.y:.0f}) 朝向 {car.heading}°")
    asyncio.create_task(tick_loop())   # 广播循环与连接处理并行跑
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
