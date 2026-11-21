# -*- coding: utf-8 -*-
"""
云端桥接后端（第三阶段：APP -> 本后端 -> 华为云 IoTDA -> 小车）

按照华为云官方文档模板编写：
  《应用侧Python SDK使用指南》
  https://support.huaweicloud.com/sdkreference-iothub/iot_10_10003.html

角色：
  - 对下（APP）：暴露简单 HTTP API（9090 端口），接口形状与第一阶段保持一致
  - 对上（云端）：用华为云应用侧 Python SDK（IoTDAClient）调用 v5 API
      * 命令下发  client.create_command()
      * 影子查询  client.show_device_shadow()
      * 设备状态  client.list_devices()

先安装 SDK（仅需一次）：
  pip install huaweicloudsdkcore
  pip install huaweicloudsdkiotda

运行：python cloud_backend.py
"""

import json
import threading
import time
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.auth.credentials import DerivedCredentials
from huaweicloudsdkiotda.v5 import *

# ---------------- 配置（按你的实例填写） ----------------
CONFIG = {
    # 访问密钥 AK/SK：控制台右上角"我的凭证 -> 访问密钥 -> 新增访问密钥"，
    # 下载的 credentials.csv 里 AK/SK 两列。也可用环境变量
    # HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK（官方推荐，优先读取）。
    "ak": os.environ.get("HUAWEICLOUD_SDK_AK", "YOUR_HUAWEI_AK"),
    "sk": os.environ.get("HUAWEICLOUD_SDK_SK", "YOUR_HUAWEI_SK"),

    # 项目 ID：控制台"我的凭证 -> 项目列表"，cn-north-4 那一行的"项目ID"
    "project_id": "YOUR_PROJECT_ID",

    # 区域 ID
    "region_id": "cn-north-4",

    # HTTPS 应用接入地址：控制台"总览 -> 接入信息 -> HTTPS 应用接入地址"
    "endpoint": "https://YOUR_APP_ENDPOINT",

    # 平台上的设备（key=短名供 APP 使用，value=完整设备 ID）
    "devices": {
        "car_001": "YOUR_PRODUCT_ID_car_001",
        "car_002": "YOUR_PRODUCT_ID_car_002",
    },

    # 本后端对 APP 暴露的端口
    "http_port": 9090,

    # 影子轮询间隔（秒）：两台设备各查一次，别设太小（API 有 QPS 限制）
    "poll_interval": 2.0,
}

# 与物模型一致：move/stop 命令挂在 car_control 服务下（属性上报在 car_status）
SERVICE_ID = "car_control"

# ---------------- IoTDA 客户端（官方模板写法） ----------------
client = None


def build_client():
    """按文档模板：创建认证 -> 创建 IoTDAClient 实例并初始化"""
    global client
    ak = os.environ.get("HUAWEICLOUD_SDK_AK", CONFIG["ak"])
    sk = os.environ.get("HUAWEICLOUD_SDK_SK", CONFIG["sk"])
    project_id = os.environ.get("HUAWEICLOUD_SDK_PROJECT_ID", CONFIG["project_id"])
    region_id = CONFIG["region_id"]
    endpoint = CONFIG["endpoint"]

    # 标准版/企业版：自定义 Region（区域ID + HTTPS应用接入地址）
    region = Region(region_id, endpoint)

    # 创建认证（BasicCredentials）
    credentials = BasicCredentials(ak, sk, project_id)
    # 标准版/企业版必须加 with_derived_predicate（基础版可删掉这一行）
    credentials.with_derived_predicate(
        DerivedCredentials.get_default_derived_predicate())

    # 创建并初始化 IoTDAClient
    client = IoTDAClient.new_builder() \
        .with_credentials(credentials) \
        .with_region(region) \
        .build()
    print(f"[SDK] IoTDAClient 初始化完成（{region_id}）")


# ---------------- 对 APP 提供的三个能力 ----------------
def send_command(device_id: str, name: str, paras: dict) -> dict:
    """命令下发：等效于 POST /v5/iot/{project_id}/devices/{device_id}/commands"""
    try:
        request = CreateCommandRequest()
        request.device_id = device_id
        request.body = DeviceCommandRequest(
            service_id=SERVICE_ID, command_name=name, paras=paras)
        response = client.create_command(request)
        return {"request_id": getattr(response, "request_id", "")}
    except exceptions.ClientRequestException as e:
        raise RuntimeError(
            f"命令下发失败 {e.status_code} {e.error_code}: {e.error_msg}")


def get_shadow(device_id: str) -> dict:
    """查询设备影子，返回该设备最新上报的属性 dict"""
    try:
        request = ShowDeviceShadowRequest()
        request.device_id = device_id
        response = client.show_device_shadow(request)
    except exceptions.ClientRequestException as e:
        raise RuntimeError(
            f"影子查询失败 {e.status_code} {e.error_code}: {e.error_msg}")

    # 响应结构：response.shadow = [ {service_id, reported: {properties: {...}}} ]
    props: dict = {}
    for svc in getattr(response, "shadow", None) or []:
        reported = getattr(svc, "reported", None)
        if reported is None:
            continue
        p = getattr(reported, "properties", None)
        if isinstance(p, dict):
            props.update(p)
    return props


def get_device_status(device_id: str) -> str:
    """查询设备在线状态：ONLINE / OFFLINE / ..."""
    try:
        request = ListDevicesRequest()
        request.device_id = device_id
        request.limit = 1
        response = client.list_devices(request)
        devices = getattr(response, "devices", None) or []
        if devices:
            return getattr(devices[0], "status", "UNKNOWN")
    except exceptions.ClientRequestException as e:
        print(f"[SDK] 状态查询失败 {e.status_code}: {e.error_msg}")
    return "UNKNOWN"


# ---------------- 状态缓存（后台轮询影子） ----------------
STATE_CACHE = {}          # 短名 -> {x,y,theta,speed,battery,online,ts}
CACHE_LOCK = threading.Lock()


def poll_loop():
    """周期性轮询每台设备的影子 + 在线状态，刷新缓存"""
    while True:
        for short, dev_id in CONFIG["devices"].items():
            entry = {"online": False, "x": 0.0, "y": 0.0, "theta": 0.0,
                     "speed": 0.0, "battery": 0.0, "ts": time.time()}
            try:
                status = get_device_status(dev_id)
                entry["online"] = (status == "ONLINE")
                props = get_shadow(dev_id)
                for k in ("x", "y", "theta", "speed", "battery"):
                    if k in props:
                        entry[k] = float(props[k])
            except Exception as e:
                print(f"[轮询] {short} 失败: {e}")
            with CACHE_LOCK:
                STATE_CACHE[short] = entry
        time.sleep(CONFIG["poll_interval"])


# ---------------- 对 APP 的 HTTP 服务 ----------------
def resolve_dev(query: dict) -> str:
    """把 ?dev=car_001（短名或完整ID）解析为完整设备 ID"""
    name = (query.get("dev") or [""])[0]
    if name in CONFIG["devices"]:
        return CONFIG["devices"][name]
    for full in CONFIG["devices"].values():
        if name == full:
            return full
    return name  # 原样返回，让云端报错


class ApiHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/api/devices":
                with CACHE_LOCK:
                    devs = {k: dict(v) for k, v in STATE_CACHE.items()}
                self._send(200, {"devices": devs})

            elif url.path == "/api/state":
                short = (query.get("dev") or ["car_001"])[0]
                with CACHE_LOCK:
                    entry = STATE_CACHE.get(short)
                if entry is None:
                    self._send(404, {"error": f"未知设备 {short}"})
                else:
                    self._send(200, entry)

            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            payload = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "bad json"})
            return

        dev_id = resolve_dev(query)
        if not dev_id:
            self._send(400, {"error": "缺少 dev 参数"})
            return
        try:
            if url.path == "/api/move":
                paras = {"vx": float(payload.get("vx", 0)),
                         "vy": float(payload.get("vy", 0))}
                send_command(dev_id, "move", paras)
                self._send(200, {"ok": True, "cmd": "move", "paras": paras})
            elif url.path == "/api/stop":
                send_command(dev_id, "stop", {})
                self._send(200, {"ok": True, "cmd": "stop"})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(502, {"error": str(e)})

    def log_message(self, *args):
        pass


def main():
    # 校验配置是否已填写
    need_fill = []
    for key in ("ak", "sk", "project_id"):
        v = os.environ.get(f"HUAWEICLOUD_SDK_{key.upper()}") or CONFIG[key]
        if (not v) or "填写" in str(v):
            need_fill.append(key)
    if need_fill:
        print("=" * 62)
        print("请先填写 CONFIG 中的:", "、".join(need_fill))
        print("  ak / sk    -> 我的凭证 -> 访问密钥（credentials.csv）")
        print("  project_id -> 我的凭证 -> 项目列表 cn-north-4 的项目ID")
        print("填完保存后再运行本脚本！")
        print("=" * 62)
        return

    build_client()
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG["http_port"]), ApiHandler)
    print(f"[HTTP] 后端已就绪: http://0.0.0.0:{CONFIG['http_port']}")
    print(f"       APP 侧接口: /api/devices  /api/state?dev=car_001")
    print(f"                   /api/move?dev=car_001  /api/stop?dev=car_001")
    server.serve_forever()


if __name__ == "__main__":
    main()
