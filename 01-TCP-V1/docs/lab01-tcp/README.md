# Lab01-TCP - 模拟车服务端（课堂单元任务书）

> 课堂实战项目：APP 与模拟小车 TCP 直连。每单元"开发 → 测试 → 绿灯 → 下一站"。
> 代码按 step 编号分步重写，每步文件头部写明教学目标与验收方法。

## 角色与架构

```
鸿蒙 APP（DevEco 模拟器，TCP 客户端，主动连接 10.0.2.2:8888）
        │  TCP :8888 —— 文本帧：指令 / 回执 / 状态
        ▼
car_server.py（小车端，TCP 服务端，监听 0.0.0.0:8888，代替真车）
        │  WebSocket :8889 —— 状态 JSON 推送（每 100ms）
        ▼
浏览器 car_view.html —— Canvas 俯视图，"只看不控"观察端

test_client.py —— 开发期替身：站在 APP 的位置先验收车端
```

- 称呼规范：**小车端** = 服务端（等人来连）；**APP 端** = TCP 客户端；单独说"客户端"指 test_client.py。
- 关键坑：模拟器里 `10.0.2.2` 才是宿主机 PC，`127.0.0.1` 是模拟器自己。

## 开发路线（7 单元）

| 单元 | 产出 | 测试与验收 |
|---|---|---|
| 1. 车端最小服务 | `server/step1_echo_server.py` + `step1_test_client.py` | 双终端回声一致；观察单客户端阻塞现象 |
| 2. 协议版车端 | `server/step2_command_server.py` | 双客户端并发互不干扰；PING/CMD/ACK/ERR 全对 |
| 3. 小车动起来 | `server/step3_state_server.py` | STATE 持续广播且坐标变化；`test_client.py <IP> --auto` 全绿 |
| 4. 双服务完整版 | `server/car_server.py`（最终版，封版） | step3 测试回归仍绿；浏览器能连上 8889 |
| 5. 浏览器俯视图 | `web/car_view_step1/step2.html` → `car_view.html` | Canvas 坐标系检查；按键控车轨迹正确 |
| 6. 鸿蒙 APP | DevEco 工程（教师建）+ ArkTS 代码 | 先连通打日志，再 UI 操控，车端日志隔离验证 |
| 7. 集成验收 | 完整项目回归 + 排错表 | checklist：控车/回执/轨迹/断线重连/非法输入/端口占用/防火墙 |

单元 4 之后车端代码封版，后续单元只消费不修改（协议契约纪律）。

## 协议速查（文本帧 · UTF-8 · 一帧一行以 \n 结尾）

| 方向 | 帧 | 说明 |
|---|---|---|
| APP→车 | `PING` | 心跳，回 `PONG` |
| APP→车 | `CMD:FORWARD[:80]` / `CMD:BACK[:40]` | 前进/后退，速度 0~100，默认 60 |
| APP→车 | `CMD:LEFT` / `CMD:RIGHT` | 原地转向，每次 ±15° |
| APP→车 | `CMD:STOP` / `CMD:STATUS` | 停止 / 立即回状态 |
| 车→APP | `ACK:FORWARD:80` 等 | 指令回执（已收到并执行） |
| 车→APP | `STATE:x=400,y=300,heading=0,speed=60,battery=87` | 每 100ms 广播（单元 3 起） |
| 车→APP | `ERR:UNKNOWN_CMD:FOO` / `ERR:BAD_FRAME:xxx` | 非法指令 |

坐标系约定（三处必须一致）：场地 800×600，(0,0) 左上角，heading 0°=朝上、顺时针增大。

## 运行方式

```bash
cd server
python step1_echo_server.py        # 单元 1
python step1_test_client.py 127.0.0.1
python car_server.py               # 单元 4 起跑这份（TCP 8888 + WebSocket 8889）
```

> 车端自单元 4 起封版：后面所有单元只消费、不再修改 car_server.py。

### test_client.py 的四种模式（IP 在前，开关顺序随意）

```bash
python test_client.py 127.0.0.1              # 交互：收发全开，STATE 实时滚动（单终端调试）
python test_client.py 127.0.0.1 --auto       # 自测：11 步指令序列，回归验收
python test_client.py 127.0.0.1 --monitor    # 监视屏：只收不发，专职打印 STATE（投影大屏）
python test_client.py 127.0.0.1 --ctrl       # 控制台：主发轻收，STATE 静默丢弃只回 ACK/ERR
```

典型组合：终端 A 跑 `--monitor` 当位置大屏，终端 B 跑 `--ctrl` 发指令——指令不再被广播刷屏。
注意：没有"纯只发"模式——完全不 recv 会堆满接收缓冲区、反压服务端；**收可以不看，但不能不收**。

### 浏览器俯视图（单元 5）

```bash
# 先启动车端，再双击（或浏览器打开）对应页面，建议按 step 顺序教学：
web/car_view_step1.html   # 静态：Canvas 画车 + heading 滑块，讲坐标系三处一致（不联网）
web/car_view_step2.html   # 实时：WebSocket 8889 接入，小车随车端状态移动
web/car_view.html         # 最终版：+ 轨迹渐隐 + HUD 数字面板 + 断线 2 秒自动重连
```

环境：纯标准库即可跑通单元 1~3；单元 4 需 `pip install websockets`。
