#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab01-TCP · 单元 1：最小 TCP 客户端（交互式）
================================================
教学目标：客户端只要两步：create_connection（connect）-> send/recv
用法：python step1_test_client.py [服务端IP]     （默认 127.0.0.1，quit 退出）
配合 step1_echo_server.py 使用：发什么回什么。
单元 2 之后，这个客户端也能用来手动发协议指令（PING / CMD:FORWARD 等）。
"""
import socket
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = 8888

sock = socket.create_connection((HOST, PORT))    # 客户端只需一步：主动 connect（内含三次握手）
print(f"[连接] {HOST}:{PORT} 已建立")
print("输入一行文字回车发送（发什么回什么），quit 退出")

try:
    while True:
        text = input("> ").strip()
        if text in ("quit", "exit"):
            break
        if not text:
            continue
        # 约定：一帧 = 一行文本，末尾补 \n（单元 2 服务端靠它分帧）
        sock.sendall((text + "\n").encode("utf-8"))
        data = sock.recv(1024)                   # 一问一答：等回声
        print(f"[<-] {data.decode('utf-8', errors='replace').strip()}")
finally:
    sock.close()
    print("[退出] 连接已关闭")
