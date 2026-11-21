# -*- coding: utf-8 -*-
"""
第 ① 步验收脚本：多车模拟底座

验收标准（路线图）：双实例同时连 MQTT，影子查询能看到两台设备各自独立的位置数据。

检查项：
  1. 注册表里每台设备在 IoTDA 平台上状态为 ONLINE
  2. 每台设备影子（car_status 服务）能查到 x/y/theta/speed/battery
  3. 两台设备的位置数据各自独立（出生点不同 → 影子坐标不同，互不串扰）

运行前提：先启动两台模拟器（start_all_cars.bat 或分别运行
  python device/car_mqtt_client.py --device car_001 / car_002）

运行：python device/verify_step1.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEVICES_JSON = ROOT / "server" / "devices.json"
CLOUD_CONFIG_JSON = ROOT / "server" / "cloud_config.json"

from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
from huaweicloudsdkiotda.v5 import *


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_client(cfg: dict):
    ak = cfg["ak"]
    sk = cfg["sk"]
    credentials = BasicCredentials(ak, sk, cfg["project_id"])
    credentials.with_derived_predicate(
        DerivedCredentials.get_default_derived_predicate())
    region = Region(cfg["region_id"], cfg["endpoint"])
    return IoTDAClient.new_builder() \
        .with_credentials(credentials) \
        .with_region(region) \
        .build()


def get_status(client, dev_id: str) -> str:
    # 注意：ListDevices 的 device_id 过滤参数不生效（会返回网关等无关设备），
    # 必须用 ShowDevice 按 ID 精确查询
    request = ShowDeviceRequest()
    request.device_id = dev_id
    response = client.show_device(request)
    return getattr(response, "status", "UNKNOWN")


def get_shadow_props(client, dev_id: str) -> dict:
    request = ShowDeviceShadowRequest()
    request.device_id = dev_id
    response = client.show_device_shadow(request)
    props: dict = {}
    for svc in getattr(response, "shadow", None) or []:
        reported = getattr(svc, "reported", None)
        if reported is None:
            continue
        p = getattr(reported, "properties", None)
        if isinstance(p, dict):
            props.update(p)
    return props


def main():
    registry = load_json(DEVICES_JSON)
    cfg = load_json(CLOUD_CONFIG_JSON)
    devices = registry.get("devices", [])
    print(f"注册表设备: {', '.join(d['device_id'] for d in devices)}\n")

    client = build_client(cfg)
    results = []
    shadows = {}

    # 检查 1：在线状态
    for dev in devices:
        full_id = dev["device_id"]
        try:
            status = get_status(client, full_id)
        except exceptions.ClientRequestException as e:
            status = f"查询失败({e.status_code})"
        ok = (status == "ONLINE")
        results.append(ok)
        print(f"[{'通过' if ok else '失败'}] {full_id} 在线状态: {status}")

    # 检查 2：影子属性
    for dev in devices:
        full_id = dev["device_id"]
        try:
            props = get_shadow_props(client, full_id)
        except exceptions.ClientRequestException as e:
            props = {}
            print(f"       影子查询失败 {e.status_code}: {e.error_msg}")
        shadows[full_id] = props
        need = ("x", "y", "theta", "speed", "battery")
        missing = [k for k in need if k not in props]
        ok = not missing
        results.append(ok)
        if ok:
            print(f"[通过] {full_id} 影子: x={props['x']} y={props['y']} "
                  f"theta={props['theta']} speed={props['speed']} battery={props['battery']}")
        else:
            print(f"[失败] {full_id} 影子缺字段: {missing}（设备上报过一次后才有影子）")

    # 检查 3：两车位置独立（影子坐标应等于各自出生点、互不相同）
    if len(devices) >= 2:
        da, db = devices[0], devices[1]
        pa, pb = shadows.get(da["device_id"], {}), shadows.get(db["device_id"], {})
        try:
            ax, ay = float(pa["x"]), float(pa["y"])
            bx, by = float(pb["x"]), float(pb["y"])
            dist = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            sa, sb = da["spawn"], db["spawn"]
            ok = (dist > 0.5
                  and abs(ax - sa["x"]) < 0.5 and abs(ay - sa["y"]) < 0.5
                  and abs(bx - sb["x"]) < 0.5 and abs(by - sb["y"]) < 0.5)
            results.append(ok)
            print(f"[{'通过' if ok else '失败'}] 位置独立: ({ax},{ay}) vs ({bx},{by}) "
                  f"相距 {dist:.1f}m（期望≈出生点 "
                  f"{(sa['x'], sa['y'])} / {(sb['x'], sb['y'])}）")
        except (KeyError, TypeError, ValueError):
            results.append(False)
            print("[失败] 位置独立: 影子缺坐标，无法比较")
    else:
        results.append(False)
        print("[失败] 位置独立: 注册表少于 2 台设备")

    print()
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"验收结果: {passed}/{total} 通过")
    if passed == total:
        print("第 ① 步（多车模拟底座）验收通过 ✅")
    else:
        print("第 ① 步验收未通过，请检查上方失败项")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
