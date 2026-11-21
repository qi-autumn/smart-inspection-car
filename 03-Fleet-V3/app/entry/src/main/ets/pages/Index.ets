/**
 * V3 车队操控 APP（第⑥步 · 网页功能对齐版）
 *
 * 车队页：摘要卡（总数/在线/平均电量）+ 卡片式车辆列表（可进入操控或轨迹）
 * 操控页：实时地图（全部车辆同屏，点车切换）+ 摇杆 + 状态面板
 * 轨迹页：历史轨迹回放（调 /track，多色分车、时间窗过滤）+ 单车详情（点数/里程/最近上报）
 * 全局：Preferences 记住绑定，重启自动恢复
 */
import { FleetClient, FleetCar, TrackPoint } from '../net/FleetClient';
import preferences from '@ohos.data.preferences';
import web_webview from '@ohos.web.webview';

// 摇杆尺寸常量（vp）
const DISC_SIZE = 230;
const KNOB_SIZE = 48;
const MAX_RADIUS = DISC_SIZE / 2 - KNOB_SIZE / 2 - 6;
const DEAD_ZONE = 8;
const SEND_INTERVAL_MS = 80;

// 地图常量
const MAP_SIZE = 300;      // 地图边长（vp）
const MAX_SCALE = 10;      // 最大比例尺：10 vp = 1 米
const EDGE_PAD = 36;       // 地图边缘留白

// 轨迹时间窗选项（分钟）
const WIN_MINUTES: number[] = [10, 60, 360, 1440];
const WIN_LABELS: string[] = ['10分钟', '1小时', '6小时', '24小时'];

// 车辆渲染色池（后端没给 color 时兜底）
const CAR_COLORS: string[] = ['#e24b4a', '#185fa5', '#3aa55d', '#9b59b6',
  '#e67e22', '#16a085', '#d35400', '#8e44ad'];

/** 一台车的轨迹数据（轨迹页用） */
class TrackData {
  deviceId: string = '';
  color: string = '#e24b4a';
  points: TrackPoint[] = [];   // 最近 24h，时间升序
}

@Entry
@Component
struct Index {
  // 视图切换：'list' 车队页 / 'ctrl' 操控页 / 'track' 轨迹页 / 'admin' 管理后台
  @State view: string = 'list';
  // 内嵌 Web 页面：'admin' 管理后台 / 'ai' 第⑦步自然语言控车（文字模式）
  @State adminPage: string = 'admin';

  // 连接参数（Preferences 记忆）
  @State host: string = '10.0.2.2';   // 模拟器访问宿主机 PC 的地址
  @State port: string = '8000';
  @State boundId: string = '';        // 上次绑定的 device_id（重启自动恢复）
  @State connecting: boolean = false;

  // 车队整队状态（FleetClient 每 800ms 刷新）
  @State cars: FleetCar[] = [];
  @State selectedId: string = '';     // 当前控制的车
  @State connected: boolean = false;

  // 地图视图（由车队分布自动计算）
  @State viewScale: number = MAX_SCALE;
  private centerX: number = 0;
  private centerY: number = 0;

  // 摇杆状态
  @State knobX: number = 0;
  @State knobY: number = 0;
  @State dragging: boolean = false;
  @State cmdAngle: number = 0;
  @State cmdSpeedPct: number = 0;

  // 轨迹页状态
  @State tracks: TrackData[] = [];
  @State trackSel: string = 'all';     // 'all' 或 device_id
  @State trackWinMin: number = 60;     // 时间窗（分钟）
  @State trackLoading: boolean = false;
  @State trackStamp: number = 0;       // 变更版本号（驱动图例刷新）
  private canvasReady: boolean = false;
  private settings: RenderingContextSettings = new RenderingContextSettings(true);
  private ctx: CanvasRenderingContext2D = new CanvasRenderingContext2D(this.settings);

  // 管理后台（内嵌 Web）
  private webController: web_webview.WebviewController = new web_webview.WebviewController();

  @State logText: string = '';

  private client: FleetClient = new FleetClient();
  private lastSendMs: number = 0;
  private warnedNotConnected: boolean = false;
  private pref: preferences.Preferences | null = null;

  aboutToAppear(): void {
    this.client.onFleet = (cars: FleetCar[]) => {
      const before = this.cars.length;
      this.cars = cars;
      this.updateMapView();
      // 选中车掉队时自动改选第一辆
      if (this.selectedId !== ''
        && !cars.some((c: FleetCar) => c.deviceId === this.selectedId)) {
        this.selectedId = cars.length > 0 ? cars[0].deviceId : '';
        if (this.selectedId !== '') {
          this.client.bind(this.selectedId);
        }
      }
      if (cars.length !== before) {
        this.appendLog(`车队更新：当前 ${cars.length} 台车`);
      }
    };
    this.client.onConnected = () => {
      this.connected = true;
      this.appendLog(`已绑定 ${this.selectedName()}（${this.shortId(this.selectedId)}）`);
    };
    this.client.onDisconnected = (reason: string) => {
      this.connected = false;
      this.knobX = 0;
      this.knobY = 0;
      this.dragging = false;
      this.cmdSpeedPct = 0;
      this.appendLog(`连接断开：${reason}`);
    };
    this.restorePrefs();
  }

  // ---------------- 工具 ----------------

  private async restorePrefs(): Promise<void> {
    try {
      this.pref = await preferences.getPreferences(getContext(this), 'fleet_bind');
      this.host = (await this.pref.get('host', '10.0.2.2')) as string;
      this.port = (await this.pref.get('port', '8000')) as string;
      this.boundId = (await this.pref.get('device_id', '')) as string;
      if (this.boundId !== '') {
        this.appendLog(`检测到上次绑定：${this.shortId(this.boundId)}，连接后点它继续`);
      }
    } catch (e) {
      this.appendLog('读取本地记忆失败（首次启动属正常）');
    }
  }

  private async savePrefs(): Promise<void> {
    if (this.pref === null) {
      return;
    }
    try {
      await this.pref.put('host', this.host);
      await this.pref.put('port', this.port);
      await this.pref.put('device_id', this.selectedId);
      await this.pref.flush();
    } catch (e) {
    }
  }

  private shortId(fullId: string): string {
    const idx = fullId.indexOf('_');
    return idx >= 0 ? fullId.substring(idx + 1) : fullId;
  }

  /** 车辆序号：6ae6e3ef..._car_003 → 3（必须取 _car_ 之后的数字，前缀里也有数字） */
  private carNo(id: string): number {
    const idx = id.lastIndexOf('_car_');
    const tail = idx >= 0 ? id.substring(idx + 5) : id;
    const n = parseInt(tail, 10);
    return isNaN(n) ? 0 : n;
  }

  private colorOf(c: FleetCar, index: number): string {
    return c.color !== '' ? c.color : CAR_COLORS[index % CAR_COLORS.length];
  }

  private selectedCar(): FleetCar | null {
    for (const c of this.cars) {
      if (c.deviceId === this.selectedId) {
        return c;
      }
    }
    return null;
  }

  private selectedName(): string {
    const c = this.selectedCar();
    return c !== null ? c.name : '未选择';
  }

  private appendLog(msg: string): void {
    const time = new Date();
    const ts = `${time.getHours().toString().padStart(2, '0')}:${time.getMinutes().toString().padStart(2, '0')}:${time.getSeconds().toString().padStart(2, '0')}`;
    this.logText = `[${ts}] ${msg}\n` + this.logText;
    if (this.logText.length > 2000) {
      this.logText = this.logText.substring(0, 2000);
    }
  }

  // ---------------- 车队页动作 ----------------

  private async onConnectClick(): Promise<void> {
    this.connecting = true;
    try {
      this.cars = await this.client.fetchFleet(this.host, parseInt(this.port));
      this.updateMapView();
      this.appendLog(`已连接后端，车队共 ${this.cars.length} 台车（在线 ${this.onlineCount()}）`);
      if (this.boundId !== ''
        && this.cars.some((c: FleetCar) => c.deviceId === this.boundId)) {
        this.appendLog(`上次绑定的 ${this.shortId(this.boundId)} 在车队中，点它继续控制`);
      } else if (this.boundId !== '') {
        this.appendLog(`上次绑定的 ${this.shortId(this.boundId)} 已不在车队，请重新选车`);
        this.boundId = '';
      }
    } catch (e) {
      this.appendLog(`连接失败：${JSON.stringify(e)}（检查后端是否运行、IP 是否正确）`);
    }
    this.connecting = false;
  }

  private onlineCount(): number {
    let n = 0;
    for (const c of this.cars) {
      if (c.online) {
        n++;
      }
    }
    return n;
  }

  private avgBattery(): number {
    let sum = 0;
    let n = 0;
    for (const c of this.cars) {
      if (c.online) {
        sum += c.battery;
        n++;
      }
    }
    return n > 0 ? sum / n : 0;
  }

  /** 点车辆卡片 → 绑定并进入操控页 */
  private async onSelectCar(c: FleetCar): Promise<void> {
    if (!c.online) {
      this.appendLog(`${c.name} 离线，无法控制`);
      return;
    }
    this.selectedId = c.deviceId;
    this.view = 'ctrl';
    this.appendLog(`选择 ${c.name}，正在绑定…`);
    await this.savePrefs();
    if (this.client.isConnected()) {
      this.client.bind(c.deviceId);
      this.appendLog(`已切换控制对象：${c.name}`);
    } else {
      try {
        await this.client.connect(this.host, parseInt(this.port), c.deviceId);
      } catch (e) {
        this.appendLog(`绑定失败：${JSON.stringify(e)}`);
      }
    }
  }

  /** 操控页 → 返回车队页（切车入口） */
  private onBackClick(): void {
    if (this.client.isConnected()) {
      this.client.disconnect('返回车队页');
    }
    this.view = 'list';
  }

  // ---------------- 地图（实时） ----------------

  /** 以全队平均位置为中心，自动选择比例尺让所有车都落在地图内 */
  private updateMapView(): void {
    if (this.cars.length === 0) {
      return;
    }
    let sx = 0;
    let sy = 0;
    for (const c of this.cars) {
      sx += c.x;
      sy += c.y;
    }
    this.centerX = sx / this.cars.length;
    this.centerY = sy / this.cars.length;
    let spread = 2;   // 至少按 2 米视野，避免堆在一起时无限放大
    for (const c of this.cars) {
      spread = Math.max(spread, Math.abs(c.x - this.centerX));
      spread = Math.max(spread, Math.abs(c.y - this.centerY));
    }
    this.viewScale = Math.min(MAX_SCALE, (MAP_SIZE / 2 - EDGE_PAD) / spread);
  }

  /** 世界坐标 → 地图偏移（屏幕 y 向下与 world y 相反） */
  private mapOffset(pos: number, center: number): number {
    let d = (pos - center) * this.viewScale;
    const half = MAP_SIZE / 2 - EDGE_PAD;
    if (d > half) {
      d = half;
    }
    if (d < -half) {
      d = -half;
    }
    return d;
  }

  // ---------------- 轨迹页 ----------------

  /** 打开轨迹页（sel = 'all' 或 device_id），并拉取全队轨迹 */
  private async openTrack(sel: string): Promise<void> {
    this.trackSel = sel;
    this.view = 'track';
    this.canvasReady = false;
    await this.loadTracks();
  }

  private async loadTracks(): Promise<void> {
    if (this.cars.length === 0) {
      this.appendLog('车队为空，先连接后端');
      return;
    }
    this.trackLoading = true;
    const out: TrackData[] = [];
    try {
      for (let i = 0; i < this.cars.length; i++) {
        const c = this.cars[i];
        const td = new TrackData();
        td.deviceId = c.deviceId;
        td.color = this.colorOf(c, i);
        try {
          td.points = await this.client.fetchTrack(this.host, parseInt(this.port), c.deviceId);
        } catch (e) {
          td.points = [];
        }
        out.push(td);
      }
      this.tracks = out;
      this.trackStamp++;
      this.appendLog(`轨迹加载完成（${this.cars.length} 台车，时间窗 ${this.trackWinLabel()}）`);
    } finally {
      this.trackLoading = false;
    }
    this.redrawTrack();
  }

  private trackWinLabel(): string {
    for (let i = 0; i < WIN_MINUTES.length; i++) {
      if (WIN_MINUTES[i] === this.trackWinMin) {
        return WIN_LABELS[i];
      }
    }
    return `${this.trackWinMin}分钟`;
  }

  /** 时间窗过滤后的轨迹集合 */
  private shownTracks(): TrackData[] {
    const cutoff = Date.now() - this.trackWinMin * 60000;
    const out: TrackData[] = [];
    for (const td of this.tracks) {
      if (this.trackSel !== 'all' && td.deviceId !== this.trackSel) {
        continue;
      }
      const filt = new TrackData();
      filt.deviceId = td.deviceId;
      filt.color = td.color;
      filt.points = td.points.filter((p: TrackPoint) => p.ts >= cutoff);
      out.push(filt);
    }
    return out;
  }

  /** 折线里程（米）：相邻点距离求和 */
  private mileageOf(points: TrackPoint[]): number {
    let d = 0;
    for (let i = 1; i < points.length; i++) {
      const dx = points[i].x - points[i - 1].x;
      const dy = points[i].y - points[i - 1].y;
      d += Math.sqrt(dx * dx + dy * dy);
    }
    return d;
  }

  /** 全部显示轨迹的总点数 / 总里程 */
  private shownTotalPoints(): number {
    let n = 0;
    for (const td of this.shownTracks()) {
      n += td.points.length;
    }
    return n;
  }

  private shownTotalMileage(): number {
    let d = 0;
    for (const td of this.shownTracks()) {
      d += this.mileageOf(td.points);
    }
    return d;
  }

  /** 显示轨迹里最近一次上报时间（字符串） */
  private lastReportTime(): string {
    let latest = '';
    for (const td of this.shownTracks()) {
      if (td.points.length > 0) {
        const t = td.points[td.points.length - 1].t;
        if (t > latest) {
          latest = t;
        }
      }
    }
    return latest !== '' ? latest : '--';
  }

  private redrawTrack(): void {
    if (this.canvasReady) {
      this.drawTrack();
    }
  }

  /** 画历史轨迹：自动定界 → 世界坐标映射画布（y 向上），起点空心/终点实心/车号标注 */
  private drawTrack(): void {
    const ctx = this.ctx;
    const W = MAP_SIZE;
    const H = MAP_SIZE;
    ctx.clearRect(0, 0, W, H);

    const shown = this.shownTracks();
    // 收集所有点求范围
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    let total = 0;
    for (const td of shown) {
      for (const p of td.points) {
        minX = Math.min(minX, p.x);
        maxX = Math.max(maxX, p.x);
        minY = Math.min(minY, p.y);
        maxY = Math.max(maxY, p.y);
        total++;
      }
    }
    // 网格底
    ctx.strokeStyle = '#E4E2D6';
    ctx.lineWidth = 1;
    for (let i = 1; i <= 3; i++) {
      const gx = W * i / 4;
      const gy = H * i / 4;
      ctx.beginPath();
      ctx.moveTo(gx, 0);
      ctx.lineTo(gx, H);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, gy);
      ctx.lineTo(W, gy);
      ctx.stroke();
    }
    if (total === 0) {
      ctx.fillStyle = '#8887A0';
      ctx.font = '13px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('该时间窗内没有轨迹数据', W / 2, H / 2);
      return;
    }

    // 世界 → 画布（四周留 26px，y 轴向上）
    const PAD = 26;
    const spanX = Math.max(maxX - minX, 0.001);
    const spanY = Math.max(maxY - minY, 0.001);
    const scale = Math.min((W - PAD * 2) / spanX, (H - PAD * 2) / spanY);
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;

    ctx.font = '11px sans-serif';
    ctx.textAlign = 'left';
    for (const td of shown) {
      if (td.points.length < 2) {
        continue;
      }
      ctx.strokeStyle = td.color;
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      for (let i = 0; i < td.points.length; i++) {
        const px = W / 2 + (td.points[i].x - cx) * scale;
        const py = H / 2 - (td.points[i].y - cy) * scale;
        if (i === 0) {
          ctx.moveTo(px, py);
        } else {
          ctx.lineTo(px, py);
        }
      }
      ctx.stroke();
      // 起点（空心圈）
      const sx = W / 2 + (td.points[0].x - cx) * scale;
      const sy = H / 2 - (td.points[0].y - cy) * scale;
      ctx.beginPath();
      ctx.arc(sx, sy, 4, 0, Math.PI * 2);
      ctx.stroke();
      // 终点（实心点）
      const ex = W / 2 + (td.points[td.points.length - 1].x - cx) * scale;
      const ey = H / 2 - (td.points[td.points.length - 1].y - cy) * scale;
      ctx.fillStyle = td.color;
      ctx.beginPath();
      ctx.arc(ex, ey, 4.5, 0, Math.PI * 2);
      ctx.fill();
      // 车号标注
      ctx.fillText(`${this.carNo(td.deviceId)}号`, ex + 7, ey - 7);
    }
  }

  // ---------------- 摇杆 ----------------

  /** 摇杆触摸：角度+速度限流发送；松手保持行驶（V2 语义） */
  private onJoystickTouch(event: TouchEvent): void {
    if (event.type === TouchType.Up || event.type === TouchType.Cancel) {
      this.dragging = false;
      return;
    }
    if (event.type !== TouchType.Down && event.type !== TouchType.Move) {
      return;
    }
    if (!this.connected || this.selectedId === '') {
      if (!this.warnedNotConnected) {
        this.appendLog('请先绑定车辆再拖动摇杆');
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
    if (r > MAX_RADIUS) {
      dx = dx / r * MAX_RADIUS;
      dy = dy / r * MAX_RADIUS;
    }
    this.knobX = dx;
    this.knobY = dy;

    const clampedR = Math.min(r, MAX_RADIUS);
    if (clampedR < DEAD_ZONE) {
      return;
    }
    let deg = Math.atan2(dx, -dy) * 180 / Math.PI;
    if (deg < 0) {
      deg += 360;
    }
    this.cmdAngle = deg;
    const ratio = Math.min(1, clampedR / MAX_RADIUS);
    this.cmdSpeedPct = Math.round(ratio * 100);

    const now = Date.now();
    if (now - this.lastSendMs < SEND_INTERVAL_MS) {
      return;
    }
    this.lastSendMs = now;
    this.dragging = true;
    this.client.sendMoveAngle(deg, ratio).catch((e: Object) => {
      this.appendLog(`move 发送失败：${JSON.stringify(e)}`);
    });
  }

  private async onStopClick(): Promise<void> {
    if (!this.connected) {
      return;
    }
    try {
      await this.client.sendStop();
      this.knobX = 0;
      this.knobY = 0;
      this.cmdSpeedPct = 0;
      this.appendLog('→ STOP（手动停车，摇杆复位）');
    } catch (e) {
      this.appendLog(`stop 发送失败：${JSON.stringify(e)}`);
    }
  }

  /** 地图上点某台车 → 切换控制对象 */
  private onMapCarTap(c: FleetCar): void {
    if (c.deviceId === this.selectedId) {
      return;
    }
    if (!c.online) {
      this.appendLog(`${c.name} 离线，无法控制`);
      return;
    }
    this.selectedId = c.deviceId;
    this.client.bind(c.deviceId);
    this.savePrefs();
    this.knobX = 0;
    this.knobY = 0;
    this.cmdSpeedPct = 0;
    this.appendLog(`点地图切换控制：${c.name}`);
  }

  /** 旋转显示角：拖动时显示指令角，空闲时显示服务端回传朝向 */
  private getDisplayAngle(): number {
    return this.dragging ? this.cmdAngle : this.selectedTheta();
  }

  private selectedTheta(): number {
    const c = this.selectedCar();
    return c !== null ? c.heading : 0;
  }

  /** 小车俯视图形（与网页大屏同款），指向正北，随 angle 旋转；scale 控制整体大小（1=原始） */
  @Builder
  carShape(body: string, angle: number, dim: boolean, selected: boolean, scale: number) {
    Stack() {
      if (selected) {
        Circle({ width: 74, height: 74 })
          .fill('#00000000')
          .stroke(body)
          .strokeWidth(2)
      }
      Column({ space: 2 }) {
        Row({ space: 24 }) {
          Rect({ width: 11, height: 20 }).fill('#22222A').radius(3)
          Rect({ width: 11, height: 20 }).fill('#22222A').radius(3)
        }
        Stack({ alignContent: Alignment.TopStart }) {
          Rect({ width: 52, height: 62 }).fill(body).radius(12)
          Rect({ width: 36, height: 14 }).fill('#BFE3FF').radius(3)
            .offset({ x: 8, y: 9 })
          Rect({ width: 30, height: 22 }).fill('#40000000').radius(4)
            .offset({ x: 11, y: 27 })
          Row({ space: 8 }) {
            Rect({ width: 8, height: 4 }).fill('#FFE066').radius(2)
            Rect({ width: 8, height: 4 }).fill('#FFE066').radius(2)
          }.offset({ x: 14, y: 2 })
        }
        Row({ space: 24 }) {
          Rect({ width: 11, height: 20 }).fill('#22222A').radius(3)
          Rect({ width: 11, height: 20 }).fill('#22222A').radius(3)
        }
      }
    }
    .rotate({
      x: 0, y: 0, z: 1,
      angle: angle,
      centerX: '50%',
      centerY: '50%'
    })
    .scale({ x: scale, y: scale })
    .opacity(dim ? 0.35 : 1.0)
  }

  // ---------------- UI ----------------

  build() {
    Column() {
      if (this.view === 'list') {
        this.listView()
      } else if (this.view === 'ctrl') {
        this.ctrlView()
      } else if (this.view === 'track') {
        this.trackView()
      } else {
        this.adminView()
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#F7F6FB')
  }

  /** ---------------- 车队页 ---------------- */
  @Builder
  listView() {
    Column({ space: 12 }) {
      Row() {
        Text('CarFleet · 智能巡检车队')
          .fontSize(22).fontWeight(FontWeight.Bold)
        Blank()
        Text('管理后台 ›').fontSize(13).fontColor('#534AB7')
          .fontWeight(FontWeight.Bold)
          .onClick(() => { this.view = 'admin'; })
      }
      .width('100%').padding({ left: 16, right: 16 })
      .margin({ top: 14 })

      Row({ space: 8 }) {
        TextInput({ text: this.host, placeholder: '后端 IP（模拟器 10.0.2.2）' })
          .layoutWeight(2).height(42).backgroundColor('#FFFFFF')
          .onChange((v: string) => { this.host = v; })
        TextInput({ text: this.port, placeholder: '端口' })
          .layoutWeight(1).height(42).type(InputType.Number)
          .backgroundColor('#FFFFFF')
          .onChange((v: string) => { this.port = v; })
        Button(this.connecting ? '…' : '连接')
          .height(42).width(84)
          .backgroundColor(this.connecting ? '#8A86A8' : '#0F6E56')
          .enabled(!this.connecting)
          .onClick(() => { this.onConnectClick(); })
      }.width('100%').padding({ left: 16, right: 16 })

      // 车队摘要卡
      Row() {
        this.summaryCell('车队总数', `${this.cars.length}`)
        Divider().vertical(true).height(30).color('#E0DEE8')
        this.summaryCell('在线', `${this.onlineCount()}`)
        Divider().vertical(true).height(30).color('#E0DEE8')
        this.summaryCell('平均电量',
          this.onlineCount() > 0 ? `${this.avgBattery().toFixed(0)}%` : '--')
      }
      .width('100%').padding({ top: 12, bottom: 12 })
      .margin({ left: 16, right: 16 })
      .borderRadius(12).backgroundColor('#FFFFFF')

      Text(this.cars.length > 0
        ? `点车辆信息区=控制 · 点「轨迹」=历史回放（共 ${this.cars.length} 台）`
        : '点「连接」拉取车队列表')
        .fontSize(12).fontColor('#8887A0')
        .width('100%').padding({ left: 20, right: 16 })

      // 车辆卡片列表
      List({ space: 10 }) {
        ForEach(this.cars, (c: FleetCar, index: number) => {
          ListItem() {
            Row({ space: 12 }) {
              Rect({ width: 6, height: 44 }).fill(this.colorOf(c, index)).radius(3)
              Column({ space: 3 }) {
                Row({ space: 6 }) {
                  Text(c.name).fontSize(16).fontWeight(FontWeight.Bold)
                  if (c.deviceId === this.boundId) {
                    Text('上次绑定').fontSize(9).fontColor('#FFFFFF')
                      .padding({ left: 5, right: 5, top: 1, bottom: 1 })
                      .borderRadius(6).backgroundColor('#534AB7')
                  }
                }
                Text(this.shortId(c.deviceId)).fontSize(10).fontColor('#8887A0')
                Row({ space: 6 }) {
                  Progress({ value: c.battery, total: 100, type: ProgressType.Linear })
                    .width(90).height(6)
                    .color(c.battery < 20 ? '#C62828' : this.colorOf(c, index))
                  Text(`${c.battery.toFixed(0)}%`)
                    .fontSize(10).fontColor('#8887A0')
                }
              }
              .alignItems(HorizontalAlign.Start)
              .layoutWeight(1)
              .onClick(() => { this.onSelectCar(c); })

              Column({ space: 6 }) {
                Text(c.online ? '● 在线' : '○ 离线')
                  .fontSize(12).fontWeight(FontWeight.Bold)
                  .fontColor(c.online ? '#1D9E75' : '#A32D2D')
                Row({ space: 10 }) {
                  Text('轨迹').fontSize(12).fontColor('#534AB7')
                    .fontWeight(FontWeight.Bold)
                    .onClick(() => { this.openTrack(c.deviceId); })
                  Text('控制 ›').fontSize(12).fontColor(this.colorOf(c, index))
                    .fontWeight(FontWeight.Bold)
                    .onClick(() => { this.onSelectCar(c); })
                }
              }
            }
            .width('100%').padding(12)
            .borderRadius(12)
            .backgroundColor(c.online ? '#FFFFFF' : '#F2F1F5')
            .border({
              width: c.deviceId === this.boundId ? 2 : 1,
              color: c.deviceId === this.boundId ? '#534AB7' : '#ECEAF2'
            })
          }
        }, (c: FleetCar) => c.deviceId + '_' + (c.online ? '1' : '0')
          + '_' + c.battery.toFixed(0))
      }
      .layoutWeight(1).width('100%').padding({ left: 16, right: 16 })

      Text(this.logText === '' ? '日志区' : this.logText)
        .fontSize(11).fontColor('#8887A0')
        .width('100%').height(100).padding(8)
        .margin({ left: 16, right: 16, bottom: 12 })
        .borderRadius(10).backgroundColor('#EFEDF6')
        .textAlign(TextAlign.Start)
    }
    .width('100%').height('100%')
  }

  @Builder
  summaryCell(label: string, value: string) {
    Column({ space: 3 }) {
      Text(value).fontSize(20).fontWeight(FontWeight.Bold).fontColor('#3B3663')
      Text(label).fontSize(11).fontColor('#8887A0')
    }
    .layoutWeight(1)
  }

  /** ---------------- 操控页 ---------------- */
  @Builder
  ctrlView() {
    Scroll() {
      Column({ space: 10 }) {
        // 顶栏
        Row({ space: 8 }) {
          Button('‹ 车队').height(34).fontSize(13)
            .backgroundColor('#534AB7')
            .onClick(() => { this.onBackClick(); })
          Button('轨迹').height(34).fontSize(13)
            .backgroundColor('#2D6EB7')
            .onClick(() => { this.openTrack(this.selectedId !== '' ? this.selectedId : 'all'); })
          Circle({ width: 12, height: 12 })
            .fill(this.selectedColorOfSelected())
          Text(this.selectedName()).fontSize(16).fontWeight(FontWeight.Bold)
            .layoutWeight(1)
          Text(this.connected ? '● 已连接' : '○ 未连接')
            .fontSize(12).fontWeight(FontWeight.Bold)
            .fontColor(this.connected ? '#1D9E75' : '#A32D2D')
        }.width('100%')

        // ---- 实时地图：全部车辆同屏，点车切换控制 ----
        Stack() {
          Rect({ width: MAP_SIZE, height: MAP_SIZE })
            .fill('#F6F5EF').stroke('#B4B2A9').strokeWidth(2)
          Column() {
            ForEach([0, 1, 2], (_i: number) => {
              Row().width(MAP_SIZE - 20).height(1).backgroundColor('#E4E2D6')
                .margin({ top: (MAP_SIZE - 20) / 4 - 1 })
            }, (i: number) => `h${i}`)
          }
          Row() {
            ForEach([0, 1, 2], (_i: number) => {
              Column().width(1).height(MAP_SIZE - 20).backgroundColor('#E4E2D6')
                .margin({ left: (MAP_SIZE - 20) / 4 - 1 })
            }, (i: number) => `v${i}`)
          }
          ForEach(this.cars, (c: FleetCar, index: number) => {
            Column({ space: 2 }) {
              this.carShape(this.colorOf(c, index), c.heading,
                !c.online, c.deviceId === this.selectedId, 0.65)
              Text(`${this.carNo(c.deviceId)}号${c.deviceId === this.selectedId ? ' ◀' : ''}`)
                .fontSize(10)
                .fontWeight(c.deviceId === this.selectedId
                  ? FontWeight.Bold : FontWeight.Normal)
                .fontColor(this.colorOf(c, index))
            }
            .translate({
              x: this.mapOffset(c.x, this.centerX),
              y: -this.mapOffset(c.y, this.centerY)
            })
            .onClick(() => { this.onMapCarTap(c); })
          }, (c: FleetCar) => c.deviceId + '_' + c.x.toFixed(1)
            + '_' + c.y.toFixed(1) + '_' + c.heading.toFixed(0)
            + '_' + (c.online ? '1' : '0'))
        }
        .width(MAP_SIZE).height(MAP_SIZE)

        Text('点地图上的车切换控制对象 ◀ 为当前控制车辆')
          .fontSize(10).fontColor('#8887A0')

        // ---- 车辆切换条 ----
        Scroll() {
          Row({ space: 8 }) {
            ForEach(this.cars, (c: FleetCar, index: number) => {
              Button(`${this.carNo(c.deviceId)}号`)
                .height(30).fontSize(12)
                .backgroundColor(c.deviceId === this.selectedId
                  ? this.colorOf(c, index) : '#8A86A8')
                .opacity(c.online ? 1 : 0.5)
                .onClick(() => { this.onMapCarTap(c); })
            }, (c: FleetCar) => c.deviceId)
            Blank()
            Button('停车').height(30).fontSize(12)
              .backgroundColor('#A32D2D')
              .onClick(() => { this.onStopClick(); })
          }
        }.scrollable(ScrollDirection.Horizontal).width('100%')

        // ---- 摇杆 ----
        Stack() {
          Text('N').fontSize(13).fontWeight(FontWeight.Bold).fontColor('#534AB7')
            .offset({ x: 0, y: -DISC_SIZE / 2 + 10 })
          Circle({ width: DISC_SIZE, height: DISC_SIZE })
            .fill(this.connected ? '#F3F1FB' : '#EFEEE9')
            .stroke(this.connected ? '#534AB7' : '#B4B2A9')
            .strokeWidth(3)
          Column().width(1).height(DISC_SIZE - 24).backgroundColor('#D9D6EE')
          Row().width(DISC_SIZE - 24).height(1).backgroundColor('#D9D6EE')
          this.carShape(this.selectedColorOfSelected(),
            this.getDisplayAngle(), false, false, 1.0)
          Circle({ width: KNOB_SIZE, height: KNOB_SIZE })
            .fill(this.dragging ? '#534AB7' : '#7F77DD')
            .stroke('#FFFFFF').strokeWidth(2)
            .shadow({ radius: 6, color: '#40000000', offsetX: 0, offsetY: 2 })
            .opacity(0.85)
            .translate({ x: this.knobX, y: this.knobY })
        }
        .width(DISC_SIZE).height(DISC_SIZE)
        .onTouch((event: TouchEvent) => { this.onJoystickTouch(event); })

        Row({ space: 14 }) {
          Text(`指令角 ${this.cmdAngle.toFixed(0)}°`)
            .fontSize(13).fontWeight(FontWeight.Bold).fontColor('#534AB7')
          Text(`速度 ${this.cmdSpeedPct}%`).fontSize(13).fontColor('#5F5E5A')
          Text('拖动设定方向速度，松手保持行驶').fontSize(10).fontColor('#8887A0')
        }

        // ---- 选中车状态面板 ----
        Row() {
          this.statCell('坐标', this.selectedXy(), '#3B3663')
          Divider().vertical(true).height(28).color('#E0DEE8')
          this.statCell('速度',
            `${this.selectedSpeedPct()}%`,
            this.selectedSpeedPct() > 0 ? '#1D9E75' : '#3B3663')
          Divider().vertical(true).height(28).color('#E0DEE8')
          this.statCell('电量',
            `${this.selectedBattery().toFixed(0)}%`,
            this.selectedBattery() < 20 ? '#C62828' : '#3B3663')
        }
        .width('100%').padding({ top: 10, bottom: 10 })
        .borderRadius(12).backgroundColor('#FFFFFF')

        Text(this.logText === '' ? '日志区：拖动摇杆驾驶当前车辆' : this.logText)
          .fontSize(11).fontColor('#8887A0')
          .width('100%').height(90).padding(8)
          .borderRadius(10).backgroundColor('#EFEDF6')
          .textAlign(TextAlign.Start)
      }
      .padding({ left: 16, right: 16, top: 10, bottom: 12 })
      .width('100%')
    }
    .width('100%').height('100%')
    .align(Alignment.TopStart)
    .scrollBar(BarState.Off)
  }

  @Builder
  statCell(label: string, value: string, color: string) {
    Column({ space: 3 }) {
      Text(value).fontSize(15).fontWeight(FontWeight.Bold).fontColor(color)
      Text(label).fontSize(11).fontColor('#8887A0')
    }.layoutWeight(1)
  }

  /** ---------------- 轨迹回放 · 车辆详情页 ---------------- */
  @Builder
  trackView() {
    Column({ space: 10 }) {
      // 顶栏
      Row({ space: 8 }) {
        Button('‹ 返回').height(34).fontSize(13)
          .backgroundColor('#534AB7')
          .onClick(() => {
            this.view = this.selectedId !== '' && this.connected ? 'ctrl' : 'list';
          })
        Text('轨迹回放 · 车辆详情').fontSize(16).fontWeight(FontWeight.Bold)
          .layoutWeight(1)
        Button(this.trackLoading ? '…' : '刷新').height(34).fontSize(13)
          .backgroundColor('#0F6E56')
          .enabled(!this.trackLoading)
          .onClick(() => { this.loadTracks(); })
      }.width('100%').padding({ left: 16, right: 16, top: 10 })

      // 车辆筛选 chips
      Scroll() {
        Row({ space: 8 }) {
          Button('全部').height(28).fontSize(12)
            .backgroundColor(this.trackSel === 'all' ? '#3B3663' : '#8A86A8')
            .onClick(() => {
              this.trackSel = 'all';
              this.trackStamp++;
              this.redrawTrack();
            })
          ForEach(this.cars, (c: FleetCar, index: number) => {
            Button(`${this.carNo(c.deviceId)}号`).height(28).fontSize(12)
              .backgroundColor(this.trackSel === c.deviceId
                ? this.colorOf(c, index) : '#8A86A8')
              .onClick(() => {
                this.trackSel = c.deviceId;
                this.trackStamp++;
                this.redrawTrack();
              })
          }, (c: FleetCar) => c.deviceId)
        }
      }.scrollable(ScrollDirection.Horizontal)
        .width('100%').padding({ left: 16, right: 16 })

      // 时间窗 chips
      Row({ space: 8 }) {
        Text('时间窗').fontSize(12).fontColor('#8887A0')
        ForEach(WIN_MINUTES, (m: number, i: number) => {
          Button(WIN_LABELS[i]).height(28).fontSize(12)
            .backgroundColor(this.trackWinMin === m ? '#534AB7' : '#C9C6DB')
            .onClick(() => {
              this.trackWinMin = m;
              this.trackStamp++;
              this.redrawTrack();
            })
        }, (m: number) => `w${m}`)
      }.width('100%').padding({ left: 16, right: 16 })

      // 轨迹画布
      Canvas(this.ctx)
        .width(MAP_SIZE).height(MAP_SIZE)
        .backgroundColor('#F6F5EF')
        .borderRadius(12)
        .onReady(() => {
          this.canvasReady = true;
          this.redrawTrack();
        })

      Text('起点 ○ ｜ 终点 ●（彩线按车辆颜色区分，与实时地图同色）')
        .fontSize(10).fontColor('#8887A0')

      // 详情面板：点数 / 里程 / 最近上报
      Row() {
        this.statCell('轨迹点数', `${this.shownTotalPoints()}`, '#3B3663')
        Divider().vertical(true).height(28).color('#E0DEE8')
        this.statCell('里程(窗内)', `${this.shownTotalMileage().toFixed(1)}m`, '#1D9E75')
        Divider().vertical(true).height(28).color('#E0DEE8')
        this.statCell('最近上报', this.lastReportTime(), '#3B3663')
      }
      .width('100%').padding({ top: 10, bottom: 10 })
      .margin({ left: 16, right: 16 })
      .borderRadius(12).backgroundColor('#FFFFFF')

      // 每车图例明细
      Column({ space: 6 }) {
        ForEach(this.shownTracks(), (td: TrackData) => {
          Row({ space: 8 }) {
            Circle({ width: 10, height: 10 }).fill(td.color)
            Text(`${this.carNo(td.deviceId)}号 · ${this.shortId(td.deviceId)}`)
              .fontSize(12).fontWeight(FontWeight.Bold).layoutWeight(1)
            Text(`${td.points.length} 点 ｜ ${this.mileageOf(td.points).toFixed(1)} m`)
              .fontSize(12).fontColor('#8887A0')
          }.width('100%')
        }, (td: TrackData) => td.deviceId + '_' + td.points.length
          + '_' + this.trackStamp.toString())
        if (this.shownTracks().length === 0) {
          Text(this.trackLoading ? '轨迹加载中…' : '无数据（先连接后端）')
            .fontSize(12).fontColor('#8887A0')
        }
      }
      .width('100%').padding(12)
      .margin({ left: 16, right: 16, bottom: 12 })
      .borderRadius(12).backgroundColor('#FFFFFF')

      Blank()
    }
    .width('100%').height('100%')
    .alignItems(HorizontalAlign.Start)
  }

  /** ---------------- 管理后台（内嵌 Web） ---------------- */
  @Builder
  adminView() {
    Column({ space: 8 }) {
      Row({ space: 8 }) {
        Button('‹ 返回').height(34).fontSize(13)
          .backgroundColor('#534AB7')
          .onClick(() => { this.view = 'list'; })
        Text(this.adminPage === 'ai' ? 'AI 语音控车' : 'B/S 管理后台').fontSize(16).fontWeight(FontWeight.Bold)
          .layoutWeight(1)
        Button(this.adminPage === 'admin' ? 'AI 控车' : '管理后台').height(34).fontSize(12)
          .backgroundColor('#0F8A8A')
          .onClick(() => {
            this.adminPage = this.adminPage === 'admin' ? 'ai' : 'admin';
            const url: string = `http://${this.host}:${this.port}/${this.adminPage}/view`;
            this.webController.loadUrl(url);
            this.appendLog(`内嵌页面已切换：${url}`);
          })
      }
      .width('100%').padding({ left: 16, right: 16, top: 10 })

      Web({ src: `http://${this.host}:${this.port}/admin/view`, controller: this.webController })
        .width('100%')
        .layoutWeight(1)
        .backgroundColor('#FFFFFF')
        .javaScriptAccess(true)
        .domStorageAccess(true)
        .onControllerAttached(() => {
          this.appendLog(`管理后台已加载：${this.host}:${this.port}/admin/view`);
        })
    }
    .width('100%').height('100%')
  }

  // ---------------- 选中车取值 ----------------

  private selectedColorOfSelected(): string {
    const c = this.selectedCar();
    if (c !== null && c.color !== '') {
      return c.color;
    }
    return CAR_COLORS[0];
  }

  private selectedXy(): string {
    const c = this.selectedCar();
    return c !== null ? `(${c.x.toFixed(1)}, ${c.y.toFixed(1)})` : '--';
  }

  private selectedSpeedPct(): number {
    const c = this.selectedCar();
    return c !== null ? c.speed : 0;
  }

  private selectedBattery(): number {
    const c = this.selectedCar();
    return c !== null ? c.battery : 0;
  }
}
