#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab01-TCP · 单元 1：最小 TCP 服务端（回声服务器）
====================================================
教学目标：
  1. TCP 服务端三步曲：bind（占端口）-> listen（排队等客）-> accept（接客）
  2. TCP 本质是"字节流"：没有消息边界，recv 收到的是"一段字节"而不是"一条消息"
     （这个坑在单元 2 正式解决，这里先埋下伏笔）
  3. 一问一答（echo）：收到什么原样回什么，验证双向通路

运行：python step1_echo_server.py
测试：另开终端 -> python step1_test_client.py 127.0.0.1
观察：再开第三个终端连进来试试——会发现它卡在"连接中"，
      因为本步一次只能伺候一个客户端（同步阻塞式），单元 2 用 asyncio 解决。
"""
import socket
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = "0.0.0.0"    # 监听本机所有网卡：同一局域网内的其他设备（模拟器/手机）也能连
PORT = 8888

# 1. 创建 socket：AF_INET=IPv4 地址族，SOCK_STREAM=TCP（对比 UDP 的 SOCK_DGRAM）
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# 端口复用：进程主动关闭后端口立刻可再次 bind（否则要等约 2 分钟的 TIME_WAIT）
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, True)

server.bind((HOST, PORT))    # 2. 占住 8888 端口（被占用会报 WinError 10048）
server.listen()              # 3. 开始排队等待客户端连接
print(f"[启动] TCP 服务端监听 {HOST}:{PORT}，等待客户端接入...（Ctrl+C 退出）")

while True:
    conn, addr = server.accept()      # 阻塞点 1：直到有客户端连进来
    print(f"[连接] 客户端 {addr[0]}:{addr[1]} 接入")
    while True:
        data = conn.recv(1024)        # 阻塞点 2：直到收到字节（一次最多取 1024 字节）
        if not data:                  # recv 返回空字节 = 对端正常关闭了连接
            print(f"[断开] {addr[0]}:{addr[1]} 离开")
            break
        text = data.decode("utf-8", errors="replace").strip()
        print(f"[收到] {text!r}")     # !r 打印原始形态，连 \n 都看得见
        conn.sendall(data)            # 原样回给客户端（echo）
    conn.close()                      # 一个客户端的生命周期结束，回到 accept 继续等人
