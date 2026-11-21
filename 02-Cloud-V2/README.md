# V2 · 智能巡检车 华为云 IoTDA 云控版

> 三代正式版本中的第二代：**APP → PC 桥接后端 → 华为云 IoTDA → 小车**。
> 车端以 MQTT 设备身份接入（TLS 8883），后端用应用侧 SDK 下发命令、查询设备影子。
> 本版本已冻结，作为教学基准；V3 的云侧物模型就是直接复用这里的。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `backend/cloud_backend.py` | 单车云端桥接后端（HTTP 9090），接口形状与 V1 保持一致 |
| `backend/cloud_backend_dual.py` | 双车防撞实验（HTTP 9091），预测 3 秒轨迹 + 自动双车急停 |
| `backend/cloud_backend_fleet.py` | N 车动态发现与全对全防撞（V2 内部第四阶段），兼容旧接口 |
| `backend/car_mqtt_client_001.py` / `_002.py` | 车端 MQTT 客户端：属性上报、命令订阅、命令响应 |
| `backend/car_simulator.py` | 多车模拟器 + 云端自动注册设备 |
| `web/topview.html`、`topview_multi.html`、`playback.html` | 单车/多车俯视图、轨迹回放 |
| `profile/Car001-产品模型.zip` | IoTDA 产品模型（属性 `car_status` / 命令服务 `car_control`） |
| `app/` | HarmonyOS APP，带「TCP 直连 / 云端模式」开关（`net/CloudClient.ets`） |
| `set_env.example.bat` | 环境变量模板（**复制后填写，不要提交填好的文件**） |

## 第一次运行：填自己的凭据（必读）

本仓库**不含任何真实密钥**。所有 AK/SK 与设备密钥都从环境变量读取，代码里只剩占位符。

1. 把 `set_env.example.bat` 复制成 `set_env.bat`，填入你自己的值；该文件已被 `.gitignore` 忽略，永远不会提交。
2. 在 CMD 里执行 `set_env.bat` 后再启动后端（PowerShell 用 `$env:NAME = "值"`）。
3. 需要设置的环境变量：

   | 变量 | 用途 |
   | --- | --- |
   | `HUAWEICLOUD_SDK_AK` | 华为云访问密钥 AK |
   | `HUAWEICLOUD_SDK_SK` | 华为云访问密钥 SK |
   | `HUAWEICLOUD_SDK_PROJECT_ID` | 可选，默认读 `CONFIG["project_id"]` |
   | `IOTDA_DEVICE_SECRET_001` / `_002` | 两台设备的 MQTT 密钥 |
   | `IOTDA_DEVICE_SECRET` | `car_simulator.py` 自动注册新设备时的默认密钥 |

4. 把代码里的 `YOUR_PRODUCT_ID`、`YOUR_PROJECT_ID`、`YOUR_APP_ENDPOINT`、`YOUR_MQTT_HOST` 换成你自己实例的对应值（这些是占位符，不是密钥）。设备 ID 格式为 `{product_id}_{node_id}`。

> 密钥建议只在本地环境变量里出现，不要写回源码、不要写进 commit。

## 运行环境

```bash
pip install huaweicloudsdkcore huaweicloudsdkiotda paho-mqtt
```

APP 端需要 DevEco Studio。

## 快速开始

```bash
# 1) 车端先上线（可另开终端跑 _002.py）
python backend/car_mqtt_client_001.py

# 2) 启动桥接后端（9090）
python backend/cloud_backend.py

# 3) 接口自检 / 俯视图
#    http://127.0.0.1:9090/api/devices
#    http://127.0.0.1:9090/api/state?dev=car_001
```

APP 端：DevEco 打开 `app/`，在界面切到「云端模式」，地址 `10.0.2.2`、端口 `9090`（切模式会自动改端口）。切回「TCP 直连」则连 V1 的 `car_server.py:9000`。

三个后端的端口：`cloud_backend.py` 9090、`cloud_backend_dual.py` 9091、`cloud_backend_fleet.py` 9090（与前两者分开运行，别同时占用同一端口）。

## 架构要点

```
APP ──HTTP── PC 桥接后端 ──应用侧 SDK(HMAC 签名)── 华为云 IoTDA
                                                      │ MQTT/TLS 8883
                                                      ▼
                                                  小车（MQTT 客户端）
```

- V1 里 APP 是 TCP 客户端、小车是 TCP 服务端；到 V2 **角色翻转**：小车变成主动连云的 MQTT 客户端。这是本课程最重要的架构对比点。
- 命令下发 `CreateCommand` + `DeviceCommandRequest`，服务 ID 必须是 `car_control`；属性上报用 `car_status`。
- 影子响应结构是 `response.shadow[].reported.properties`，数值为 `Decimal`，取值时注意类型转换。

## 与 V3 的关系

V3 车队版**零改动**复用了这里的物模型、MQTT 接入方式与应用侧 SDK，只把业务层扩展成 N 车 + Flask 后端 + 网页/后台/AI。所以本仓库是理解 V3 云端部分的基准。

## 上传 GitHub 的安全说明

- 所有 AK/SK、设备密钥已替换为 `os.environ.get(...)`，账号专属 ID 与接入域名已占位化。
- `app/build-profile.json5` 的本机签名配置已清空；`local.properties`、`.idea/`、`.hvigor/`、`build/`、`oh_modules/`、`*.hap`、`fleet.db*` 均未收录或已被 `.gitignore` 忽略。
- 推送前在上层目录执行 `python verify_before_push.py` 复检。

## 许可

教学示例项目，仅用于课程演示与学习。
