#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab01-TCP · 单元 2：应用层协议 —— 把"字节流"变成"指令"
============================================================
教学目标：
  1. 粘包问题：TCP 只保证字节顺序，不保证"一次 recv = 一条消息"——
     可能是半条（半包），也可能是多条拼成一坨（粘包）。
     解法 = 应用层约定「一帧 = 一行文本（UTF-8），以 \\n 结尾」，接收端按行读。
  2. asyncio：一个事件循环同时伺候多个客户端（对比单元 1：一次只能接一个）。
  3. 协议设计初版：PING 心跳 / CMD:动作[:参数] / ACK 回执 / ERR 错误。
     注意：本步还没有运动模型——FORWARD 只回 ACK，车不会动（单元 3 才动）。

协议（车端 -> 客户端）：
  PONG / ACK:FORWARD:60 / ACK:LEFT / ERR:UNKNOWN_CMD:FOO / ERR:BAD_FRAME:xxx

运行：python step2_command_server.py
测试：python step1_test_client.py 127.0.0.1
      依次输入 PING / CMD:FORWARD / CMD:FORWARD:80 / CMD:FOO / BADLINE 看应答
观察：开两个客户端同时发指令，两边都能得到应答（单元 1 做不到）。
"""
import asyncio
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = "0.0.0.0"
PORT = 8888
DEFAULT_SPEED = 60    # FORWARD/BACK 未带速度参数时的默认值


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 协议解析
def handle_command(text):
    """解析一行指令，返回应答帧列表（一帧指令可能产生多条应答）"""
    if text == "PING":                       # 心跳：证明连接还活着
        return ["PONG"]
    if not text.startswith("CMD:"):          # 不认识的帧格式，明确拒绝
        return [f"ERR:BAD_FRAME:{text}"]

    # CMD:动作[:参数] —— 按冒号切分
    parts = text.split(":")
    action = parts[1].strip().upper()

    if action in ("FORWARD", "BACK"):
        speed = DEFAULT_SPEED
        if len(parts) >= 3:                  # 带了速度参数：校验并钳位到 0~100
            try:
                speed = max(0, min(100, int(parts[2])))
            except ValueError:
                return [f"ERR:BAD_SPEED:{parts[2]}"]
        # 约定：前进速度记为正，后退记为负（单元 3 运动模型直接消费这个符号）
        return [f"ACK:{action}:{speed if action == 'FORWARD' else -speed}"]
    if action == "LEFT":
        return ["ACK:LEFT"]                  # 单元 3 起会附带真实朝向角
    if action == "RIGHT":
        return ["ACK:RIGHT"]
    if action == "STOP":
        return ["ACK:STOP"]
    if action == "STATUS":
        return ["ACK:STATUS"]                # 单元 3 升级成真正的 STATE 帧
    return [f"ERR:UNKNOWN_CMD:{action}"]


# ---------------------------------------------------------------- 连接处理
async def client_handler(reader, writer):
    """每个客户端一个协程：asyncio 事件循环让它们"同时"运行"""
    addr = writer.get_extra_info("peername")
    log(f"[连接] 新客户端 {addr[0]}:{addr[1]}")
    try:
        while True:
            # readline：读到 \n 才返回 —— 按"帧"取数据，粘包问题就此解决
            line = await reader.readline()
            if not line:                     # 读到空字节 = 对端正常关闭
                log(f"[断开] {addr[0]}:{addr[1]} 正常退出")
                break
            text = line.decode("utf-8").strip()
            if not text:                     # 空行（只有 \n）直接忽略
                continue
            log(f"[<-] {text}   来自 {addr[0]}:{addr[1]}")
            for reply in handle_command(text):
                log(f"[->] {reply}")
                writer.write((reply + "\n").encode("utf-8"))
            await writer.drain()             # 确保写缓冲真正发出去
    except (ConnectionError, UnicodeDecodeError) as e:
        log(f"[断开] {addr[0]}:{addr[1]} 异常: {e}")
    finally:
        writer.close()                       # 无论哪条路退出，都释放连接


async def main():
    # start_server = bind/listen/accept 的 asyncio 版：每来一个连接
    # 就自动创建一个 client_handler 协程去伺候它
    server = await asyncio.start_server(client_handler, HOST, PORT)
    log(f"[启动] 协议服务端监听 {HOST}:{PORT}（asyncio 可同时接多个客户端）")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
