# V1 · 智能巡检车 TCP 直连版

> 三代正式版本中的第一代。APP 通过 **TCP 长连接直接控制模拟小车**，中间没有云平台、没有业务后端。
> 对应《V3-开发路线图》里的 V1（TCP 直连，定版归档），位于本仓库的 `01-TCP-V1/` 目录。

## 这个版本在练什么

- 一条 JSON 消息占一行（换行分帧），解决 TCP 字节流没有消息边界的问题
- 客户端 / 服务端的角色分工：APP 是 TCP 客户端，小车是 TCP 服务端
- 20Hz 运动学积分：`move(vx, vy)` → 位置、朝向、速度、电量
- 浏览器俯视图：HTTP 提供 `/state` 状态接口

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `simulator/car_server.py` | 模拟小车：TCP 9000 + HTTP 8080 + 运动模拟 |
| `simulator/acceptance_test.py` | 自底向上的分层验收脚本 |
| `simulator/topview.html` | 浏览器俯视图页面 |
| `app/` | HarmonyOS APP 工程（DevEco 打开），核心是 `entry/src/main/ets/net/CarTcpClient.ets` 与 `pages/Index.ets` |
| `docs/项目分层验收方案.md` | 五层验收顺序与检查项 |
| `docs/knowledge-cards/` | 课堂知识卡（线程、C/S、TCP/HTTP、WebSocket、Canvas、协议） |

## 运行环境

Python 3（只用标准库，无需 pip 安装）；APP 端需要 DevEco Studio。

## 快速开始

```bash
# 1) 启动模拟小车（TCP 9000 + 俯视图 8080）
cd simulator
python car_server.py

# 2) 浏览器打开俯视图
#    http://127.0.0.1:8080/        状态接口：/state

# 3) 分层验收（另开一个终端）
python acceptance_test.py
```

APP 端：用 DevEco 打开 `app/`，运行到虚拟手机，连接地址填 `10.0.2.2`、端口 `9000`；实体真机填 PC 的局域网 IP。

> `10.0.2.2` 是 DevEco 模拟器访问宿主机 PC 的专用别名，不是密钥，也不是真机地址。

## 端口与协议

| 端口 | 用途 | 协议 |
| --- | --- | --- |
| 9000 | APP ↔ 小车 | TCP 长连接，JSON + `\n` 分帧 |
| 8080 | 俯视图 | HTTP `GET /state` |

主要消息：

```json
{"type":"cmd","action":"move","vx":0.5,"vy":-1.0}
{"type":"cmd","action":"stop"}
{"type":"ping"}          -> {"type":"pong"}
{"type":"state","x":..,"y":..,"theta":..,"speed":..,"battery":..}
```

## 与后两个版本的关系

V1 定义的 `Protocol.ets`（`move` / `stop` 与 `vx/vy` 语义）被 V2、V3 完整继承。V2 只是把「APP 直连小车」换成「APP → PC 后端 → 华为云 IoTDA → 小车」；V3 再把单车扩成车队并加业务系统。**协议语义一直没变，变的是中间那一层。**

| 版本 | 目录 | 关键变化 |
| --- | --- | --- |
| V1 | 本仓库 `01-TCP-V1` | 直连、无云、单车 |
| V2 | `02-Cloud-V2` | 加华为云 IoTDA 与应用侧 SDK |
| V3 | `03-Fleet-V3` | N 车 + 网页 + 后台 + 轨迹 + AI |

## 上传 GitHub 的安全说明

- 本仓库**不含任何云凭据**（V1 本来就不用云）。
- `app/build-profile.json5` 的本机签名配置（`keyPassword` / `storePassword` / `.p12` 路径）已清空为 `"signingConfigs": []`；DevEco 首次打开时会自动生成自己的签名。
- `local.properties`（本机 SDK 路径）、`.idea/`、`.hvigor/`、`build/`、`oh_modules/`、`*.hap`、数据库与日志都未收录，也已写入 `.gitignore`。
- 推送前可在上层目录执行 `python verify_before_push.py` 做一次静态复检。

## 许可

教学示例项目，仅用于课程演示与学习。
