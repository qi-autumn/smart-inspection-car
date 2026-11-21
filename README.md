# 智能巡检车 · 三代演进项目

> 一个仓库里放 **V1 / V2 / V3 三个版本**的完整源码、文档与配置模板。
> 主题是「从单车直连 → 云端接入 → 多车车队」的演进过程；三个版本按目录分隔，各自可独立运行。

**这个仓库只包含源码、文档和配置模板**：真实密钥、设备密钥、签名私钥、数据库、日志、构建产物（`.hap` / `.p12` / `.pem` / `.db` 等）一律不入库。成品如另行分发，见下方 [Release 说明](#release-与版本标记)。

---

## 版本对照

| 目录 | 版本 | 架构与关键技术 | 主要端口 | 状态 |
| --- | --- | --- | --- | --- |
| [`01-TCP-V1/`](./01-TCP-V1/README.md) | V1 · TCP 直连单车版 | APP 与模拟小车 TCP 直连，无云平台；JSON 换行分帧、20Hz 运动学积分、Canvas 俯视图 | 9000 / 8080 | 定版归档 |
| [`02-Cloud-V2/`](./02-Cloud-V2/README.md) | V2 · 华为云 IoTDA 云控版 | APP → PC 桥接后端 → IoTDA → 小车；MQTT/TLS 8883、设备影子、应用侧 SDK | 9090 / 9091 | 冻结为教学基准 |
| [`03-Fleet-V3/`](./03-Fleet-V3/README.md) | V3 · 巡检车队平台 | N 台车 + Flask 业务后端 + SQLite + WebSocket + 网页大屏 / 管理后台 / 轨迹回放 / AI 自然语言控车；**云侧物模型沿用 V2，零改动** | 8000 | 当前版本 |

三代完整关系、目录归属与常见混淆见 [`docs/V3-学-项目版本全景与目录盘点.html`](./docs/V3-学-项目版本全景与目录盘点.html)。

## 目录地图

```
README.md                 本文件：总览与索引
.gitignore                统一忽略规则（整个仓库一份）
verify_before_push.py     上传前静态复检：凭据 / 本机路径 / 禁止产物 / 必备文件
docs/                     跨版本公共讲解（HTML，下载后双击可离线阅读）
01-TCP-V1/                simulator/ · app/ · docs/ · README.md
02-Cloud-V2/               backend/ · web/ · profile/ · app/ · set_env.example.bat
03-Fleet-V3/               server/ · device/ · web/ · docs/ · app/ · setup.py · *.bat
```

## 快速开始

**每个子目录都是独立的工作目录，请先 `cd` 进去再运行命令。**

### V1 · TCP 直连

```bash
cd 01-TCP-V1
cd simulator
python car_server.py          # TCP 9000 + 俯视图 http://127.0.0.1:8080/
# 另开终端：python acceptance_test.py
```

APP：DevEco 打开 `01-TCP-V1/app/`，连接地址填 `10.0.2.2`（模拟器）或 PC 局域网 IP（真机），端口 `9000`。

### V2 · 华为云 IoTDA

```bash
cd 02-Cloud-V2
# 先按 set_env.example.bat 的清单设置环境变量（AK / SK / 设备密钥）
python backend/car_mqtt_client_001.py     # 车端上线
python backend/cloud_backend.py           # 桥接后端 9090
```

APP：DevEco 打开 `02-Cloud-V2/app/`，界面切到「云端模式」，地址 `10.0.2.2`、端口 `9090`。

### V3 · 巡检车队（主版本）

```bash
cd 03-Fleet-V3
python setup.py              # 从 *.example.json 生成本地配置（被 .gitignore 忽略）
# 按自己的账号填好 server/cloud_config.json、devices.json、ai_config.json
python server/app.py         # 后端 8000
```

网页入口：`/monitor/view` 监控大屏、`/track/view` 轨迹回放、`/admin/view` 管理后台、`/ai/view` 自然语言控车。
批量启动三台模拟小车可用 `start_all_cars.bat`，后端可用 `start_backend.bat`。

> 演示账号 `admin` / `admin123` 仅用于本机，公网部署前必须修改。

## 配置与密钥

| 需要自己准备的东西 | 从哪里来 | 是否入库 |
| --- | --- | --- |
| V3 华为云配置 / 设备清单 / AI Key | `03-Fleet-V3/server/*.example.json` → 跑 `python setup.py` 生成本地文件 | ❌ 本地生成，`.gitignore` 忽略 |
| V2 华为云 AK/SK、设备密钥 | 环境变量（清单见 `02-Cloud-V2/set_env.example.bat`） | ❌ 代码里只有占位符 |
| APP 签名证书 | DevEco Studio 自动签名生成 | ❌ `build-profile.json5` 已清空本机签名配置 |

`10.0.2.2` 是 DevEco 模拟器访问宿主机的专用别名，不是密钥。

## 脱敏说明

- 真实 API Key、AK/SK、设备密钥、账号专属 ID、接入域名、设备 UDID 已占位化或移除。
- 所有 Python 源码、`*.example.json`、Markdown、HTML、脚本均通过 `verify_before_push.py` 静态扫描。
- 推送前请执行 `python verify_before_push.py`，看到 `[PASS]` 再继续；脚本只是静态扫描，仍应肉眼确认 `git status` 的文件清单。

## Release 与版本标记

本仓库只有一条 Releases 列表，用 tag 区分三个版本：

| tag | 对应目录 | 建议附件 |
| --- | --- | --- |
| `v1.0.0` | `01-TCP-V1/` | 可不放；或 `v1-tcp_source-snapshot.zip` |
| `v2.0.0` | `02-Cloud-V2/` | 可不放；或 `v2-iotda_profile.zip`（产品模型） |
| `v3.0.0` | `03-Fleet-V3/` | `v3-fleet_unsigned.hap`（未签名）+ 已知限制说明 / 截图包 |

附件命名统一带版本前缀，发布说明里写清对应目录、适用 SDK / 板子版本与 SHA256。

> ⚠️ 公开仓库的 Release 附件同样是公开的，**不得**上传 `*.p12` 私钥、真实 `devices.json` / `cloud_config.json` / `ai_config.json`，也不得上传含密钥的本地完整包 zip。

## 在 DAYU200 开发板上运行（可选）

本项目**无需刷系统镜像**：板子系统由厂商提供，要做的只是把编译签名好的 `.hap` 应用包用 `hdc install` 装上去（相当于往手机装 APP）。本仓库不提供系统镜像、SDK、`hdc` 或签名工具，也不提供已签名应用包 —— 请用 DevEco 自动签名自行生成。

已知限制：现有 HAP 为 HarmonyOS 6 / API 22 字节码，在实测 API 15 的板子上安装后启动崩溃；签名版 HAP 还绑定特定设备 UDID，不可跨板使用。详见 [`docs/V3-学-把软件装到DAYU200开发板.html`](./03-Fleet-V3/docs/V3-学-把软件装到DAYU200开发板.html) 与上板实录、以及 [`docs/V3-学-固件与烧录工具要不要上传.html`](./docs/V3-学-固件与烧录工具要不要上传.html)。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [`docs/V3-学-项目版本全景与目录盘点.html`](./docs/V3-学-项目版本全景与目录盘点.html) | 三个正式版本、目录归属、三套编号对照 |
| [`docs/V3-学-把项目上传到GitHub.html`](./docs/V3-学-把项目上传到GitHub.html) | Git / GitHub 上传手把手（三仓库方案，命令通用） |
| [`docs/V3-学-固件与烧录工具要不要上传.html`](./docs/V3-学-固件与烧录工具要不要上传.html) | 源码 / 成品 / 第三方工具 / 密钥的分类边界 |
| [`docs/V3-学-单仓库还是三个仓库.html`](./docs/V3-学-单仓库还是三个仓库.html) | 单仓库结构、方案对比、tag 与 Release 组织 |
| [`docs/V3-学-单仓库方案上传GitHub手把手.html`](./docs/V3-学-单仓库方案上传GitHub手把手.html) | **本仓库的上传步骤**：init → push → 3 个 tag → Release 附件 |
| 各版本 `docs/` | 路线图、接口协议、接口文档、16+ 份专题讲解与实拍截图 |

HTML 讲解文件建议下载到本地后双击打开（可离线阅读，含 SVG 图示与自测题）。

## 许可

本仓库未附带 LICENSE 文件。如需开源许可，请自行选择并添加 `LICENSE` 后再公开分发。
