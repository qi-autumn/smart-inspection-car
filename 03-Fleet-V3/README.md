# V3 · 智能巡检车队平台

> 三代正式版本中的第三代：**N 台车 + 实时监控大屏 + 管理后台 + 轨迹回放 + AI 自然语言控车**。
> 客户端（网页 / APP）只连 PC 上的业务后端 `:8000`，华为云密钥全部收在后端手里。
> 云侧物模型与 MQTT 协议沿用 V2，零改动。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `server/app.py` | Flask 业务后端（8000）：L1 接口、WebSocket、状态缓存、命令排程 |
| `server/ai_agent.py` | 自然语言控车：ASR → 结构化解析 → 白名单校验 → 指令规范化 |
| `server/track_store.py` | SQLite 轨迹库（WAL，变化才插行） |
| `server/drive_cars.py` | 造演示轨迹数据的脚本 |
| `server/test_api.py` | 35 项接口自测 |
| `server/*.example.json` | **配置模板**（不含密钥，见下） |
| `device/car_mqtt_client.py` | 通用 MQTT 小车模拟器（`--device car_001`） |
| `device/verify_step1.py` | 第①步验收脚本 |
| `web/` | `monitor.html` 大屏、`admin.html` 后台、`ai_chat.html` AI、`fleet_console.html`、`track_demo.html` |
| `docs/` | 开发路线图、接口协议、接口文档与 16+ 份 HTML 讲解、DAYU200 实拍截图 |
| `app/` | CarFleet ArkTS APP（选车、摇杆、大屏、后台、AI 内嵌视图） |
| `setup.py` | 从模板生成 3 个本地配置文件 |
| `start_*.bat` | 小车模拟器与后端启动脚本 |

## 第一次运行：生成并填写本地配置（必做一次）

```bash
python setup.py
```

它会从 `*.example.json` 生成下面三个文件（已在 `.gitignore` 中，**绝不能提交**）：

| 文件 | 要填什么 |
| --- | --- |
| `server/cloud_config.json` | 华为云 `ak` / `sk` / `project_id` / `endpoint` / `mqtt_host` |
| `server/devices.json` | 每台车的 `device_id`（`{product_id}_{node_id}`）与 `secret`、出生点、颜色、调试端口 |
| `server/ai_config.json` | 大模型 `api_key`（也可用环境变量，不写文件） |

支持环境变量覆盖（推荐，避免改文件）：

```
HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK
V3_AI_BASE_URL / V3_AI_API_KEY / V3_AI_MODEL
```

模板里所有 `YOUR_PRODUCT_ID`、`YOUR_DEVICE_SECRET_00X`、`YOUR_AI_API_KEY` 等都是占位符，换成自己的即可。

## 运行环境

```bash
pip install flask flask-sock paho-mqtt requests huaweicloudsdkcore huaweicloudsdkiotda
```

APP 端需要 DevEco Studio。

## 快速开始

```bash
# 1) 启动 3 台小车模拟器（Windows 可双击 start_all_cars.bat）
python device/car_mqtt_client.py --device car_001
python device/car_mqtt_client.py --device car_002
python device/car_mqtt_client.py --device car_003

# 2) 启动业务后端（Windows 可双击 start_backend.bat）
python server/app.py

# 3) 接口自测
python server/test_api.py
```

APP 端：DevEco 打开 `app/`，虚拟手机地址填 `10.0.2.2`、端口 `8000`；真机填 PC 局域网 IP。

## 验收入口（后端起来后）

| 地址 | 看什么 |
| --- | --- |
| `http://127.0.0.1:8000/monitor/view` | 实时监控大屏 + 摇杆驾驶 |
| `http://127.0.0.1:8000/track/view` | 轨迹回放（按车勾选、按时间窗查询） |
| `http://127.0.0.1:8000/admin/view` | 管理后台，演示账号 `admin` / `admin123` |
| `http://127.0.0.1:8000/ai/view` | 文字 / 语音自然语言控车 |
| `http://127.0.0.1:8000/health` | 在线车数与 AI 是否配置 |
| `WS /ws` | 每秒广播 `fleet_state` |

> ⚠️ `admin / admin123` 是**演示口令**，如果要部署到公网，务必先改掉并加上真正的鉴权。
> 网页语音采集要求 HTTPS 或 `localhost`，局域网 HTTP 访问时浏览器会禁用麦克风（文字控车不受影响）。

## 硬约束与常见坑

- IoTDA S0 免费单元每日约 1 万条消息：属性上报节奏固定 **行驶 5s / 空闲 60s**，不要调快。
- 同一台车的模拟器**只能跑一个实例**，否则 MQTT 互踢会导致位置乱跳、看似刹不住。
- `ListDevices` 的 `device_id` 过滤不可靠，查在线状态请用 `ShowDevice`（`resp.status` 平铺）。
- 8000 端口被旧后端占用时，新页面会读到旧数据；用 `netstat -ano | findstr :8000` 清掉多余进程。

## 与 V1 / V2 的关系

| 层 | V1 | V2 | V3 |
| --- | --- | --- | --- |
| 车端 | TCP 服务端（Python 模拟） | MQTT 设备客户端 | 参数化 MQTT 模拟器（`devices.json` 登记 N 台） |
| 通信 | 裸 TCP + JSON 分帧 | MQTT/TLS 8883 + 影子 + 应用侧 API | 沿用 V2，云侧零改动 |
| 后端 | 无 | PC 桥接后端 9090 | Flask 8000 + SQLite + WebSocket + AI |
| 展示 | 1 个 APP + 俯视图 | APP 云端模式 + 多车俯视图 | 5 个网页 + 后台 + 轨迹 + CarFleet APP |

## 上传 GitHub 的安全说明

- 真实 `ai_config.json` / `cloud_config.json` / `devices.json` **没有放进仓库**，只有 `*.example.json` 模板；生成出来的真实文件已被 `.gitignore` 忽略。
- 文档中的设备 UDID、账号专属 ID 与接入域名已占位化；`app/build-profile.json5` 签名配置已清空。
- `fleet.db*`、`commands.log`、`*.hap`、`*.p12`/`*.p7b`/`*.cer`、`local.properties`、`.idea/`、`.hvigor/`、`build/`、`oh_modules/` 均未收录或已忽略。
- 推送前在上层目录执行 `python verify_before_push.py` 复检。

## 许可

教学示例项目，仅用于课程演示与学习。
