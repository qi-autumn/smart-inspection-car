# -*- coding: utf-8 -*-
"""
V3 巡检车队平台 · 业务后端（第②步：业务后端多车化）

按《V3-接口协议设计.md》v1.1 实现 L1 接口（APP ↔ 业务后端，HTTP 8000）：
  GET  /devices            车列表（名称/在线/电量）
  GET  /status?device_id=  单车状态（device_id 必填）
  POST /command            控制命令（FORWARD/BACK/LEFT/RIGHT/STOP + speed 0~100）
  GET  /fleet/status       车队状态批量（L4 兜底通道，第④步网页用）
  GET  /health             探活 + 车队摘要

设计要点：
  - 设备注册表 server/devices.json（协议 §3），device_id 为华为云完整 ID
  - 拉模式（L2）：后台线程周期查 ShowDevice（在线状态）+ ShowDeviceShadow（影子），
    写入车队状态缓存 STATE_CACHE（协议 §5；第④步 WebSocket 广播的数据源）
  - 命令映射：协议层 FORWARD/BACK/LEFT/RIGHT/STOP + speed(0~100)
    → V2 物模型 car_control 服务的 move(vx,vy)/stop（云侧零改动）
  - 响应包络：{"ok": true, ...} / {"ok": false, "error": "..."}（协议 §2）
  - 审计日志 server/commands.log（JSON Lines，含 device_id）
  - 在线状态必须用 ShowDevice 按 ID 精确查（ListDevices 过滤参数不生效，第①步踩坑）

运行（先启动模拟器，再启动本后端）：
  python server/app.py          # 监听 8000
"""

import json
import logging
import math
import os
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_sock import Sock

from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkcore.auth.credentials import BasicCredentials, DerivedCredentials
from huaweicloudsdkiotda.v5 import (
    IoTDAClient, ShowDeviceRequest, ShowDeviceShadowRequest, CreateCommandRequest,
    DeviceCommandRequest, ListDevicesRequest, AddDeviceRequest, DeleteDeviceRequest,
)

import track_store
import ai_agent                 # 第⑦步：自然语言控车（ASR + LLM 解析 + 规范化）

# ---------------- 路径与配置 ----------------
SERVER_DIR = Path(__file__).resolve().parent
DEVICES_JSON = SERVER_DIR / "devices.json"
CLOUD_CONFIG_JSON = SERVER_DIR / "cloud_config.json"
COMMANDS_LOG = SERVER_DIR / "commands.log"

# 与 V2 物模型一致：move/stop 命令挂在 car_control 服务下（属性上报在 car_status）
SERVICE_ID_CONTROL = "car_control"
SERVICE_ID_STATUS = "car_status"

# 影子里 speed 的单位是 m/s（模拟器 MAX_SPEED=1.5 满速），
# 协议 §2 要求 L1 层 speed 为 0~100，此处做换算
MAX_SPEED_MS = 1.5

POLL_INTERVAL = 2.0            # 影子/状态轮询间隔（秒），两台车约 2 QPS，安全
COMMAND_PARAS_TIMEOUT = 20     # 协议 §4.3 同步命令超时（秒），预留字段

# ---- 本地高速旁路（仅模拟阶段）----
# 模拟器进程自带 20Hz 的 /state 调试接口，后端以 10Hz 直读本机内存状态喂给页面：
# 不产生任何云端消息、不违反 5s/60s 上报硬约束、轨迹库仍只记真实影子观测。
# 环境变量 V3_LOCAL_FAST=0 可关闭（第⑧步真车接入后应关闭，回退纯云端链路）。
LOCAL_FAST = os.environ.get("V3_LOCAL_FAST", "1") != "0"
LOCAL_FAST_INTERVAL = 0.1      # 10Hz 刷新
LOCAL_FAST_TIMEOUT = 0.15      # 单次本机 HTTP 超时
LOCAL_FAST_MAX_AGE = 1.5       # 本地快照超过 1.5s 未更新则不再采信


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


REGISTRY = load_json(DEVICES_JSON)
CLOUD_CFG = load_json(CLOUD_CONFIG_JSON)
DEVICES = {d["device_id"]: d for d in REGISTRY.get("devices", [])}

# ---------------- 设备注册表校验（路线图 ②-1；第⑤步支持热重载） ----------------
def validate_registry(reg: dict) -> None:
    seen = set()
    for dev in reg.get("devices", []):
        did = dev.get("device_id", "")
        if "_" not in did or did.count("_") < 1:
            raise SystemExit(f"[注册表错误] device_id 格式非法: {did!r}（应为 {{product_id}}_{{node_id}}）")
        if did in seen:
            raise SystemExit(f"[注册表错误] device_id 重复: {did}")
        for field in ("name", "color"):
            if field not in dev:
                raise SystemExit(f"[注册表错误] {did} 缺字段: {field}")
        seen.add(did)
    if not reg.get("devices"):
        raise SystemExit("[注册表错误] devices 列表为空")


validate_registry(REGISTRY)


def reload_registry() -> None:
    """第⑤步：devices.json 变更（动态注册/冻结）后重建内存注册表。
    整体替换引用（原子），读方在旧快照上迭代也安全。"""
    global REGISTRY, DEVICES, DEV_META
    REGISTRY = load_json(DEVICES_JSON)
    validate_registry(REGISTRY)
    DEVICES = {d["device_id"]: d for d in REGISTRY.get("devices", [])}
    DEV_META = dict(DEVICES)
    logging.info("[注册表] 已重载：%d 台设备", len(DEVICES))


# ---------------- IoTDA 客户端（与第①步 verify 相同的模板） ----------------
def build_client() -> IoTDAClient:
    credentials = BasicCredentials(
        CLOUD_CFG["ak"], CLOUD_CFG["sk"], CLOUD_CFG["project_id"])
    credentials.with_derived_predicate(
        DerivedCredentials.get_default_derived_predicate())
    region = Region(CLOUD_CFG["region_id"], CLOUD_CFG["endpoint"])
    return IoTDAClient.new_builder() \
        .with_credentials(credentials) \
        .with_region(region) \
        .build()


CLIENT = build_client()


def cloud_show_status(device_id: str) -> str:
    """设备在线状态（必须 ShowDevice 精确查，ListDevices 过滤不生效）"""
    req = ShowDeviceRequest()
    req.device_id = device_id
    resp = CLIENT.show_device(req)
    return getattr(resp, "status", "UNKNOWN")


def cloud_get_shadow(device_id: str) -> dict:
    """查影子，返回 (car_status 属性 dict, event_time)"""
    req = ShowDeviceShadowRequest()
    req.device_id = device_id
    resp = CLIENT.show_device_shadow(req)
    props: dict = {}
    event_time = ""
    for svc in getattr(resp, "shadow", None) or []:
        reported = getattr(svc, "reported", None)
        if reported is None:
            continue
        p = getattr(reported, "properties", None)
        if isinstance(p, dict):
            props.update(p)
        et = getattr(reported, "event_time", None)
        if et:
            event_time = et
    return props, event_time


def cloud_create_command(device_id: str, name: str, paras: dict) -> str:
    """下发命令（car_control 服务），返回平台 command_id"""
    req = CreateCommandRequest()
    req.device_id = device_id
    req.body = DeviceCommandRequest(
        service_id=SERVICE_ID_CONTROL, command_name=name, paras=paras)
    resp = CLIENT.create_command(req)
    return getattr(resp, "command_id", "") or getattr(resp, "request_id", "")


# ---------------- 车队状态缓存（路线图 ②-8，第④步 WS 广播的数据源） ----------------
STATE_CACHE: dict = {}          # device_id -> {online,x,y,heading,speed,battery,time}
CACHE_LOCK = threading.Lock()
ECHO_PENDING: dict = {}         # device_id -> {at, before} 乐观回显守卫（poll_loop 用）
WS_DIRTY = threading.Event()    # 命令受理 → 触发 WebSocket 立即广播一帧


def _echo_command_state(did: str, cmd: str, speed, angle) -> None:
    """乐观回显：命令被云平台受理后，立刻把预期的 heading/speed 写入缓存并触发
    即时 WS 广播 —— 页面端外推从命令时刻起步，消除"命令已发、显示未动"的空窗。
    真实影子回报（模拟器命令事件上报，约 1~2s）随后自然覆盖确认。"""
    if cmd == "STOP":
        new_h, new_s = None, 0
    elif cmd in ("FORWARD", "BACK", "LEFT", "RIGHT", "MOVE"):
        s = DEFAULT_TURN_SPEED if speed is None else speed
        if cmd == "MOVE":
            try:
                new_h = round(float(angle)) % 360
            except (TypeError, ValueError):
                return
        else:
            new_h = {"FORWARD": 0, "BACK": 180, "LEFT": 270, "RIGHT": 90}[cmd]
        new_s = round(s)
    else:
        return
    with CACHE_LOCK:
        entry = STATE_CACHE.get(did)
        if entry is None or not entry.get("online"):
            return                       # 离线设备回显无意义（命令本就送不到）
        prev = ECHO_PENDING.get(did)
        # before 保留链上第一次的"命令前观测"，连续命令不互相污染守卫基准
        before = prev["before"] if prev else (
            entry.get("x"), entry.get("y"), entry.get("heading"), entry.get("speed"))
        if new_h is not None:
            entry["heading"] = new_h
        entry["speed"] = new_s
        ECHO_PENDING[did] = {"at": time.time(), "before": before}
    WS_DIRTY.set()


def _props_to_entry(props: dict, event_time: str, online: bool) -> dict:
    """影子属性（物模型字段）→ 协议 §2 状态字段"""
    def num(key, default=None):
        try:
            return float(props[key])
        except (KeyError, TypeError, ValueError):
            return default
    theta = num("theta")
    speed_ms = num("speed", 0.0) or 0.0
    return {
        "online": online,
        "x": num("x"),
        "y": num("y"),
        "heading": None if theta is None else round(theta),
        "speed": round(max(0.0, min(1.0, speed_ms / MAX_SPEED_MS)) * 100),
        "battery": num("battery"),
        "time": event_time or None,
    }


# ---------------- 轨迹落库（第③步，协议 §7.3 v1.1 勘误：变化才插行） ----------------
TRACK_LAST: dict = {}           # device_id -> 上次入库的状态元组


def _track_changed(did: str, entry: dict) -> bool:
    """与上次入库相比，状态有实质变化才记录（x/y 微动/朝向/速度/电量任一变化）"""
    if entry.get("x") is None or entry.get("y") is None:
        return False
    key = (entry["x"], entry["y"], entry["heading"], entry["speed"], entry["battery"])
    last = TRACK_LAST.get(did)
    if last is None:
        return True
    return (abs(key[0] - last[0]) > 0.005 or abs(key[1] - last[1]) > 0.005
            or (key[2] is not None and last[2] is not None
                and abs(key[2] - last[2]) > 0.5)
            or abs((key[3] or 0) - (last[3] or 0)) > 0.5
            or abs((key[4] or 0) - (last[4] or 0)) > 0.05)


def poll_loop() -> None:
    """周期轮询每台设备的在线状态 + 影子，刷新缓存；状态变化时落轨迹"""
    while True:
        for device_id in list(DEVICES):
            entry = {"online": False, "x": None, "y": None, "heading": None,
                     "speed": 0, "battery": None, "time": None}
            shadow_entry = None
            try:
                status = cloud_show_status(device_id)
                entry["online"] = (status == "ONLINE")
                props, event_time = cloud_get_shadow(device_id)
                shadow_entry = _props_to_entry(props, event_time, entry["online"])
                entry.update(shadow_entry)
            except Exception as e:
                logging.warning("轮询 %s 失败: %s", device_id, e)
                shadow_entry = None
            # 乐观回显守卫：命令刚下发、影子尚未更新时，不让旧影子把回显盖回去
            with CACHE_LOCK:
                pend = ECHO_PENDING.get(device_id)
                if pend is not None:
                    if time.time() - pend["at"] > 4:
                        ECHO_PENDING.pop(device_id, None)   # 守卫超时，放行影子
                    else:
                        b = pend["before"]
                        same = (b[0] is not None and entry["x"] is not None
                                and abs(entry["x"] - b[0]) < 0.005
                                and abs(entry["y"] - b[1]) < 0.005
                                and entry["heading"] == b[2]
                                and entry["speed"] == b[3])
                        if same:
                            cur = STATE_CACHE.get(device_id) or {}
                            entry["heading"] = cur.get("heading")
                            entry["speed"] = cur.get("speed", 0)
                        else:
                            ECHO_PENDING.pop(device_id, None)   # 真实回报已到
                STATE_CACHE[device_id] = entry
                # 云端影子是 5s 前的旧值，每 2s 抄一次会盖掉本地实时位置 → 立刻再合并回来
                if LOCAL_FAST:
                    STATE_CACHE[device_id] = _merge_local_entry(device_id, entry)
            # 轨迹落库只认真实影子观测（乐观回显/本地旁路均不落库），在线且状态有变化才插行
            if shadow_entry and shadow_entry["online"] and _track_changed(device_id, shadow_entry):
                try:
                    track_store.insert_point(
                        device_id, shadow_entry["x"], shadow_entry["y"],
                        shadow_entry["heading"], shadow_entry["speed"],
                        shadow_entry["battery"])
                    TRACK_LAST[device_id] = (shadow_entry["x"], shadow_entry["y"],
                                             shadow_entry["heading"],
                                             shadow_entry["speed"],
                                             shadow_entry["battery"])
                except Exception as e:
                    logging.warning("轨迹落库 %s 失败: %s", device_id, e)
        time.sleep(POLL_INTERVAL)


def cache_snapshot() -> dict:
    with CACHE_LOCK:
        return {k: dict(v) for k, v in STATE_CACHE.items()}


# ---------------- 本地高速旁路：10Hz 直读模拟器 /state ----------------
LOCAL_STATE: dict = {}         # device_id -> {x,y,heading,speed,battery,at}
LOCAL_DEAD: dict = {}          # device_id -> 上次连接失败时刻（1s 内不再重试）
LOCAL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="localfast")


def _fetch_local_state(port: int):
    """并发拉取一台模拟器的本地状态；失败返回 None"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/state",
                                    timeout=LOCAL_FAST_TIMEOUT) as r:
            return json.load(r)
    except Exception:
        return None


def _apply_local_fast(entry: dict, did: str) -> None:
    """本地状态新鲜时用其覆盖位置/朝向/速度/电量；online 仍以云端 ShowDevice 为准。
    注意：模拟器 /state 的 speed 是 m/s，影子链路换算成 0~100，这里读取时已换算。"""
    ls = LOCAL_STATE.get(did)
    if not ls or time.time() - ls["at"] > LOCAL_FAST_MAX_AGE:
        return
    for k in ("x", "y", "heading", "speed", "battery"):
        if ls.get(k) is not None:
            entry[k] = ls[k]
    entry["time"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _merge_local_entry(did: str, entry: dict) -> dict:
    """把本地实时位置合并进缓存条目；命令回显守卫期间保留回显的朝向/速度。"""
    merged = dict(entry)
    _apply_local_fast(merged, did)
    if ECHO_PENDING.get(did):
        merged["heading"] = entry.get("heading")
        merged["speed"] = entry.get("speed", 0)
    return merged


def local_fast_loop() -> None:
    """每 0.1s 拉一次每台模拟器进程内存里的 20Hz 真实状态并推 WS。
    并发拉取（离线端口的超时不阻塞其他车）+ 失败端口 1s 内跳过。
    模拟器没跑 / 端口不通 → 该设备自动回退到云端影子，不影响其他车。"""
    while True:
        t0 = time.time()
        fresh = False
        targets = [(did, DEVICES[did]["http_port"]) for did in list(DEVICES)
                   if DEVICES[did].get("http_port")
                   and t0 - LOCAL_DEAD.get(did, 0) > 1.0]
        replies = list(LOCAL_POOL.map(
            lambda t: _fetch_local_state(t[1]), targets)) if targets else []
        for (did, _), s in zip(targets, replies):
            if s is None:
                LOCAL_DEAD[did] = t0
                continue                      # 该模拟器未启动 → 用云端影子兜底
            LOCAL_DEAD.pop(did, None)
            sid = s.get("device_id")
            if sid and sid != did:
                continue                      # 端口被别的车占用，拒绝串数据
            spd = s.get("speed")
            ls = {"x": s.get("x"), "y": s.get("y"),
                  "heading": s.get("theta"),
                  "speed": None if spd is None else round(
                      max(0.0, min(1.0, spd / MAX_SPEED_MS)) * 100),
                  "battery": s.get("battery"), "at": time.time()}
            with CACHE_LOCK:
                LOCAL_STATE[did] = ls
                entry = STATE_CACHE.get(did)
                if entry is None or not entry.get("online"):
                    continue                  # 云端判离线 → 不伪造在线
                # 本地状态已反映命令 → 提前解除回显守卫（比等影子快 1~2s）
                pend = ECHO_PENDING.get(did)
                if pend:
                    b = pend["before"]
                    if (abs((ls["heading"] or 0) - (b[2] or 0)) > 1
                            or abs((ls["speed"] or 0) - (b[3] or 0)) > 1):
                        ECHO_PENDING.pop(did, None)
                        pend = None
                STATE_CACHE[did] = _merge_local_entry(did, entry)
            fresh = True
        if fresh:
            WS_DIRTY.set()                    # 触发 WS 立即广播（10Hz）
        time.sleep(max(0.0, LOCAL_FAST_INTERVAL - (time.time() - t0)))


# ---------------- 审计日志（路线图 ②-7） ----------------
LOG_LOCK = threading.Lock()


def audit(device_id: str, action: str, detail: dict, result: str) -> None:
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device_id": device_id,
        "action": action,
        "detail": detail,
        "result": result,
    }
    line = json.dumps(record, ensure_ascii=False)
    with LOG_LOCK:
        with open(COMMANDS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    print("[审计]", line)


# ---------------- Flask 应用（L1 接口） ----------------
app = Flask(__name__)
sock = Sock(app)               # 第④步：WebSocket 实时广播


def ok(**data):
    return jsonify({"ok": True, **data})


def err(code: int, message: str):
    return jsonify({"ok": False, "error": message}), code


def resolve_device_id(raw: str) -> str | None:
    """注册表精确匹配完整 ID；也接受唯一的 node_id 短名（如 car_001）"""
    if raw in DEVICES:
        return raw
    matches = [d for d in DEVICES if d.split("_", 1)[-1] == raw]
    return matches[0] if len(matches) == 1 else None


def require_device(args) -> tuple[str | None, tuple | None]:
    """device_id 必填且已注册，否则 (None, (400, 响应))"""
    raw = (args.get("device_id") or "").strip()
    if not raw:
        return None, err(400, "缺少必填参数 device_id")
    did = resolve_device_id(raw)
    if did is None:
        return None, err(400, f"device_id 未注册: {raw}")
    return did, None


# 协议 §4.3：cmd 白名单；speed 仅 FORWARD/BACK 需要，0~100 整数
CMD_WHITELIST = ("FORWARD", "BACK", "LEFT", "RIGHT", "MOVE", "STOP")
DEFAULT_TURN_SPEED = 50


def build_move_paras(cmd: str, speed, angle=None) -> tuple[dict | None, str | None]:
    """协议命令 → 物模型 move(vx,vy)/stop。vx 向右 / vy 向上，单位为满速比例

    MOVE 为 V2 摇杆矢量语义扩展：angle 为方位角（0=正北，顺时针 0~360），
    speed 0~100 → vx=sin(a)*ratio, vy=cos(a)*ratio，与 V2 设备端
    theta=atan2(vx,vy) 的朝向约定严格一致。FORWARD/BACK/LEFT/RIGHT 保持协议原样。
    """
    if cmd == "STOP":
        return {}, None
    if cmd in ("FORWARD", "BACK", "LEFT", "RIGHT", "MOVE"):
        if cmd == "MOVE" and angle is None:
            return None, "命令 MOVE 缺少必填参数 angle（方位角 0~360，0=正北顺时针）"
        if cmd in ("FORWARD", "BACK") and speed is None:
            return None, f"命令 {cmd} 缺少必填参数 speed（0~100 整数）"
        s = DEFAULT_TURN_SPEED if speed is None else speed
        if isinstance(s, bool) or not isinstance(s, (int, float)) or not (0 <= s <= 100):
            return None, f"speed 越界（需 0~100 数值）: {s!r}"
        ratio = s / 100.0
        if cmd == "MOVE":
            try:
                a = float(angle)
            except (TypeError, ValueError):
                return None, f"angle 非法（需数值 0~360）: {angle!r}"
            if not (0 <= a <= 360):
                return None, f"angle 越界（需 0~360）: {a!r}"
            rad = math.radians(a)
            return {"vx": round(math.sin(rad) * ratio, 4),
                    "vy": round(math.cos(rad) * ratio, 4)}, None
        paras = {
            "FORWARD": {"vx": 0.0, "vy": ratio},
            "BACK":    {"vx": 0.0, "vy": -ratio},
            "LEFT":    {"vx": -ratio, "vy": 0.0},
            "RIGHT":   {"vx": ratio, "vy": 0.0},
        }[cmd]
        return paras, None
    return None, f"未知 cmd: {cmd!r}（白名单: {'/'.join(CMD_WHITELIST)}）"


@app.get("/health")
def health():
    snap = cache_snapshot()
    return ok(total=len(DEVICES),
              online=sum(1 for e in snap.values() if e["online"]),
              local_fast=LOCAL_FAST,
              ai_configured=(SERVER_DIR / "ai_config.json").exists())


@app.get("/devices")
def devices():
    """协议 §4.1：车列表（battery 离线取影子最后上报值，从未上报为 null）"""
    snap = cache_snapshot()
    out = []
    for did, dev in DEVICES.items():
        entry = snap.get(did, {})
        out.append({
            "device_id": did,
            "name": dev["name"],
            "online": bool(entry.get("online", False)),
            "battery": entry.get("battery"),
        })
    return ok(devices=out)


@app.get("/status")
def status():
    """协议 §4.2：单车状态，device_id 必填"""
    did, bad = require_device(request.args)
    if bad:
        return bad
    entry = cache_snapshot().get(did)
    if entry is None:                      # 首轮轮询还没完成
        entry = {"online": False, "x": None, "y": None, "heading": None,
                 "speed": 0, "battery": None, "time": None}
    return ok(device_id=did, **entry)


@app.get("/fleet/status")
def fleet_status():
    """协议 §7.2：车队状态批量（WebSocket 兜底），cars 数组带渲染色"""
    snap = cache_snapshot()
    cars = []
    for did, dev in DEVICES.items():
        entry = snap.get(did, {})
        cars.append({"device_id": did, "name": dev["name"], "color": dev["color"],
                     **entry})
    return ok(cars=cars)


# ---------------- /track 轨迹查询（第③步，协议 §7.3） ----------------
WEB_DIR = SERVER_DIR.parent / "web"


def _parse_time_param(raw: str | None, name: str, default_ts: int) -> tuple[int, str | None]:
    """时间参数：缺省用 default；接受 Unix 秒或 'YYYY-MM-DD HH:MM[:SS]'（本地时间）"""
    if raw is None or not raw.strip():
        return default_ts, None
    s = raw.strip()
    if s.isdigit() or (s[0] == "-" and s[1:].isdigit()):
        return int(s), None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(s, fmt).timestamp()), None
        except ValueError:
            continue
    return 0, (f"参数 {name} 时间格式非法: {s!r}（应为 Unix 秒或 "
               f"'YYYY-MM-DD HH:MM[:SS]'）")


@app.get("/track")
def track():
    """协议 §7.3：GET /track?device_id=&from=&to=（缺省最近 24 小时，时间升序）"""
    did, bad = require_device(request.args)
    if bad:
        return bad
    now = int(time.time())
    from_ts, e1 = _parse_time_param(request.args.get("from"), "from", now - 86400)
    if e1:
        return err(400, e1)
    to_ts, e2 = _parse_time_param(request.args.get("to"), "to", now)
    if e2:
        return err(400, e2)
    if from_ts > to_ts:
        return err(400, "参数 from 不能晚于 to")
    rows = track_store.query(did, from_ts, to_ts)
    points = [{
        "x": round(p["x"], 3), "y": round(p["y"], 3),
        "t": datetime.fromtimestamp(p["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        "heading": None if p["heading"] is None else round(p["heading"]),
        "speed": None if p["speed"] is None else round(p["speed"]),
    } for p in rows]
    return ok(device_id=did, count=len(points), points=points)


@app.get("/track/view")
def track_view():
    """第③步验收展示页：网页可视化查看 /track 轨迹"""
    return send_file(WEB_DIR / "track_demo.html")


@app.get("/console/view")
def console_view():
    """第②步验收展示页：L1 接口全流程控制台（列表/轮询/命令/审计）"""
    return send_file(WEB_DIR / "fleet_console.html")


@app.get("/audit/recent")
def audit_recent():
    """审计日志最近 N 条（控制台展示用）"""
    n = min(int(request.args.get("n", 6) or 6), 50)
    records: list = []
    if COMMANDS_LOG.exists():
        with open(COMMANDS_LOG, encoding="utf-8") as f:
            records = [ln.rstrip("\n") for ln in f if ln.strip()][-n:]
    return ok(records=records)


def dispatch_command(did: str, cmd: str, speed, angle,
                     source: str = "http") -> tuple[dict | None, tuple | None]:
    """协议 §4.3 命令执行核心：/command 与 /ai/command 共用同一套白名单、审计、乐观回显。

    返回 (result_dict, None) 或 (None, (http_code, message))。
    第⑦步刻意复用这里，保证"AI 解析出的命令"和"摇杆命令"走的是同一条下发链路。
    """
    if DEVICES[did].get("frozen"):
        audit(did, cmd, {"source": source}, "rejected: 设备已冻结")
        return None, (403, f"设备已冻结，命令被拒绝: {did}")

    paras, perr = build_move_paras(cmd, speed, angle=angle)
    if perr is not None:
        audit(did, cmd, {"source": source}, f"rejected: {perr}")
        return None, (400, perr)

    # 注：协议 §4.3 要求"等小车执行回执才返回（约 20 秒）"。
    # 拉模式下应用侧收不到设备 MQTT 回执（需第④/⑤步引入应用侧推送/AMQP 后升级），
    # 当前语义 = 云平台已受理命令（设备在线时立即送达）。
    try:
        command_id = cloud_create_command(
            did, "stop" if cmd == "STOP" else "move", paras)
    except exceptions.ClientRequestException as e:
        audit(did, cmd, paras, f"error: {e.status_code} {e.error_msg} source={source}")
        return None, (502, f"华为云 API 调用失败 {e.status_code}: {e.error_msg}")
    except Exception as e:
        audit(did, cmd, paras, f"error: {e} source={source}")
        return None, (502, f"华为云 API 调用失败: {e}")

    audit(did, cmd, paras, f"ok command_id={command_id} source={source}")
    _echo_command_state(did, cmd, speed, angle)
    return {"device_id": did, "command_id": command_id,
            "executed": True, "response_name": cmd, "paras": paras}, None


@app.post("/command")
def command():
    """协议 §4.3：控制命令（device_id 必填，同步等待回执语义见 dispatch_command 注释）"""
    body = request.get_json(silent=True) or {}
    raw = (body.get("device_id") or "").strip()
    if not raw:
        return err(400, "缺少必填参数 device_id")
    did = resolve_device_id(raw)
    if did is None:
        audit(raw, body.get("cmd"), {"raw": body}, "rejected: device_id 未注册")
        return err(400, f"device_id 未注册: {raw}")
    cmd = (body.get("cmd") or "").strip().upper()
    result, bad = dispatch_command(did, cmd, body.get("speed"), body.get("angle"),
                                   source="http")
    if bad:
        return err(bad[0], bad[1])
    return ok(**result)


# ---------------- 第⑦步：自然语言 / 语音控车 ----------------
AI_LOCK = threading.Lock()     # 同一时刻只允许一个 AI 任务控车，避免指令交错


@app.post("/ai/command")
def ai_command():
    """语音→文字→AI 解析→规范指令→白名单校验→排程→既有 /command 链路。

    入参（二选一）：text（文字模式）/ audio_b64 + mime（语音模式）
    可选：device_id（页面选中的默认车）、dry_run=true（只解析不下发）
    """
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    audio_b64 = body.get("audio_b64") or ""
    mime = (body.get("mime") or "audio/wav").strip()
    hint = (body.get("device_id") or "").strip()
    dry_run = bool(body.get("dry_run"))
    t0 = time.time()

    try:
        cfg = ai_agent.load_config()
    except ai_agent.AIError as e:
        return err(503, f"AI 未配置好: {e}")

    # ① 语音 → 文字（文字模式跳过这一步）
    transcript = text
    if not transcript and audio_b64:
        try:
            transcript = ai_agent.transcribe(cfg, audio_b64, mime)
        except ai_agent.AIError as e:
            return err(502, f"语音转文字失败: {e}")
        if not transcript:
            return err(400, "没有识别到清晰的语音，请重说或改用文字输入")
    if not transcript:
        return err(400, "缺少 text 或 audio_b64")

    # ② 文字 → 规范指令（Function Calling）
    try:
        args = ai_agent.parse_instruction(cfg, transcript, hint, DEVICES)
    except ai_agent.AIError as e:
        return err(502, f"AI 解析失败: {e}")

    # ③ 复核：车号 / 白名单 / 参数范围 / 步数 / 时长
    did, plan, say, perr = ai_agent.normalize(
        args, cfg, DEVICES, resolve_device_id, CMD_WHITELIST)
    if perr:
        audit("AI", "parse", {"transcript": transcript, "raw": args},
              f"rejected: {perr}")
        clarify = str(args.get("say") or "").strip()[:200] if isinstance(args, dict) else ""
        return err(400, perr + (f"　模型提示：{clarify}" if clarify else ""))

    audit(did, "AI", {"transcript": transcript, "plan": plan, "dry_run": dry_run},
          "parsed (dry_run)" if dry_run else "executing")
    if dry_run:
        return ok(transcript=transcript, device_id=did, plan=plan, steps=[],
                  say=say, reply="仅解析，未下发任何命令（dry_run）",
                  dry_run=True, elapsed=round(time.time() - t0, 2))

    # ④⑤ 排程执行：逐条复用 dispatch_command；有 duration 时到点再发下一条（含 STOP）
    steps = []
    with AI_LOCK:
        for i, step in enumerate(plan):
            result, bad = dispatch_command(did, step["cmd"], step.get("speed"),
                                           step.get("angle"), source="ai")
            if bad:
                steps.append({"step": i + 1, **step, "ok": False,
                              "http_code": bad[0], "error": bad[1]})
                break
            steps.append({"step": i + 1, **step, "ok": True,
                          "command_id": result["command_id"],
                          "paras": result["paras"]})
            dur = step.get("duration_ms", 0)
            if dur and i < len(plan) - 1:
                time.sleep(dur / 1000.0)

    okc = sum(1 for s in steps if s["ok"])
    short = did.split("_", 1)[-1]
    if okc == len(plan):
        reply = f"已对 {short} 下发 {okc} 条命令并全部受理"
    else:
        reply = (f"{short} 执行中断：成功 {okc}/{len(plan)} 条；"
                 f"失败原因 {steps[-1].get('error')}")
    return ok(transcript=transcript, device_id=did, plan=plan, steps=steps,
              say=say, reply=reply, dry_run=False,
              elapsed=round(time.time() - t0, 2))


@app.get("/ai/view")
def ai_view():
    """第⑦步验收展示页：自然语言 / 语音控车对话页"""
    return send_file(WEB_DIR / "ai_chat.html")


# ---------------- 第④步：WebSocket 实时广播 + 监控大屏 ----------------
WS_BROADCAST_INTERVAL = 0.5    # 兜底广播周期；命令受理时 WS_DIRTY 触发立即广播

DEV_META = {d["device_id"]: d for d in REGISTRY.get("devices", [])}


def fleet_state_message() -> str:
    """组装广播帧：缓存快照 + 注册表名称/颜色"""
    snap = cache_snapshot()
    cars = []
    for did in DEVICES:
        entry = snap.get(did, {})
        meta = DEV_META.get(did, {})
        cars.append({"device_id": did, "name": meta.get("name", did),
                     "color": meta.get("color", "#888888"), **entry})
    return json.dumps({"type": "fleet_state", "ts": int(time.time()),
                       "cars": cars}, ensure_ascii=False)


@sock.route("/ws")
def ws_fleet(ws):
    """每客户端一个发送线程：状态变化（WS_DIRTY）立即推帧，另有 0.5s 兜底周期"""
    logging.info("[WS] 客户端接入，兜底广播周期 %ss", WS_BROADCAST_INTERVAL)
    try:
        while True:
            ws.send(fleet_state_message())
            WS_DIRTY.wait(WS_BROADCAST_INTERVAL)
            WS_DIRTY.clear()
    except Exception:
        logging.info("[WS] 客户端断开，广播线程退出")


@app.get("/monitor/view")
def monitor_view():
    """第④步验收展示页：WebSocket 实时多车监控大屏"""
    return send_file(WEB_DIR / "monitor.html")


# ---------------- 第⑤步：B/S 管理后台 ----------------
ADMIN_USERS = {"admin": "admin123"}     # 演示环境内置账号；生产环境应改环境变量+哈希
TOKENS: dict = {}                       # token -> username（内存态，重启失效）
COLOR_POOL = ["#3aa55d", "#9b59b6", "#e67e22", "#16a085",
              "#d35400", "#f1c40f", "#8e44ad", "#2c82c9"]
SIM_PROCS: dict = {}                    # device_id -> Popen（本后端拉起的模拟器子进程）
ROOT_DIR = SERVER_DIR.parent            # 项目根（device/ 在此之下）


def _authed(req) -> str | None:
    """校验 X-Auth-Token，返回用户名；失败返回 None"""
    token = req.headers.get("X-Auth-Token", "")
    return TOKENS.get(token)


def _save_registry() -> None:
    with open(DEVICES_JSON, "w", encoding="utf-8") as f:
        json.dump(REGISTRY, f, ensure_ascii=False, indent=2)


@app.post("/admin/login")
def admin_login():
    body = request.get_json(silent=True) or {}
    u, p = body.get("username", ""), body.get("password", "")
    if ADMIN_USERS.get(u) != p or not u:
        return err(401, "用户名或密码错误")
    token = secrets.token_hex(16)
    TOKENS[token] = u
    audit("system", "ADMIN_LOGIN", {"user": u}, "ok")
    return ok(token=token, username=u)


@app.get("/admin/overview")
def admin_overview():
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    snap = cache_snapshot()
    cars = []
    for did, dev in DEVICES.items():
        e = snap.get(did, {})
        cars.append({"device_id": did, "name": dev["name"], "color": dev["color"],
                     "frozen": bool(dev.get("frozen")), **e})
    online = sum(1 for c in cars if c["online"])
    batteries = [c["battery"] for c in cars if c["battery"] is not None]
    return ok(total=len(cars), online=online, frozen=sum(1 for c in cars if c["frozen"]),
              avg_battery=round(sum(batteries) / len(batteries), 1) if batteries else None,
              track_points=track_store.count(),
              commands_total=_count_log_lines(),
              cars=cars)


def _count_log_lines() -> int:
    try:
        with open(COMMANDS_LOG, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


@app.get("/admin/devices")
def admin_devices():
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    snap = cache_snapshot()
    out = []
    for did, dev in DEVICES.items():
        e = snap.get(did, {})
        out.append({"device_id": did, "name": dev["name"], "color": dev["color"],
                    "frozen": bool(dev.get("frozen")), "online": e.get("online"),
                    "battery": e.get("battery"), "x": e.get("x"), "y": e.get("y")})
    return ok(devices=out)


@app.post("/admin/devices")
def admin_register_device():
    """动态注册：IoTDA 云端建设备 + 写 devices.json（含模拟器扩展字段）"""
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    body = request.get_json(silent=True) or {}
    node = (body.get("node_id") or "").strip()
    name = (body.get("name") or "").strip()
    import re
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", node):
        return err(400, "node_id 非法（3~32位字母/数字/下划线，如 car_003）")
    prefix = list(DEVICES)[0].split("_", 1)[0]
    full_id = f"{prefix}_{node}"
    if full_id in DEVICES:
        return err(400, f"node_id 已注册: {node}")
    if not name:
        name = f"巡检车{node}"
    color = COLOR_POOL[len(DEVICES) % len(COLOR_POOL)]
    idx = len(DEVICES)
    secret = "Car" + secrets.token_hex(3).upper()   # 9位随机密钥，页面展示一次

    # 云端注册（product_id/app_id 从现有 car_001 提取）
    try:
        list_req = ListDevicesRequest()
        list_req.limit = 50
        resp = CLIENT.list_devices(list_req)
        product_id = app_id = None
        for d in (resp.devices or []):
            if d.device_id == list(DEVICES)[0]:
                product_id, app_id = d.product_id, d.app_id
                break
        if product_id is None:
            return err(502, "云端未找到现有车辆，无法确定 product_id")
        from huaweicloudsdkiotda.v5.model.add_device import AddDevice
        from huaweicloudsdkiotda.v5.model.auth_info import AuthInfo
        add_req = AddDeviceRequest()
        add_req.body = AddDevice(device_id=full_id, node_id=node, device_name=name,
                                 product_id=product_id, app_id=app_id,
                                 auth_info=AuthInfo(secret=secret, secure_access=True))
        CLIENT.add_device(add_req)
    except exceptions.ClientRequestException as e:
        audit("system", "REGISTER", {"node_id": node}, f"error: {e.status_code} {e.error_msg}")
        return err(502, f"华为云注册失败 {e.status_code}: {e.error_msg}")
    except Exception as e:
        audit("system", "REGISTER", {"node_id": node}, f"error: {e}")
        return err(502, f"华为云注册失败: {e}")

    # 写注册表（模拟器扩展字段：密钥/出生点 8m 网格/调试端口）
    reg_devs = REGISTRY["devices"]
    reg_devs.append({"device_id": full_id, "name": name, "color": color,
                     "secret": secret,
                     "spawn": {"x": 8.0 * (idx // 4), "y": 8.0 * (idx % 4)},
                     "http_port": 8083 + sum(1 for d in reg_devs if d.get("http_port", 0) >= 8083),
                     "frozen": False})
    _save_registry()
    reload_registry()
    audit("system", "REGISTER", {"node_id": node, "device_id": full_id}, "ok")
    return ok(device_id=full_id, name=name, color=color, secret=secret,
              hint=f"启动模拟器: python device/car_mqtt_client.py --device {node}")


@app.post("/admin/devices/<path:did>/freeze")
def admin_freeze(did):
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    if did not in DEVICES:
        return err(400, f"device_id 未注册: {did}")
    body = request.get_json(silent=True) or {}
    frozen = bool(body.get("frozen"))
    for d in REGISTRY["devices"]:
        if d["device_id"] == did:
            d["frozen"] = frozen
    _save_registry()
    reload_registry()
    audit("system", "FREEZE" if frozen else "UNFREEZE", {"device_id": did}, "ok")
    return ok(device_id=did, frozen=frozen)


@app.get("/admin/cloud/devices")
def admin_cloud_devices():
    """云端对账：列出 IoTDA 上本产品下全部直连车设备及其状态，
    并标注是否在本地注册表中（发现云端残留/本地缺失）"""
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    try:
        list_req = ListDevicesRequest()
        list_req.limit = 50    # 平台上限 50，超出报 400
        resp = CLIENT.list_devices(list_req)
    except exceptions.ClientRequestException as e:
        return err(502, f"云端查询失败 {e.status_code}: {e.error_msg}")

    # ListDevices 的过滤参数不生效（第①步踩坑），按 product_id 客户端过滤
    product_id = None
    briefs = []
    for d in (resp.devices or []):
        if d.device_id in DEVICES and product_id is None:
            product_id = d.product_id
        briefs.append(d)
    if product_id is None:
        return err(502, "云端未找到本地注册表中的任何设备，无法确定 product_id")

    cloud_list = []
    for d in briefs:
        if getattr(d, "product_id", None) != product_id:
            continue
        try:
            st = cloud_show_status(d.device_id)
        except Exception:
            st = "UNKNOWN"
        cloud_list.append({
            "device_id": d.device_id,
            "node_id": getattr(d, "node_id", "") or "",
            "name": getattr(d, "device_name", "") or "",
            "status": st,
            "in_registry": d.device_id in DEVICES,
        })
    # 本地有、云端没有（理论上不该出现，注册流程两边同时建）
    local_only = [did for did in DEVICES
                  if did not in {c["device_id"] for c in cloud_list}]
    audit("system", "CLOUD_SYNC", {"cloud": len(cloud_list), "local_only": local_only}, "ok")
    return ok(product_id=product_id, cloud=cloud_list, local_only=local_only)


@app.delete("/admin/cloud/<path:did>")
def admin_delete_cloud_only(did):
    """删除云端残留设备（不在本地注册表中的 IoTDA 设备）"""
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    if did in DEVICES:
        return err(400, "该设备在本地注册表中，请走设备管理接口删除")
    try:
        del_req = DeleteDeviceRequest()
        del_req.device_id = did
        CLIENT.delete_device(del_req)
    except exceptions.ClientRequestException as e:
        audit("system", "DELETE_CLOUD", {"device_id": did},
              f"error: {e.status_code} {e.error_msg}")
        return err(502, f"云端删除失败 {e.status_code}: {e.error_msg}")
    audit("system", "DELETE_CLOUD", {"device_id": did}, "ok")
    return ok(device_id=did, deleted_from="cloud")


@app.delete("/admin/devices/<path:did>")
def admin_delete_device(did):
    """注销设备：云端 DeleteDevice + 本地注册表移除（不可恢复）"""
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    if did not in DEVICES:
        return err(400, f"device_id 未注册: {did}")
    if len(DEVICES) <= 1:
        return err(400, "车队至少保留一台设备，不能全部删除")
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return err(400, "缺少 confirm=true，拒绝执行删除")
    try:
        del_req = DeleteDeviceRequest()
        del_req.device_id = did
        CLIENT.delete_device(del_req)
    except exceptions.ClientRequestException as e:
        if e.status_code == 404:
            pass    # 云端本就没有，视为清理成功
        else:
            audit("system", "DELETE", {"device_id": did},
                  f"error: {e.status_code} {e.error_msg}")
            return err(502, f"云端删除失败 {e.status_code}: {e.error_msg}")
    except Exception as e:
        audit("system", "DELETE", {"device_id": did}, f"error: {e}")
        return err(502, f"云端删除失败: {e}")
    _stop_sim(did)   # 若本机模拟器还在跑，先停掉
    REGISTRY["devices"] = [d for d in REGISTRY["devices"] if d["device_id"] != did]
    _save_registry()
    reload_registry()
    with CACHE_LOCK:
        STATE_CACHE.pop(did, None)
    audit("system", "DELETE", {"device_id": did}, "ok")
    return ok(device_id=did, deleted_from="cloud+registry")


def _find_sim_pids(node: str) -> list:
    """按命令行特征查找本机所有该设备的模拟器进程 PID（含非本后端拉起的）。
    后端重启后会"忘记"自己以前拉起的进程，必须按特征兜底查找。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.CommandLine -like '*car_mqtt_client.py*' "
             f"-and $_.CommandLine -like '*--device {node}*' }} | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=15)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def _stop_sim(did: str) -> bool:
    """终止该设备所有模拟器进程（本后端拉起的 + 外部启动的）"""
    stopped = False
    p = SIM_PROCS.pop(did, None)
    if p is not None and p.poll() is None:
        p.terminate()
        stopped = True
    node = did.split("_", 1)[1]
    for pid in _find_sim_pids(node):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
            stopped = True
        except Exception:
            pass
    return stopped


@app.post("/admin/devices/<path:did>/start")
def admin_start_sim(did):
    """一键上线：在本机拉起该设备的模拟器进程（APP → 后端 → 模拟器连 MQTT）"""
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    if did not in DEVICES:
        return err(400, f"device_id 未注册: {did}")
    p = SIM_PROCS.get(did)
    if p is not None and p.poll() is None:
        return err(400, f"模拟器已在运行（pid {p.pid}）")
    node = did.split("_", 1)[1]
    ext = _find_sim_pids(node)
    if ext:
        if STATE_CACHE.get(did, {}).get("online"):
            return err(400, f"模拟器已在运行（外部进程 pid {ext[0]}）")
        # 有残留进程但车并不在线 → 先清理残留再启动（僵进程会占住调试端口）
        for pid in ext:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        time.sleep(2)   # 等端口释放
    sim_path = ROOT_DIR / "device" / "car_mqtt_client.py"
    if not sim_path.exists():
        return err(500, f"找不到模拟器脚本: {sim_path}")
    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        p = subprocess.Popen(
            [sys.executable, "-u", str(sim_path), "--device", node],
            cwd=str(ROOT_DIR), creationflags=flags)
    except Exception as e:
        audit("system", "SIM_START", {"device_id": did}, f"error: {e}")
        return err(502, f"启动失败: {e}")
    SIM_PROCS[did] = p
    audit("system", "SIM_START", {"device_id": did, "pid": p.pid}, "ok")
    return ok(device_id=did, pid=p.pid,
              hint="模拟器进程已启动，MQTT 建连后约 2~5 秒在线，请稍后刷新")


@app.post("/admin/devices/<path:did>/stop")
def admin_stop_sim(did):
    """一键下线：终止该设备的模拟器进程"""
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    if did not in DEVICES:
        return err(400, f"device_id 未注册: {did}")
    stopped = _stop_sim(did)
    audit("system", "SIM_STOP", {"device_id": did}, "ok" if stopped else "not_running")
    return ok(device_id=did, stopped=stopped)


@app.get("/admin/commands")
def admin_commands():
    """命令历史（commands.log 结构化读取，最新的在前）"""
    if not _authed(request):
        return err(401, "未登录或令牌失效")
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except ValueError:
        return err(400, "limit 非法（正整数）")
    records = []
    try:
        with open(COMMANDS_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return ok(total=len(records), records=list(reversed(records))[:limit])


@app.get("/admin/view")
def admin_view():
    """第⑤步验收展示页：B/S 管理后台"""
    return send_file(WEB_DIR / "admin.html")


# ---------------- 入口 ----------------
def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    track_store.init()
    removed = track_store.cleanup()
    print(f"[轨迹] 启动清理完成，删除 {removed} 行（{track_store.EXPIRE_DAYS}天过期 + "
          f"单车{track_store.MAX_PER_CAR}条上限）")
    threading.Thread(target=track_store.cleanup_loop, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    if LOCAL_FAST:
        threading.Thread(target=local_fast_loop, daemon=True).start()
        print(f"[本地高速旁路] 已启用：{1/LOCAL_FAST_INTERVAL:.0f}Hz 直读模拟器 /state"
              f"（零云端消息；V3_LOCAL_FAST=0 可关闭）")
    print("=" * 62)
    print(f"V3 业务后端已启动（协议 v1.1，L1 端口 8000）")
    print(f"注册表设备: {', '.join(DEVICES)}")
    print(f"接口: GET /devices | GET /status?device_id= | POST /command")
    print(f"      GET /fleet/status | GET /track?device_id= | GET /health")
    print(f"      WS  /ws（每秒广播车队状态，第④步）")
    print(f"展示: http://127.0.0.1:8000/monitor/view （第④步实时监控大屏）")
    print(f"      http://127.0.0.1:8000/track/view   （轨迹回放验收页）")
    print(f"      http://127.0.0.1:8000/admin/view   （第⑤步管理后台 admin/admin123）")
    print(f"      http://127.0.0.1:8000/ai/view      （第⑦步自然语言/语音控车）")
    print(f"AI 配置: {'已就绪' if (SERVER_DIR / 'ai_config.json').exists() else '未配置 /ai/command 会返回 503'}")
    print(f"审计日志: {COMMANDS_LOG} | 轨迹库: {track_store.DB_PATH}")
    print("=" * 62)
    app.run(host="0.0.0.0", port=8000, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
