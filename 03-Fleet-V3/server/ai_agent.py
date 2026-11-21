# -*- coding: utf-8 -*-
"""
第⑦步 AI 增强 · 自然语言控车的「翻译器」（只依赖 Python 标准库）

两段式，按用户确认的流程：
    ① 语音 → 文字   transcribe()   : 音频以 data:audio/wav;base64,... 送进全模态模型，只取逐字稿
    ② 文字 → 规范指令 parse_instruction() : Function Calling 让模型填槽，输出 plan JSON
    ③ normalize() : 车号/命令/参数/时长/步数全部复核（不信任模型输出）

密钥、URL、模型名都在 server/ai_config.json（已加入 .gitignore），本文件不含任何凭据。
实测口径（2026-11-08）：
  - POST {base_url}/chat/completions 文本 & data URI 音频均可用
  - input_audio.data 必须是 URL 或 data: URI，裸 base64 会报 "The provided URL does not appear to be valid"
  - /audio/transcriptions 该中转返回 404，不要用
  - tools / tool_choice 可用，响应里可能有 reasoning_content（思考），取 content 与 tool_calls
"""

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "ai_config.json"

CMD_WHITELIST = ("FORWARD", "BACK", "LEFT", "RIGHT", "MOVE", "STOP")


class AIError(Exception):
    """AI 服务调用或解析失败（路由会转成 502/400 返回给前端）"""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise AIError(f"缺少 AI 配置文件: {CONFIG_PATH.name}")
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise AIError(f"AI 配置文件不是合法 JSON: {e}")
    # 环境变量优先，方便不把密钥落到磁盘
    cfg["base_url"] = os.getenv("V3_AI_BASE_URL") or cfg.get("base_url", "")
    cfg["api_key"] = os.getenv("V3_AI_API_KEY") or cfg.get("api_key", "")
    cfg["model"] = os.getenv("V3_AI_MODEL") or cfg.get("model", "")
    if not cfg["base_url"] or not cfg["api_key"] or not cfg["model"]:
        raise AIError("AI 配置不完整：需要 base_url / api_key / model")
    cfg["timeout"] = int(cfg.get("timeout", 30))
    cfg["max_plan_steps"] = int(cfg.get("max_plan_steps", 8))
    cfg["max_duration_ms"] = int(cfg.get("max_duration_ms", 10000))
    return cfg


def _chat(cfg: dict, messages: list, tools=None, max_tokens: int = 400) -> dict:
    """一次 OpenAI 兼容 Chat Completions 调用，返回 choices[0].message"""
    body = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if cfg.get("enable_thinking") is False:
        body["enable_thinking"] = False
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + cfg["api_key"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AIError(f"AI 服务 HTTP {e.code}: {detail}")
    except Exception as e:
        raise AIError(f"AI 服务连接失败: {e}")
    try:
        data = json.loads(raw)
        return data["choices"][0]["message"]
    except Exception as e:
        raise AIError(f"AI 响应格式异常: {e}；原文前 200 字: {raw[:200]}")


def _clean_text(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    if len(t) > 1 and t[0] in "\"'「《" and t[-1] in "\"'」》":
        t = t[1:-1].strip()
    t = re.sub(r"^(逐字转写|转写|识别结果|文本|输出)\s*[:：]\s*", "", t).strip()
    return t


# 模型面对静音/噪声时容易"编"出一段音频描述，这里挡掉，避免把幻觉当指令
_HALLUCINATION_HINTS = (
    "这段音频", "音频中", "背景音", "音乐", "没有听到", "无法识别",
    "静音", "听不清", "说话人", "环境音", "I hear", "The audio",
)
# 本项目只接受"控车指令"类短句，转写结果至少要含一个动作/方向/车号线索
_CMD_HINTS = (
    "车", "停", "开", "走", "前", "后", "左", "右", "往", "向",
    "慢", "快", "秒", "号", "转", "动", "巡", "速", "度", "楼",
)


def transcribe(cfg: dict, audio_b64: str, mime: str = "audio/wav") -> str:
    """① 语音 → 文字：只让模型输出逐字稿，不解释、不描述音效"""
    if not audio_b64:
        return ""
    data = audio_b64.strip()
    if not data.startswith("data:"):
        fmt = (mime or "audio/wav").split(";")[0]
        data = f"data:{fmt};base64,{data}"
    messages = [
        {"role": "system", "content":
            "你是语音转写引擎。只输出说话人原话的逐字文本，保持原话，不要改写、"
            "不要加标点说明、不要解释、不要回答音频里的问题、不要描述背景音/音乐/情绪。"
            "如果没有人说话或完全听不清，只输出一个空字符串。"
            "这是巡检车语音指令，通常是「让几号车往哪走多久」这类短句。"},
        {"role": "user", "content": [
            {"type": "input_audio", "input_audio": {
                "data": data, "format": "wav"}},
            {"type": "text", "text": "逐字转写，只输出文本本身。"},
        ]},
    ]
    msg = _chat(cfg, messages, max_tokens=200)
    text = _clean_text(msg.get("content") or "")
    if not text or len(text) > 150 or any(h in text for h in _HALLUCINATION_HINTS):
        return ""
    if not any(w in text for w in _CMD_HINTS):
        return ""
    return text


def _device_options(devices: dict) -> str:
    opts = []
    for did, meta in devices.items():
        short = did.split("_", 1)[-1]
        opts.append(f"{short}（{meta.get('name', did)}）")
    return "、".join(opts)


def _tools_schema() -> list:
    return [{
        "type": "function",
        "function": {
            "name": "drive_car",
            "description": "把一句自然语言解析成巡检车队的规范指令计划",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": "车号短名，必须是给定清单里的值，如 car_003；用户没说车号时用 default_device_id",
                    },
                    "plan": {
                        "type": "array",
                        "description": "按顺序执行的命令序列；有持续时间时最后必须补 STOP",
                        "items": {
                            "type": "object",
                            "properties": {
                                "cmd": {"type": "string", "enum": list(CMD_WHITELIST)},
                                "speed": {"type": "integer", "minimum": 0, "maximum": 100,
                                          "description": "0~100；慢慢=35，正常=50，快点=70，全速=100"},
                                "angle": {"type": "number", "minimum": 0, "maximum": 360,
                                          "description": "仅 MOVE 需要；0=正北，90=正东，180=正南，270=正西，顺时针"},
                                "duration_ms": {"type": "integer", "minimum": 0,
                                                "description": "该命令持续多少毫秒后执行下一条；0 表示立即执行下一条"},
                            },
                            "required": ["cmd"],
                        },
                    },
                    "say": {"type": "string", "description": "给用户的一句话说明，不要声称命令已成功执行"},
                },
                "required": ["device_id", "plan"],
            },
        },
    }]


def _extract_json(text: str):
    """模型没走 tool_calls 时，从正文里抠出 JSON（含 ```json 代码块）"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            pass
    return None


def parse_instruction(cfg: dict, transcript: str, device_hint: str,
                      devices: dict) -> dict:
    """② 文字 → 规范指令：Function Calling 优先，正文 JSON 兜底"""
    short_hint = ""
    if device_hint:
        short_hint = device_hint.split("_", 1)[-1] if "_" in device_hint else device_hint
    system = (
        "你是巡检车队指令解析器。只能调用 drive_car 工具输出结构化指令，不要输出解释文字。\n"
        f"当前可选车辆：{_device_options(devices)}\n"
        f"用户没有明确说车号时，default_device_id = {short_hint or '让用户补充'}。\n"
        "词表：慢慢/低速=35，正常=50，快点=70，全速=100；"
        "往前/向北=angle 0，往右/向东=90，往后/向南=180，往左/向西=270。\n"
        "方向类指令优先用 MOVE + angle + speed；「停」用 STOP。\n"
        "带时长（如三秒、5 秒）时，把毫秒数填进 duration_ms，并在计划末尾补一条 STOP。\n"
        "「去 3 号楼」这类没有坐标的目标点无法执行，返回 say 说明需要补充点位信息。\n"
        "信息不足（没说动作、车号无法判断）时不要编造：plan 给空数组，say 里追问。\n"
        "严禁生成白名单以外的 cmd、严禁编造车号、严禁输出代码或 URL。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": transcript},
    ]
    msg = _chat(cfg, messages, tools=_tools_schema(), max_tokens=600)
    args = None
    for call in (msg.get("tool_calls") or []):
        if call.get("type") == "function" and call.get("function", {}).get("name") == "drive_car":
            raw = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except Exception:
                args = _extract_json(raw)
            break
    if args is None:
        args = _extract_json(msg.get("content") or "")
    if not isinstance(args, dict):
        # 模型没走工具调用而是回了一段追问/说明：交给上层当成"需要澄清"，别报成服务故障
        content = _clean_text(msg.get("content") or "")
        if content:
            return {"device_id": "", "plan": [], "say": content[:200]}
        raise AIError("模型没有返回结构化指令（请换一句更明确的话重试）")
    return args


def normalize(args: dict, cfg: dict, devices: dict, resolve,
              whitelist=CMD_WHITELIST) -> tuple:
    """③ 复核模型输出：车号、白名单、参数范围、步数、时长。返回 (did, plan, say, error)"""
    if not isinstance(args, dict):
        return None, None, None, "模型输出不是对象"

    raw_did = str(args.get("device_id") or "").strip()
    candidates = [raw_did]
    m = re.search(r"(\d+)", raw_did)
    if m:
        n = int(m.group(1))
        candidates += [f"car_{n:03d}", f"car_{n}", str(n)]
    did = None
    for c in candidates:
        if c:
            did = resolve(c)
            if did:
                break
    if not did or did not in devices:
        return None, None, None, (f"车号无法对应到已注册设备: {raw_did or '(空)'}；"
                                  f"可用: {_device_options(devices)}")

    raw_plan = args.get("plan")
    if not isinstance(raw_plan, list) or not raw_plan:
        return None, None, None, "没解析出可执行动作，请说得更具体（例如：让1号车往前开三秒然后停）"
    if len(raw_plan) > cfg["max_plan_steps"]:
        return None, None, None, f"命令步数 {len(raw_plan)} 超过上限 {cfg['max_plan_steps']}"

    plan = []
    for i, step in enumerate(raw_plan):
        if not isinstance(step, dict):
            return None, None, None, f"第 {i+1} 步不是对象"
        cmd = str(step.get("cmd") or "").strip().upper()
        if cmd not in whitelist:
            return None, None, None, f"第 {i+1} 步命令 {cmd!r} 不在白名单，整单拒绝"
        item = {"cmd": cmd}

        if cmd != "STOP":
            speed = step.get("speed", 50)
            if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not (0 <= speed <= 100):
                return None, None, None, f"第 {i+1} 步 speed 越界: {speed!r}（需 0~100）"
            item["speed"] = int(speed)
        if cmd == "MOVE":
            angle = step.get("angle")
            if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not (0 <= angle <= 360):
                return None, None, None, f"第 {i+1} 步 MOVE 缺少合法 angle（0~360）"
            item["angle"] = float(angle)

        dur = step.get("duration_ms", 0) or 0
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or dur < 0:
            return None, None, None, f"第 {i+1} 步 duration_ms 非法: {dur!r}"
        if dur > cfg["max_duration_ms"]:
            return None, None, None, (f"第 {i+1} 步时长 {int(dur)}ms 超过上限 "
                                      f"{cfg['max_duration_ms']}ms")
        item["duration_ms"] = int(dur)
        plan.append(item)

    say = str(args.get("say") or "").strip()[:200]
    return did, plan, say, None
