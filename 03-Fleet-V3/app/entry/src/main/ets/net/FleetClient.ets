/**
 * V3 车队协议客户端（第⑥步：APP 多车关联 · 地图增强版）
 *
 * 通过 HTTP 与 V3 业务后端（server/app.py，默认 8000 端口）交互，
 * 协议对齐《V3-接口协议设计.md》v1.1（L1 层）：
 *   GET  /fleet/status      整队实时状态（每车 device_id/name/color/位置/电量）
 *   POST /command           控制命令（device_id + cmd + speed/angle）
 *
 * 设计要点：
 *  - 只轮询 /fleet/status 一个接口（800ms），同时供「实时地图」和「状态面板」使用；
 *  - bind() 可随时切换控制对象，无需断开重连；
 *  - 连续 3 次拉取失败自动断开并回调 onDisconnected。
 */
import http from '@ohos.net.http';

const POLL_INTERVAL_MS = 800;   // 整队状态轮询周期
const MAX_FAIL = 3;             // 连续失败 N 次判定断开

/** 整队状态里的单台车（GET /fleet/status 的 cars[] 元素） */
export class FleetCar {
  deviceId: string = '';
  name: string = '';
  color: string = '#e24b4a';
  online: boolean = false;
  x: number = 0;          // 世界坐标（米）
  y: number = 0;
  heading: number = 0;    // 朝向（度，0=正北顺时针）
  speed: number = 0;      // 0~100（L1 层）
  battery: number = 0;    // 电量百分比
}

/** /track 返回的单个轨迹点 */
export class TrackPoint {
  x: number = 0;
  y: number = 0;
  t: string = '';         // "YYYY-MM-DD HH:MM:SS"（后端本地时间）
  ts: number = 0;         // 毫秒时间戳（由 t 解析，供时间窗过滤）
  heading: number = 0;
  speed: number = 0;
}

export class FleetClient {
  // 回调
  public onFleet: (cars: FleetCar[]) => void = () => {};
  public onConnected: () => void = () => {};
  public onDisconnected: (reason: string) => void = () => {};

  private baseUrl: string = '';
  private devId: string = '';
  private polling: boolean = false;
  private timerId: number = -1;
  private failCount: number = 0;

  /** 拉取整队状态（连接前调用；失败抛异常由调用方提示） */
  public async fetchFleet(host: string, port: number): Promise<FleetCar[]> {
    const req = http.createHttp();
    try {
      const resp = await req.request(`http://${host}:${port}/fleet/status`, {
        method: http.RequestMethod.GET,
        connectTimeout: 5000,
        readTimeout: 5000
      });
      if (resp.responseCode !== 200) {
        throw new Error(`HTTP ${resp.responseCode}`);
      }
      const obj = JSON.parse(resp.result as string) as Record<string, Object>;
      const arr = obj['cars'] as Array<Record<string, Object>>;
      const out: FleetCar[] = [];
      for (const d of arr) {
        const c = new FleetCar();
        c.deviceId = d['device_id'] as string;
        c.name = d['name'] as string;
        c.color = (d['color'] as string) ?? '#e24b4a';
        c.online = d['online'] as boolean;
        c.x = (d['x'] as number) ?? 0;
        c.y = (d['y'] as number) ?? 0;
        c.heading = (d['heading'] as number) ?? 0;
        c.speed = (d['speed'] as number) ?? 0;
        c.battery = (d['battery'] as number) ?? 0;
        out.push(c);
      }
      return out;
    } finally {
      req.destroy();
    }
  }

  /** 绑定某台车并开始整队状态轮询（探活失败会抛异常） */
  public async connect(host: string, port: number, deviceId: string): Promise<void> {
    this.baseUrl = `http://${host}:${port}`;
    this.devId = deviceId;
    await this.fetchFleet(host, port);   // 探活
    this.failCount = 0;
    this.polling = true;
    this.timerId = setInterval(() => { this.pollOnce(); }, POLL_INTERVAL_MS);
    this.onConnected();
  }

  /** 切换控制对象（保持轮询，无需重连） */
  public bind(deviceId: string): void {
    this.devId = deviceId;
  }

  /** 当前绑定的 device_id */
  public currentId(): string {
    return this.devId;
  }

  public disconnect(reason: string): void {
    this.polling = false;
    if (this.timerId >= 0) {
      clearInterval(this.timerId);
      this.timerId = -1;
    }
    this.onDisconnected(reason);
  }

  public isConnected(): boolean {
    return this.polling;
  }

  /** 摇杆矢量行驶：任意角度（0=正北顺时针）+ 速度比例 0~1（松手保持行驶） */
  public async sendMoveAngle(angleDeg: number, speedRatio: number): Promise<void> {
    await this.postCommand({
      'device_id': this.devId,
      'cmd': 'MOVE',
      'angle': Math.round(angleDeg * 10) / 10,
      'speed': Math.round(speedRatio * 100)
    });
  }

  public async sendStop(): Promise<void> {
    await this.postCommand({
      'device_id': this.devId,
      'cmd': 'STOP'
    });
  }

  /** 拉取单车历史轨迹（后端缺省返回最近 24 小时，时间升序） */
  public async fetchTrack(host: string, port: number, deviceId: string): Promise<TrackPoint[]> {
    const req = http.createHttp();
    try {
      const resp = await req.request(
        `http://${host}:${port}/track?device_id=${encodeURIComponent(deviceId)}`, {
        method: http.RequestMethod.GET,
        connectTimeout: 5000,
        readTimeout: 10000
      });
      if (resp.responseCode !== 200) {
        throw new Error(`HTTP ${resp.responseCode}`);
      }
      const obj = JSON.parse(resp.result as string) as Record<string, Object>;
      const arr = obj['points'] as Array<Record<string, Object>>;
      const out: TrackPoint[] = [];
      for (const p of arr) {
        const tp = new TrackPoint();
        tp.x = (p['x'] as number) ?? 0;
        tp.y = (p['y'] as number) ?? 0;
        tp.t = (p['t'] as string) ?? '';
        tp.ts = Date.parse(tp.t.replace(' ', 'T'));
        tp.heading = typeof p['heading'] === 'number' ? p['heading'] as number : 0;
        tp.speed = typeof p['speed'] === 'number' ? p['speed'] as number : 0;
        out.push(tp);
      }
      return out;
    } finally {
      req.destroy();
    }
  }

  // ---------------- 内部实现 ----------------

  private async pollOnce(): Promise<void> {
    const req = http.createHttp();
    try {
      const resp = await req.request(`${this.baseUrl}/fleet/status`, {
        method: http.RequestMethod.GET,
        connectTimeout: 5000,
        readTimeout: 5000
      });
      if (resp.responseCode === 200) {
        const obj = JSON.parse(resp.result as string) as Record<string, Object>;
        const arr = obj['cars'] as Array<Record<string, Object>>;
        const cars: FleetCar[] = [];
        for (const d of arr) {
          const c = new FleetCar();
          c.deviceId = d['device_id'] as string;
          c.name = d['name'] as string;
          c.color = (d['color'] as string) ?? '#e24b4a';
          c.online = d['online'] as boolean;
          c.x = (d['x'] as number) ?? 0;
          c.y = (d['y'] as number) ?? 0;
          c.heading = (d['heading'] as number) ?? 0;
          c.speed = (d['speed'] as number) ?? 0;
          c.battery = (d['battery'] as number) ?? 0;
          cars.push(c);
        }
        this.failCount = 0;
        this.onFleet(cars);
      } else {
        this.failCount++;
      }
    } catch (e) {
      this.failCount++;
    } finally {
      req.destroy();
    }
    if (this.failCount >= MAX_FAIL && this.polling) {
      this.disconnect(`连续 ${MAX_FAIL} 次获取车队状态失败`);
    }
  }

  private async postCommand(body: Record<string, Object>): Promise<void> {
    const req = http.createHttp();
    try {
      const resp = await req.request(`${this.baseUrl}/command`, {
        method: http.RequestMethod.POST,
        header: { 'Content-Type': 'application/json' },
        extraData: JSON.stringify(body),
        connectTimeout: 5000,
        readTimeout: 8000
      });
      if (resp.responseCode !== 200) {
        let msg = `HTTP ${resp.responseCode}`;
        try {
          const err = JSON.parse(resp.result as string) as Record<string, Object>;
          if (err['error']) {
            msg += ` ${err['error']}`;
          }
        } catch (e) {
        }
        throw new Error(msg);
      }
    } finally {
      req.destroy();
    }
  }
}
