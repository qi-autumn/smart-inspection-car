# -*- coding: utf-8 -*-
"""
第 ② 步接口自测脚本（路线图 ② 验收：curl 全流程 + 未注册 device_id 返回 400）

运行前提：
  1. 两台模拟器已启动（start_all_cars.bat）
  2. 业务后端已启动（python server/app.py，端口 8000）

运行：python server/test_api.py
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
SERVER_DIR = Path(__file__).resolve().parent
DEVICES_JSON = SERVER_DIR / "devices.json"
COMMANDS_LOG = SERVER_DIR / "commands.log"

results = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append(cond)
    print(f"[{'通过' if cond else '失败'}] {name}" + (f"（{detail}）" if detail else ""))


def call(method: str, path: str, body: dict | None = None,
         token: str | None = None, params: dict | None = None):
    """返回 (status_code, 解析后的json或None)"""
    if params:
        from urllib.parse import urlencode
        path += ("&" if "?" in path else "?") + urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, {"_html": raw}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return e.code, None


def main():
    with open(DEVICES_JSON, encoding="utf-8") as f:
        registry = json.load(f)
    devs = registry["devices"]
    car1, car2 = devs[0]["device_id"], devs[1]["device_id"]

    # ---- /health ----
    code, r = call("GET", "/health")
    check("/health 返回 200 + ok", code == 200 and r.get("ok") is True)
    check("/health 车队摘要 total=2", r.get("total") == len(devs), f"total={r.get('total')}")
    check("/health 有车在线（模拟器需已启动）", r.get("online", 0) >= 1, f"online={r.get('online')}")

    # ---- /devices ----
    code, r = call("GET", "/devices")
    lst = r.get("devices", [])
    check("/devices 返回 200 + ok", code == 200 and r.get("ok") is True)
    check("/devices 车数量与注册表一致", len(lst) == len(devs), f"{len(lst)}台")
    check("/devices 字段齐全(device_id/name/online/battery)",
          all(all(k in d for k in ("device_id", "name", "online", "battery")) for d in lst))

    # ---- /status 正常流 ----
    code, r = call("GET", f"/status?device_id={car1}")
    check("/status 返回 200 + ok", code == 200 and r.get("ok") is True)
    check("/status 带状态字段(x/y/heading/speed/battery/time/online)",
          all(k in r for k in ("x", "y", "heading", "speed", "battery", "time", "online")))
    code, r = call("GET", f"/status?device_id={car2}")
    check("/status 第二台车也正常", code == 200 and r.get("ok") is True)

    # ---- /status 参数错误（400） ----
    code, r = call("GET", "/status")
    check("/status 缺 device_id → 400", code == 400 and r.get("ok") is False)
    code, r = call("GET", "/status?device_id=not_exist_car_009")
    check("/status 未注册 device_id → 400", code == 400 and r.get("ok") is False)

    # ---- /command 正常流（真车会动，最后 STOP 刹停） ----
    code, r = call("POST", "/command", {"device_id": car1, "cmd": "FORWARD", "speed": 60})
    check("/command FORWARD+speed=60 → ok + command_id",
          code == 200 and r.get("ok") is True and r.get("command_id"),
          f"command_id={r.get('command_id', '')[:16]}...")
    check("/command 响应带 device_id/executed/response_name",
          r.get("device_id") == car1 and r.get("executed") is True
          and r.get("response_name") == "FORWARD")
    code, r = call("POST", "/command", {"device_id": car2, "cmd": "LEFT", "speed": 40})
    check("/command 第二台车 LEFT 也路由成功", code == 200 and r.get("ok") is True)
    code, r = call("POST", "/command", {"device_id": car2, "cmd": "STOP"})
    check("/command 第二台车 STOP 刹停 → ok", code == 200 and r.get("ok") is True)

    # ---- MOVE：V2 摇杆矢量语义（angle 方位角 0=北顺时针 + speed） ----
    code, r = call("POST", "/command",
                   {"device_id": car1, "cmd": "MOVE", "angle": 90, "speed": 60})
    check("/command MOVE 90°(正东)+speed=60 → ok",
          code == 200 and r.get("ok") is True and r.get("response_name") == "MOVE")
    code, r = call("POST", "/command",
                   {"device_id": car1, "cmd": "MOVE", "angle": 315, "speed": 45})
    check("/command MOVE 315°(东北) 任意角度 → ok", code == 200 and r.get("ok") is True)
    code, r = call("POST", "/command", {"device_id": car1, "cmd": "STOP"})
    check("/command MOVE 后 STOP 刹停 → ok", code == 200 and r.get("ok") is True)

    code, r = call("POST", "/command", {"device_id": car1, "cmd": "STOP"})
    check("/command STOP 刹停 → ok", code == 200 and r.get("ok") is True)

    # ---- /command 参数错误（400） ----
    code, r = call("POST", "/command", {"device_id": car1, "cmd": "FLY"})
    check("/command 未知 cmd → 400", code == 400 and r.get("ok") is False)
    code, r = call("POST", "/command", {"device_id": car1, "cmd": "MOVE", "speed": 60})
    check("/command MOVE 缺 angle → 400", code == 400 and r.get("ok") is False)
    code, r = call("POST", "/command",
                   {"device_id": car1, "cmd": "MOVE", "angle": 400, "speed": 60})
    check("/command MOVE angle=400 越界 → 400", code == 400 and r.get("ok") is False)
    code, r = call("POST", "/command", {"device_id": car1, "cmd": "FORWARD", "speed": 150})
    check("/command speed=150 越界 → 400", code == 400 and r.get("ok") is False)
    code, r = call("POST", "/command", {"device_id": car1, "cmd": "FORWARD"})
    check("/command FORWARD 缺 speed → 400", code == 400 and r.get("ok") is False)
    code, r = call("POST", "/command", {"cmd": "STOP"})
    check("/command 缺 device_id → 400", code == 400 and r.get("ok") is False)
    code, r = call("POST", "/command", {"device_id": "not_exist_car_009", "cmd": "STOP"})
    check("/command 未注册 device_id → 400", code == 400 and r.get("ok") is False)

    # ---- /fleet/status ----
    code, r = call("GET", "/fleet/status")
    cars = r.get("cars", [])
    check("/fleet/status 返回 200 + cars 数组", code == 200 and isinstance(cars, list))
    check("/fleet/status 车辆数与注册表一致", len(cars) == len(devs))
    check("/fleet/status 每车带 color（协议 §3 渲染色）",
          all(c.get("color") for c in cars),
          ", ".join(f"{c['device_id'].split('_')[-1]}:{c.get('color')}" for c in cars))
    check("/fleet/status 状态字段齐全",
          all(all(k in c for k in ("online", "x", "y", "heading", "speed", "battery", "time"))
              for c in cars))

    # ---- /track 轨迹查询（第③步，协议 §7.3） ----
    def track_q(extra: str = ""):
        return call("GET", f"/track?device_id={car1}{extra}")

    # 若库里还没有轨迹，先短驾驶 ~26 秒造数据（行驶 5s 节奏可产生 4+ 个点）
    code, r = track_q()
    if code == 200 and r.get("count", 0) == 0:
        print("（轨迹库为空，先短驾驶两车造 26 秒数据…）")
        for c in (car1, car2):
            call("POST", "/command", {"device_id": c, "cmd": "FORWARD", "speed": 50})
        time.sleep(12)
        for c in (car1, car2):
            call("POST", "/command", {"device_id": c, "cmd": "LEFT", "speed": 50})
        time.sleep(12)
        for c in (car1, car2):
            call("POST", "/command", {"device_id": c, "cmd": "STOP"})
        time.sleep(6)
        code, r = track_q()

    check("/track 返回 200 + ok + device_id", code == 200 and r.get("ok") is True
          and r.get("device_id") == car1)
    check("/track 带超集字段(x/y/t/heading/speed) + count",
          r.get("count", 0) > 0 and all(
              all(k in p for k in ("x", "y", "t", "heading", "speed"))
              for p in r.get("points", [])),
          f"count={r.get('count')}")
    ts_list = [p["t"] for p in r.get("points", [])]
    check("/track 时间升序无重复", ts_list == sorted(ts_list) and
          len(ts_list) == len(set(ts_list)))

    code, r = call("GET", "/track")
    check("/track 缺 device_id → 400", code == 400 and r.get("ok") is False)
    code, r = call("GET", "/track?device_id=not_exist_car_009")
    check("/track 未注册 device_id → 400", code == 400 and r.get("ok") is False)
    code, r = track_q("&from=abc")
    check("/track 非法时间格式 → 400", code == 400 and r.get("ok") is False)
    code, r = track_q("&from=2026-01-01%2010:00&to=2026-01-01%2009:00")
    check("/track from 晚于 to → 400", code == 400 and r.get("ok") is False)

    code, r5 = track_q("&from=" + str(int(time.time()) - 300))
    code24, r24 = track_q()
    both_ok = r5.get("ok") and r24.get("ok")
    check("/track 时间窗过滤生效（最近5分钟 ≤ 24小时，且都有数据）",
          both_ok and 0 < r5["count"] <= r24["count"],
          f"5min={r5.get('count')} 24h={r24.get('count')}")

    # ---- 审计日志 ----
    has_log = COMMANDS_LOG.exists()
    got_device_id = False
    got_reject = False
    if has_log:
        lines = COMMANDS_LOG.read_text(encoding="utf-8").strip().splitlines()
        for line in lines[-15:]:
            rec = json.loads(line)
            if "device_id" in rec:
                got_device_id = True
            if str(rec.get("result", "")).startswith("rejected"):
                got_reject = True
    check("commands.log 存在且记录带 device_id", got_device_id)
    check("commands.log 含命令拒绝记录", got_reject)

    # ---- 第⑤步：管理后台接口 ----
    code, r = call("GET", "/admin/overview")
    check("/admin/overview 未登录 → 401", code == 401 and r.get("ok") is False)
    code, r = call("POST", "/admin/login", {"username": "admin", "password": "wrong"})
    check("/admin/login 密码错误 → 401", code == 401 and r.get("ok") is False)
    code, r = call("POST", "/admin/login", {"username": "admin", "password": "admin123"})
    check("/admin/login 正确账号 → ok + token",
          code == 200 and r.get("ok") is True and bool(r.get("token")))
    token = r.get("token", "")
    code, r = call("GET", "/admin/overview", token=token)
    check("/admin/overview 登录后 → total/online/cars 齐全",
          code == 200 and r.get("total") == 2 and "cars" in r and "track_points" in r)
    code, r = call("GET", "/admin/devices", token=token)
    check("/admin/devices 列出注册表（带 frozen 字段）",
          code == 200 and len(r.get("devices", [])) == 2
          and "frozen" in r["devices"][0])
    code, r = call("GET", "/admin/commands?limit=10", token=token)
    check("/admin/commands 返回结构化历史（最新在前）",
          code == 200 and isinstance(r.get("records"), list) and len(r["records"]) <= 10)
    car1_frozen_body = {"device_id": car1, "cmd": "STOP"}
    call("POST", f"/admin/devices/{car1}/freeze", {"frozen": True}, token=token)
    code, r = call("POST", "/command", car1_frozen_body)
    check("冻结后 /command → 403 拒绝", code == 403 and r.get("ok") is False)
    code, r = call("GET", "/status", params={"device_id": car1}, token=token)
    check("冻结不影响状态查询", code == 200 and r.get("ok") is True)
    call("POST", f"/admin/devices/{car1}/freeze", {"frozen": False}, token=token)
    code, r = call("POST", "/command", car1_frozen_body)
    check("解冻后 /command 恢复正常 → ok", code == 200 and r.get("ok") is True)
    code, r = call("GET", "/admin/view")
    check("/admin/view 管理后台页面可访问", code == 200 and "<html" in r.get("_html", "").lower())

    print()
    passed, total = sum(1 for x in results if x), len(results)
    print(f"验收结果: {passed}/{total} 通过")
    if passed == total:
        print("第 ② 步（业务后端多车化）自测通过 ✅（待人工验收）")
    else:
        print("第 ② 步自测存在失败项，请检查上方 [失败] 条目")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
