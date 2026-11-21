#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab01-TCP · 完整版 TCP 测试客户端（收发两条线程）
====================================================
背景：step1_test_client.py 是"发一条才收一条"的阻塞式循环——
服务端每 100ms 推的 STATE 帧会积压在接收缓冲区里，直到下一次发送
才被一次性捞出打印（批量炸屏）。真实客户端必须"同时听和说"：
  后台线程：专职 recv，实时解析并打印每一帧（含 \n 分帧，同服务端约定）
  主线程  ：专职读键盘，负责发送

四种用法（IP 写在最前面，开关顺序随意）：
  1) 交互：python test_client.py <IP> [端口]
     收发全开，STATE 实时滚动——单终端调试用。
  2) 自测：python test_client.py <IP> --auto
     自动发送一组指令并统计收包，快速验收车端（单元 3~4 回归用）。
  3) 监视屏：python test_client.py <IP> --monitor
     只收不发：专职打印 STATE 广播，当"位置大屏"用；Ctrl+C 退出。
     （没有"纯只发"模式：完全不 recv 会堆满接收缓冲区、反压服务端——
      收可以不看，但不能不收。）
  4) 控制台：python test_client.py <IP> --ctrl
     主发轻收：键盘发指令，ACK/ERR/PONG 回执照常打印；
     STATE 帧静默丢弃（只在回执后附带"已静默 N 帧"计数）。
     指令不再被广播刷屏——监视屏和控制台两个终端各司其职。

典型组合：终端 A 跑 --monitor 投影当大屏，终端 B 跑 --ctrl 发指令。
"""
import socket
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 自测脚本：(要发送的指令, 发送后等待秒数)
AUTO_SCRIPT = [
    ("PING", 0.2),
    ("CMD:STATUS", 0.2),
    ("CMD:FORWARD:70", 0.6),   # 前进 0.6 秒，观察 STATE 中 x/y 变化
    ("CMD:LEFT", 0.2),         # 原地左转 15°
    ("CMD:FORWARD:70", 0.6),   # 沿新朝向前进
    ("CMD:RIGHT", 0.2),
    ("CMD:BACK:40", 0.6),      # 后退
    ("CMD:STOP", 0.3),
    ("CMD:FOO", 0.2),          # 未知指令 -> 应回 ERR:UNKNOWN_CMD
    ("BADLINE", 0.2),          # 非法帧   -> 应回 ERR:BAD_FRAME
    ("CMD:STATUS", 0.3),       # 最终状态
]

received = {"n": 0}            # 收帧计数（线程共享，dict 原子操作够用）
state_muted = [0]              # --ctrl 模式下被静默丢弃的 STATE 帧数
stop = threading.Event()


def recv_loop(sock, show_state=True, prompt=""):
    """后台线程：专职收帧。注意这里同样手工做 \\n 分帧——
    recv 拿到的是字节流，一次可能半帧也可能一坨帧（和服务端同一个坑）。

    show_state=False（--ctrl）：STATE 帧照收但静默丢弃、只计数——
    不显示 ≠ 不接收，不收会反压服务端，这是 TCP 的硬道理。
    prompt：打印后重画的提示符；监视模式传空串（没有输入行可护）。"""
    buf = b""
    while not stop.is_set():
        try:
            data = sock.recv(4096)
        except OSError:
            break
        if not data:                                  # 空 = 服务端关闭连接
            print("\n[断开] 服务端关闭了连接")
            break
        received["n"] += 1
        buf += data
        while b"\n" in buf:                           # 缓冲区里可能有多帧，逐帧取出
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            if text.startswith("STATE:") and not show_state:
                state_muted[0] += 1                   # 静默丢弃，TCP 通路保持顺畅
                continue
            # \r 回到行首抹掉提示符，打印后重画 —— 提示符不被刷乱
            note = f"  (已静默 {state_muted[0]} 帧)" if state_muted[0] else ""
            print(f"\r[<-车端] {text}{note}")
            if prompt:
                print(prompt, end="", flush=True)
    muted = f"，其中静默丢弃 STATE {state_muted[0]} 帧" if state_muted[0] else ""
    print(f"\n[统计] 共接收 {received['n']} 次 recv{muted}")


def parse_args(argv):
    """宽容解析：-- 开头当开关，纯数字当端口，第一个剩下的当 IP。
    （旧版把 sys.argv[1] 直接当 IP，敲 --ctrl 会报 getaddrinfo failed——
    开关写在哪、IP 写不写在前面都该能容错。）"""
    ip, port, mode = None, 8888, "interactive"
    for a in argv:
        if a == "--auto":
            mode = "auto"
        elif a == "--monitor":
            mode = "monitor"
        elif a == "--ctrl":
            mode = "ctrl"
        elif a.isdigit():
            port = int(a)
        elif ip is None:
            ip = a
    return ip, port, mode


def main():
    ip, port, mode = parse_args(sys.argv[1:])
    if ip is None:
        print(__doc__)
        print("[用法] python test_client.py <IP> [端口] [--auto | --monitor | --ctrl]")
        return

    sock = socket.create_connection((ip, port))
    mode_note = {
        "interactive": "交互模式（收发全开）",
        "auto": "自测模式",
        "monitor": "监视模式（只收不发，Ctrl+C 退出）",
        "ctrl": "控制模式（主发轻收，STATE 静默丢弃）",
    }[mode]
    print(f"[连接] {ip}:{port} 已建立 · {mode_note}")

    show_state = mode != "ctrl"
    prompt = "" if mode == "monitor" else "> "
    # 收发分离：后台收帧线程设为 daemon —— 主线程退出时它自动跟着结束
    t = threading.Thread(target=recv_loop, args=(sock, show_state, prompt), daemon=True)
    t.start()

    try:
        if mode == "auto":
            for payload, wait in AUTO_SCRIPT:
                sock.sendall((payload + "\n").encode("utf-8"))
                print(f"\r[->车端] {payload}")
                time.sleep(wait)
            time.sleep(0.5)
            print("\n" + "=" * 46)
            print(f"自测完成：共收到 {received['n']} 次 recv")
            print("检查要点：每条 CMD 都有 ACK 回执；STATE 持续广播（约 100ms/帧）；")
            print("CMD:FOO / BADLINE 分别回 ERR:UNKNOWN_CMD / ERR:BAD_FRAME")
            print("=" * 46)
        elif mode == "monitor":
            # 只收不发：主线程挂起等 Ctrl+C，后台线程独自打印
            print("监视中... Ctrl+C 退出")
            while True:
                time.sleep(0.5)
        else:  # interactive / ctrl：键盘发送
            print("输入指令（如 CMD:FORWARD:60 / PING），回车发送，quit 退出")
            while True:
                text = input("> ").strip()
                if text in ("quit", "exit"):
                    break
                if text:
                    sock.sendall((text + "\n").encode("utf-8"))
    except (EOFError, KeyboardInterrupt):
        # 管道输入结束 / Ctrl+C：正常收摊
        pass

    stop.set()
    try:
        # 先 shutdown 通知对端"我说完了"（发 FIN），再 close 释放资源——
        # 直接 close 也是合法的，但服务端日志会记成"异常断开"，不够优雅
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()          # 关 socket 会让 recv 线程的 recv() 立刻报错退出


if __name__ == "__main__":
    main()
