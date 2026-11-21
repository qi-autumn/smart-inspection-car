# -*- coding: utf-8 -*-
"""
双车驾驶脚本（第③步验收用）：让两台模拟车真实跑动，产生轨迹数据。

用法：
  python server/drive_cars.py --minutes 10     # 双车随机驾驶 10 分钟
  python server/drive_cars.py --minutes 2      # 快速造 2 分钟数据

前提：模拟器与业务后端都已启动（车辆需在线）。
结束时自动双车刹停。
"""

import argparse
import json
import random
import time
import urllib.request
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
DEVICES_JSON = SERVER_DIR / "devices.json"

CMDS = ("FORWARD", "BACK", "LEFT", "RIGHT")


def command(base: str, device_id: str, cmd: str, speed: int | None) -> bool:
    body = {"device_id": device_id, "cmd": cmd}
    if speed is not None:
        body["speed"] = speed
    req = urllib.request.Request(
        base + "/command", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()).get("ok") is True
    except Exception as e:
        print(f"  [失败] {device_id.split('_')[-1]} {cmd}: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="V3 双车随机驾驶（造轨迹数据）")
    ap.add_argument("--minutes", type=float, default=10.0, help="驾驶时长（分钟）")
    ap.add_argument("--interval", type=float, default=6.0, help="换动作间隔（秒）")
    ap.add_argument("--backend", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    with open(DEVICES_JSON, encoding="utf-8") as f:
        cars = [d["device_id"] for d in json.load(f)["devices"]]
    print(f"驾驶车辆: {', '.join(c.split('_')[-1] for c in cars)}")
    print(f"时长 {args.minutes} 分钟，每 {args.interval}s 换一次动作\n")

    deadline = time.time() + args.minutes * 60
    rnd = random.Random()
    round_no = 0
    try:
        while time.time() < deadline:
            round_no += 1
            remain = (deadline - time.time()) / 60
            for car in cars:
                if rnd.random() < 0.25:
                    ok = command(args.backend, car, "STOP", None)
                    act = "STOP"
                else:
                    cmd = rnd.choice(CMDS)
                    speed = rnd.randint(30, 70)
                    ok = command(args.backend, car, cmd, speed)
                    act = f"{cmd} speed={speed}"
                print(f"第{round_no:>3}轮 {car.split('_')[-1]}: {act} "
                      f"{'✔' if ok else '✘'}（剩余 {remain:.1f} 分钟）")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n手动中断")
    finally:
        print("\n收尾：双车刹停")
        for car in cars:
            command(args.backend, car, "STOP", None)
        print("完成。现在可以打开 http://127.0.0.1:8000/track/view 查看轨迹")


if __name__ == "__main__":
    main()
