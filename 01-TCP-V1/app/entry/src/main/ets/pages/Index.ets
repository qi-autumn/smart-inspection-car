/**
 * 模拟小车操控演示页（摇杆版）
 *
 * 功能：
 *  1. 输入服务端 IP/端口，建立/断开 TCP 长连接
 *  2. 圆盘摇杆：拖动控制任意方向（0~360°），拖动距离映射速度，松开停车
 *  3. 盘中央小车图形随朝向旋转，实时显示指令角与服务端回传朝向
 *  4. 实时显示小车回传状态（坐标/朝向/速度/电量）与收发日志
 */
import { CarTcpClient } from '../net/CarTcpClient';
import { CloudClient } from '../net/CloudClient';
import { CarState, angleToVector } from '../model/Protocol';

// 摇杆尺寸常量（vp）
const DISC_SIZE = 280;          // 圆盘直径
const KNOB_SIZE = 56;           // 摇杆球直径
const MAX_RADIUS = DISC_SIZE / 2 - KNOB_SIZE / 2 - 6;  // 球心最大偏移
const DEAD_ZONE = 8;            // 死区（vp），避免轻微触碰误发指令
const SEND_INTERVAL_MS = 80;    // 指令发送最小间隔（限流）

@Entry
@Component
struct Index {
  @State host: string = '10.0.2.2';   // 模拟器访问宿主机 PC 的地址
  @State port: string = '9000';
  @State connected: boolean = false;
  @State logText: string = '';
  @State carX: number = 0;
  @State carY: number = 0;
  @State carTheta: number = 0;
  @State carSpeed: number = 0;
  @State carBattery: number = 100;

  // 云端模式（第三阶段：APP → PC后端 → 华为云IoTDA → 小车）
  @State cloudMode: boolean = false;
  @State devShort: string = 'car_001';   // 云端模式下控制哪台车
  @State cloudOnline: boolean = false;

  // 摇杆状态
  @State knobX: number = 0;           // 球相对盘心的偏移
  @State knobY: number = 0;
  @State dragging: boolean = false;
  @State cmdAngle: number = 0;        // 当前指令角（0~360，0°=正北顺时针）
  @State cmdSpeedPct: number = 0;     // 当前指令速度百分比

  private client: CarTcpClient = new CarTcpClient();
  private cloudClient: CloudClient = new CloudClient();
  private lastSendMs: number = 0;
  private warnedNotConnected: boolean = false;

  aboutToAppear(): void {
    this.client.onConnected = () => {
      this.connected = true;
      this.appendLog('已连接到模拟小车');
    };
    this.client.onDisconnected = (reason: string) => {
      this.connected = false;
      // 断线后摇杆复位（服务端断连会自动停车）
      this.knobX = 0;
      this.knobY = 0;
      this.dragging = false;
      this.cmdSpeedPct = 0;
      this.appendLog(`连接断开：${reason}`);
    };
    this.client.onState = (s: CarState) => {
      this.carX = s.x;
      this.carY = s.y;
      this.carTheta = s.theta;
      this.carSpeed = s.speed;
      this.carBattery = s.battery;
    };
    this.cloudClient.onState = (s: CarState) => {
      this.carX = s.x;
      this.carY = s.y;
      this.carTheta = s.theta;
      this.carSpeed = s.speed;
      this.carBattery = s.battery;
      this.cloudOnline = s.online;
    };
    this.cloudClient.onConnected = () => {
      this.connected = true;
      this.appendLog(`已连接云端后端（设备 ${this.devShort}）`);
    };
    this.cloudClient.onDisconnected = (reason: string) => {
      this.connected = false;
      this.cloudOnline = false;
      this.appendLog(`云端连接断开：${reason}`);
    };
  }

  private appendLog(msg: string): void {
    const time = new Date();
    const ts = `${time.getHours().toString().padStart(2, '0')}:${time.getMinutes().toString().padStart(2, '0')}:${time.getSeconds().toString().padStart(2, '0')}`;
    this.logText = `[${ts}] ${msg}\n` + this.logText;
    if (this.logText.length > 2000) {
      this.logText = this.logText.substring(0, 2000);
    }
  }

  /** 切换 TCP 直连 / 云端模式（连接中先断开） */
  private onModeToggle(on: boolean): void {
    if (this.connected) {
      this.onDisconnectClick();
      this.connected = false;
    }
    this.cloudMode = on;
    this.port = on ? '9090' : '9000';
    this.appendLog(on
      ? '已切换到云端模式（APP→后端→华为云IoTDA→小车）'
      : '已切换到 TCP 直连模式');
  }

  private async onConnectClick(): Promise<void> {
    try {
      if (this.cloudMode) {
        await this.cloudClient.connect(this.host, parseInt(this.port), this.devShort);
      } else {
        await this.client.connect(this.host, parseInt(this.port));
      }
    } catch (e) {
      this.appendLog(`连接失败：${JSON.stringify(e)}`);
    }
  }

  private async onDisconnectClick(): Promise<void> {
    if (this.cloudMode) {
      this.cloudClient.disconnect('主动断开');
    } else {
      await this.client.disconnect('主动断开');
    }
  }

  /** 旋转显示角：拖动时显示指令角，空闲时显示服务端回传朝向 */
  private getDisplayAngle(): number {
    return this.dragging ? this.cmdAngle : this.carTheta;
  }

  /**
   * 摇杆触摸处理：把触点换算为角度 + 速度并限流发送
   * 触点坐标相对圆盘组件左上角，盘心 = (DISC_SIZE/2, DISC_SIZE/2)
   * 屏幕坐标系 y 向下；协议约定 0°=正北（上），顺时针为正：
   *   angle = atan2(dx, -dy)，speed = min(1, r / MAX_RADIUS)
   */
  private onJoystickTouch(event: TouchEvent): void {
    if (event.type === TouchType.Up || event.type === TouchType.Cancel) {
      // 松手：球保持当前位置，小车按设定方向持续行驶（由「停车」按钮结束）
      this.dragging = false;
      return;
    }
    if (event.type !== TouchType.Down && event.type !== TouchType.Move) {
      return;
    }
    if (!this.connected) {
      if (!this.warnedNotConnected) {
        this.appendLog('请先连接小车再拖动摇杆');
        this.warnedNotConnected = true;
      }
      return;
    }
    const touch = event.touches[0];
    const cx = DISC_SIZE / 2;
    const cy = DISC_SIZE / 2;
    let dx = touch.x - cx;
    let dy = touch.y - cy;
    const r = Math.sqrt(dx * dx + dy * dy);
    // 限幅：球心不出盘
    if (r > MAX_RADIUS) {
      dx = dx / r * MAX_RADIUS;
      dy = dy / r * MAX_RADIUS;
    }
    this.knobX = dx;
    this.knobY = dy;

    const clampedR = Math.min(r, MAX_RADIUS);
    if (clampedR < DEAD_ZONE) {
      return; // 死区内不发送
    }
    let deg = Math.atan2(dx, -dy) * 180 / Math.PI;
    if (deg < 0) {
      deg += 360;
    }
    this.cmdAngle = deg;
    const ratio = Math.min(1, clampedR / MAX_RADIUS);
    this.cmdSpeedPct = Math.round(ratio * 100);

    // 限流：SEND_INTERVAL_MS 内不重复发送
    const now = Date.now();
    if (now - this.lastSendMs < SEND_INTERVAL_MS) {
      return;
    }
    this.lastSendMs = now;
    this.dragging = true;
    if (this.cloudMode) {
      const vec = angleToVector(deg, ratio);
      this.cloudClient.sendMove(vec[0], vec[1]).catch((e: Object) => {
        this.appendLog(`move 发送失败：${JSON.stringify(e)}`);
      });
    } else {
      this.client.sendMoveByAngle(deg, ratio);
    }
  }

  /** 停车：发 stop 指令 + 摇杆球复位 */
  private async onStopClick(): Promise<void> {
    if (!this.connected) {
      return;
    }
    if (this.cloudMode) {
      await this.cloudClient.sendStop();
    } else {
      await this.client.sendStop();
    }
    this.knobX = 0;
    this.knobY = 0;
    this.cmdSpeedPct = 0;
    this.appendLog('→ stop（手动停车，摇杆复位）');
  }

  /** 小车俯视图形：指向正北（上），随 displayAngle 旋转 */
  @Builder
  carShape() {
    Column({ space: 2 }) {
      // 前轮
      Row({ space: 24 }) {
        Rect({ width: 11, height: 20 }).fill('#22222A').radius(3)
        Rect({ width: 11, height: 20 }).fill('#22222A').radius(3)
      }
      // 车身
      Stack({ alignContent: Alignment.TopStart }) {
        Rect({ width: 52, height: 62 })
          .fill('#534AB7')
          .radius(12)
        Rect({ width: 36, height: 14 })   // 前挡风
          .fill('#BFE3FF')
          .radius(3)
          .offset({ x: 8, y: 9 })
        Rect({ width: 30, height: 22 })   // 车顶舱
          .fill('#3D3690')
          .radius(4)
          .offset({ x: 11, y: 27 })
        Row({ space: 8 }) {               // 前大灯
          Rect({ width: 8, height: 4 }).fill('#FFE066').radius(2)
          Rect({ width: 8, height: 4 }).fill('#FFE066').radius(2)
        }.offset({ x: 14, y: 2 })
      }
      // 后轮
      Row({ space: 24 }) {
        Rect({ width: 11, height: 20 }).fill('#22222A').radius(3)
        Rect({ width: 11, height: 20 }).fill('#22222A').radius(3)
      }
    }
    .rotate({
      x: 0, y: 0, z: 1,
      angle: this.getDisplayAngle(),
      centerX: '50%',
      centerY: '50%'
    })
  }

  build() {
    Column({ space: 12 }) {
      Text('智能巡检车 · 摇杆操控')
        .fontSize(20)
        .fontWeight(FontWeight.Bold)

      // ---- 模式切换（TCP直连 / 云端） ----
      Row({ space: 8 }) {
        Text('TCP直连').fontSize(13).fontColor('#5F5E5A')
        Toggle({ type: ToggleType.Switch, isOn: this.cloudMode })
          .selectedColor('#534AB7')
          .onChange((on: boolean) => { this.onModeToggle(on); })
        Text('云端模式').fontSize(13).fontWeight(FontWeight.Bold).fontColor('#534AB7')
        if (this.cloudMode) {
          Blank()
          Button('car_001')
            .height(30).fontSize(12)
            .backgroundColor(this.devShort === 'car_001' ? '#534AB7' : '#8A86A8')
            .enabled(!this.connected)
            .onClick(() => { this.devShort = 'car_001'; })
          Button('car_002')
            .height(30).fontSize(12)
            .backgroundColor(this.devShort === 'car_002' ? '#534AB7' : '#8A86A8')
            .enabled(!this.connected)
            .onClick(() => { this.devShort = 'car_002'; })
        }
      }
      .width('100%')

      // ---- 连接设置 ----
      Row({ space: 8 }) {
        TextInput({ text: this.host, placeholder: this.cloudMode ? '后端 IP（PC 局域网地址）' : '服务端 IP' })
          .layoutWeight(2)
          .height(40)
          .enabled(!this.connected)
          .onChange((v: string) => { this.host = v; })
        TextInput({ text: this.port, placeholder: '端口' })
          .layoutWeight(1)
          .height(40)
          .type(InputType.Number)
          .enabled(!this.connected)
          .onChange((v: string) => { this.port = v; })
        if (this.connected) {
          Button('断开')
            .height(40)
            .backgroundColor('#A32D2D')
            .onClick(() => { this.onDisconnectClick(); })
        } else {
          Button('连接')
            .height(40)
            .backgroundColor('#0F6E56')
            .onClick(() => { this.onConnectClick(); })
        }
      }
      .width('100%')

      // ---- 圆盘摇杆 ----
      Stack() {
        // 外圈刻度方位字
        Text('N').fontSize(14).fontWeight(FontWeight.Bold).fontColor('#534AB7')
          .offset({ x: 0, y: -DISC_SIZE / 2 + 10 })
        Text('E').fontSize(14).fontWeight(FontWeight.Bold).fontColor('#B4B2A9')
          .offset({ x: DISC_SIZE / 2 - 16, y: 0 })
        Text('S').fontSize(14).fontWeight(FontWeight.Bold).fontColor('#B4B2A9')
          .offset({ x: 0, y: DISC_SIZE / 2 - 22 })
        Text('W').fontSize(14).fontWeight(FontWeight.Bold).fontColor('#B4B2A9')
          .offset({ x: -DISC_SIZE / 2 + 14, y: 0 })

        // 圆盘底盘
        Circle({ width: DISC_SIZE, height: DISC_SIZE })
          .fill(this.connected ? '#F3F1FB' : '#EFEEE9')
          .stroke(this.connected ? '#534AB7' : '#B4B2A9')
          .strokeWidth(3)

        // 方向引导十字线
        Column()
          .width(1)
          .height(DISC_SIZE - 24)
          .backgroundColor('#D9D6EE')
        Row()
          .width(DISC_SIZE - 24)
          .height(1)
          .backgroundColor('#D9D6EE')

        // 盘心小车图形
        this.carShape()

        // 摇杆球（跟随手指，松手后停留在设定位置）
        Circle({ width: KNOB_SIZE, height: KNOB_SIZE })
          .fill(this.dragging ? '#534AB7' : '#7F77DD')
          .stroke('#FFFFFF')
          .strokeWidth(2)
          .shadow({ radius: 6, color: '#40000000', offsetX: 0, offsetY: 2 })
          .opacity(0.85)
          .translate({ x: this.knobX, y: this.knobY })
      }
      .width(DISC_SIZE)
      .height(DISC_SIZE)
      .onTouch((event: TouchEvent) => { this.onJoystickTouch(event); })

      // ---- 角度显示 ----
      Row({ space: 20 }) {
        Text(`指令角 ${this.cmdAngle.toFixed(0)}°`)
          .fontSize(16)
          .fontWeight(FontWeight.Bold)
          .fontColor('#534AB7')
        Text(`朝向 ${this.carTheta.toFixed(0)}°`)
          .fontSize(16)
          .fontWeight(FontWeight.Bold)
          .fontColor('#1D9E75')
        Text(`速度 ${this.cmdSpeedPct}%`)
          .fontSize(16)
          .fontColor('#5F5E5A')
        Button('停车')
          .height(34)
          .backgroundColor('#A32D2D')
          .onClick(() => { this.onStopClick(); })
      }
      Text('拖动摇杆设定方向和速度，松手后保持行驶；按「停车」结束并使摇杆复位')
        .fontSize(11)
        .fontColor('#888780')

      // ---- 小车状态面板 ----
      Row({ space: 16 }) {
        if (this.cloudMode) {
          Text(`设备: ${this.devShort}`)
            .fontSize(13).fontWeight(FontWeight.Bold)
          Text(this.cloudOnline ? '在线' : '离线')
            .fontSize(13)
            .fontColor(this.cloudOnline ? '#1D9E75' : '#A32D2D')
            .fontWeight(FontWeight.Bold)
        }
        Text(`坐标: (${this.carX.toFixed(2)}, ${this.carY.toFixed(2)})`).fontSize(13)
        Text(`速度: ${this.carSpeed.toFixed(2)} m/s`).fontSize(13)
        Text(`电量: ${this.carBattery}%`).fontSize(13)
      }
      .width('100%')
      .padding(10)
      .borderRadius(8)
      .backgroundColor('#EEEDFE')

      // ---- 日志 ----
      Text(this.logText === '' ? '日志区：连接后拖动摇杆，收发信息将显示在此' : this.logText)
        .fontSize(12)
        .fontColor('#5F5E5A')
        .width('100%')
        .layoutWeight(1)
        .padding(8)
        .borderRadius(8)
        .backgroundColor('#F1EFE8')
        .textAlign(TextAlign.Start)
    }
    .padding(16)
    .width('100%')
    .height('100%')
    .alignItems(HorizontalAlign.Center)
  }
}
