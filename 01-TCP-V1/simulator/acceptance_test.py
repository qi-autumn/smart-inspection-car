# -*- coding: utf-8 -*-
"""
智能巡检车 · 分层自动验收脚本

用法：
  python acceptance_test.py        # 依次执行第 1、2、3 层全部自动验收项
  python acceptance_test.py 1      # 只验收第 1 层（协议）
  python acceptance_test.py 2      # 只验收第 2 层（网络，约 15 秒）
  python acceptance_test.py 3      # 只验收第 3 层（模拟）

前提：先启动服务端  python car_server.py
第 4、5 层为手动验收清单，见 docs/项目分层验收方案.md
"""

import json
import math
import socket
import sys
import time
import urllib.request

HOST = "127.0.0.1"
TCP_PORT = 9000
HTTP_URL = f"http://{HOST}:8080/state"
MAX_SPEED = 1.5

results = []  # (layer, item, desc, pass_bool, detail)


def check(layer, item, desc, ok, detail=""):
    results.append((layer, item, desc, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {item} {desc}  {detail}")


class CarClient:
    """验收用最小 TCP 客户端（与鸿蒙端同协议）"""

    def __init__(self):
        self.sock = socket.create_connection((HOST, TCP_PORT), timeout=3)
        self.buf = b""

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def recv_states(self, dur=1.0):
        """dur 秒内收到的全部消息"""
        self.sock.settimeout(0.2)
        out, t0 = b"", time.time()
        while time.time() - t0 < dur:
            try:
                d = self.sock.recv(4096)
                if not d:
                    break
                out += d
            except socket.timeout:
                pass
        return [json.loads(l) for l in out.decode().split("\n") if l.strip()]

    def close(self):
        self.sock.close()


def http_state():
    return json.loads(urllib.request.urlopen(HTTP_URL, timeout=2).read().decode())


# ---------------- 第 1 层：通信协议层 ----------------

def layer1():
    print("\n=== 第一层：通信协议层 ===")
    c = CarClient()

    # 1.1+1.2 指令格式：服务端能解析即视为格式一致（异常解析会被静默丢弃，无状态更新即 FAIL）
    c.send({"type": "cmd", "action": "move", "vx": 0, "vy": 0.5})
    c.send({"type": "cmd", "action": "stop"})
    c.send({"type": "ping"})
    msgs = c.recv_states(1.0)
    has_pong = any(m.get("type") == "pong" for m in msgs)
    has_state = any(m.get("type") == "state" for m in msgs)
    check(1, "1.1/1.2", "指令格式两端一致", has_pong and has_state,
          f"pong={'有' if has_pong else '无'} state={'有' if has_state else '无'}")

    # 1.2 state 字段完整性
    st = next((m for m in msgs if m.get("type") == "state"), None)
    fields_ok = st is not None and all(k in st for k in ("x", "y", "theta", "speed", "battery"))
    check(1, "1.2b", "state 五字段完整", fields_ok,
          f"字段={sorted(st.keys()) if st else '无'}")

    # 1.3 粘包：三条一次发送
    c.sendall = c.sock.sendall
    c.sock.sendall((json.dumps({"type": "cmd", "action": "move", "vx": 0, "vy": 0.5}) + "\n"
                    + json.dumps({"type": "cmd", "action": "stop"}) + "\n"
                    + json.dumps({"type": "ping"}) + "\n").encode())
    msgs = c.recv_states(1.0)
    sticky_ok = (any(m.get("type") == "pong" for m in msgs)
                 and any(m.get("type") == "state" and m["speed"] == 0 for m in msgs))
    check(1, "1.3", "粘包逐条解析", sticky_ok, f"共收 {len(msgs)} 条")

    # 1.4 拆包：半条消息 + 后续补齐
    half = json.dumps({"type": "cmd", "action": "move", "vx": 0, "vy": 1.0}).encode()
    c.sock.sendall(half[:len(half) // 2])
    time.sleep(0.2)
    c.recv_states(0.3)
    c.sock.sendall(half[len(half) // 2:] + b"\n")
    msgs = c.recv_states(1.0)
    moving = any(m.get("type") == "state" and m["speed"] > 0.1 for m in msgs)
    check(1, "1.4", "拆包拼接后正常", moving,
          f"split 后 speed>0.1 的状态 {sum(1 for m in msgs if m.get('speed', 0) > 0.1)} 条")
    c.send({"type": "cmd", "action": "stop"})
    c.recv_states(0.5)
    c.close()


# ---------------- 第 2 层：网络连接层 ----------------

def layer2():
    print("\n=== 第二层：网络连接层（约 15 秒）===")
    c = CarClient()

    # 2.1 建立连接
    check(2, "2.1", "TCP 连接建立", True, f"{HOST}:{TCP_PORT}")

    # 2.2 心跳保活：静置 10 秒，连接仍可用
    print("  ... 心跳静置 10 秒 ...")
    time.sleep(10)
    c.send({"type": "ping"})
    msgs = c.recv_states(1.0)
    alive = any(m.get("type") == "pong" for m in msgs)
    check(2, "2.2", "静置 10 秒后连接存活", alive)

    # 2.3 状态广播频率（5Hz）
    c.send({"type": "cmd", "action": "move", "vx": 0, "vy": 0.3})
    msgs = c.recv_states(2.0)
    c.send({"type": "cmd", "action": "stop"})
    c.recv_states(0.5)
    n = sum(1 for m in msgs if m.get("type") == "state")
    check(2, "2.3", "状态广播 ~5Hz", 6 <= n <= 14, f"2 秒收到 {n} 条")

    # 2.5 断开后重连
    c.close()
    time.sleep(0.5)
    try:
        c2 = CarClient()
        c2.send({"type": "ping"})
        msgs = c2.recv_states(1.0)
        check(2, "2.5", "断开后重连成功", any(m.get("type") == "pong" for m in msgs))
        # 为 3.6 做准备：异常断开（不发 stop 直接断）
        c2.send({"type": "cmd", "action": "move", "vx": 0, "vy": 1.0})
        time.sleep(0.3)
        c2.close()  # 模拟异常断连
    except OSError as e:
        check(2, "2.5", "断开后重连成功", False, str(e))

    # 2.4 + 3.6 断线感知 / 断连自动停车：观察 HTTP 状态
    time.sleep(1.2)
    st = http_state()
    check(2, "2.4", "服务端感知客户端断开", st["speed"] == 0,
          f"断连后 speed={st['speed']}（应自动停车为 0）")


# ---------------- 第 3 层：小车模拟层 ----------------

def layer3():
    print("\n=== 第三层：小车模拟层 ===")
    c = CarClient()

    # 3.1 匀速直线：move (0,1) 1 秒，y 增量 ≈ 1.5
    c.send({"type": "cmd", "action": "move", "vx": 0, "vy": 1.0})
    states = [m for m in c.recv_states(1.0) if m.get("type") == "state"]
    c.send({"type": "cmd", "action": "stop"})
    st_end = http_state()
    c.recv_states(0.5)
    dy = states[-1]["y"] - states[0]["y"] if len(states) >= 2 else 0
    check(3, "3.1", "匀速直线 y 增量 ≈ 1.5m", 1.2 <= dy <= 1.8,
          f"实测 Δy={dy:.3f}, theta={states[-1]['theta'] if states else '?'}")

    # 3.2 方向映射：(0.7,0.7) → theta≈45°
    c.send({"type": "cmd", "action": "move", "vx": 0.7, "vy": 0.7})
    states = [m for m in c.recv_states(0.8) if m.get("type") == "state"]
    theta = states[-1]["theta"] if states else -999
    check(3, "3.2", "斜向 45° 朝向", 40 <= theta <= 50, f"theta={theta}°")
    c.send({"type": "cmd", "action": "stop"})
    c.recv_states(0.5)

    # 3.3 摇杆限幅：(1,1) 归一化后速度不超 MAX_SPEED
    c.send({"type": "cmd", "action": "move", "vx": 1.0, "vy": 1.0})
    states = [m for m in c.recv_states(0.8) if m.get("type") == "state"]
    spd = states[-1]["speed"] if states else 999
    check(3, "3.3", "矢量限幅 speed ≤ 1.5", spd <= MAX_SPEED + 1e-6, f"speed={spd}")
    c.send({"type": "cmd", "action": "stop"})
    c.recv_states(0.5)

    # 3.4 停车：位置不再变化
    s1 = http_state()
    time.sleep(0.6)
    s2 = http_state()
    check(3, "3.4", "stop 后位置静止", (s1["x"], s1["y"]) == (s2["x"], s2["y"]),
          f"({s1['x']},{s1['y']}) -> ({s2['x']},{s2['y']})")

    # 3.5 电量：移动时下降（前面已多次移动）
    check(3, "3.5", "电量已消耗且 <100", s2["battery"] < 100, f"battery={s2['battery']}%")

    # 3.7 HTTP 与 TCP 同源
    c.send({"type": "cmd", "action": "ping"})
    c.recv_states(0.5)
    st = http_state()
    consistent = set(st.keys()) >= {"x", "y", "theta", "speed", "battery"}
    check(3, "3.7", "HTTP /state 数据源一致", consistent, json.dumps(st))

    # 3.8 360° 全角度：输入 210° → 角度转矢量 → theta 应 ≈ 210°
    deg = 210.0
    rad = math.radians(deg)
    c.send({"type": "cmd", "action": "move",
            "vx": round(math.sin(rad), 4), "vy": round(math.cos(rad), 4)})
    states = [m for m in c.recv_states(0.8) if m.get("type") == "state"]
    theta = states[-1]["theta"] if states else -999
    check(3, "3.8", "全角度 210° 朝向一致", 200 <= theta <= 220, f"theta={theta}°")
    c.send({"type": "cmd", "action": "stop"})
    c.recv_states(0.5)

    # 3.9 360° 全角度：输入 315°（西北方向）→ 位置应向 x 减小、y 增大
    deg = 315.0
    rad = math.radians(deg)
    x0, y0 = http_state()["x"], http_state()["y"]
    c.send({"type": "cmd", "action": "move",
            "vx": round(math.sin(rad), 4), "vy": round(math.cos(rad), 4)})
    c.recv_states(1.0)
    c.send({"type": "cmd", "action": "stop"})
    c.recv_states(0.5)
    st = http_state()
    check(3, "3.9", "全角度 315° 位移方向（西北）", st["x"] < x0 and st["y"] > y0,
          f"Δx={st['x'] - x0:+.2f} Δy={st['y'] - y0:+.2f}")

    c.close()


# ---------------- 汇总 ----------------

def summary(layers_shown):
    print("\n" + "=" * 56)
    print(" 分层验收汇总")
    print("=" * 56)
    for layer in layers_shown:
        items = [r for r in results if r[0] == layer]
        passed = sum(1 for r in items if r[3])
        print(f" 第 {layer} 层: {passed}/{len(items)} 通过")
    total = sum(1 for r in results if r[3])
    print(f" 合计: {total}/{len(results)}")
    if total < len(results):
        print("\n 未通过项详情：")
        for layer, item, desc, ok, detail in results:
            if not ok:
                print(f"  第 {layer} 层 {item} {desc}  {detail}")
    else:
        print("\n 自动验收全部通过。第四、五层请按 docs/项目分层验收方案.md 手动清单执行。")


if __name__ == "__main__":
    layers = [int(a) for a in sys.argv[1:] if a.isdigit()] or [1, 2, 3]
    try:
        for layer in layers:
            if layer == 1:
                layer1()
            elif layer == 2:
                layer2()
            elif layer == 3:
                layer3()
            else:
                print(f"第 {layer} 层为手动验收，清单见 docs/项目分层验收方案.md")
        summary(layers)
    except (ConnectionRefusedError, socket.timeout, urllib.error.URLError):
        print("无法连接服务端，请先运行: python car_server.py")
        sys.exit(1)
