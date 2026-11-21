/**
 * 云端模式客户端（第三阶段）
 *
 * 通过 HTTP 与 PC 上的桥接后端（cloud_backend.py，默认 9090 端口）交互，
 * 后端再转发到华为云 IoTDA：命令经应用侧 API 下发给小车，
 * 小车状态通过轮询设备影子获得。
 *
 * 对上层（Index.ets）暴露与 CarTcpClient 一致风格的接口：
 *   connect / disconnect / sendMove / sendStop + onState 回调
 */
import http from '@ohos.net.http';
import { CarState } from '../model/Protocol';

const POLL_INTERVAL_MS = 500;   // 状态轮询周期
const MAX_FAIL = 3;             // 连续失败 N 次判定离线

export class CloudClient {
  // 回调（与 CarTcpClient 风格一致）
  public onState: (s: CarState) => void = () => {};
  public onConnected: () => void = () => {};
  public onDisconnected: (reason: string) => void = () => {};

  private baseUrl: string = '';
  private dev: string = 'car_001';
  private polling: boolean = false;
  private timerId: number = -1;
  private failCount: number = 0;

  /** 连接后端：探活成功后开始轮询状态 */
  public async connect(host: string, port: number, dev: string): Promise<void> {
    this.baseUrl = `http://${host}:${port}`;
    this.dev = dev;
    await this.fetchOnce();      // 探活：失败会抛异常，由调用方提示
    this.failCount = 0;
    this.polling = true;
    this.timerId = setInterval(() => { this.fetchOnce(); }, POLL_INTERVAL_MS);
    this.onConnected();
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

  /** 移动指令：摇杆矢量（各分量 -1~1） */
  public async sendMove(vx: number, vy: number): Promise<void> {
    const paras: Record<string, number> = { 'vx': vx, 'vy': vy };
    await this.postCmd('move', paras);
  }

  public async sendStop(): Promise<void> {
    const paras: Record<string, number> = {};
    await this.postCmd('stop', paras);
  }

  // ---------------- 内部实现 ----------------

  private async fetchOnce(): Promise<void> {
    const req = http.createHttp();
    try {
      const resp = await req.request(`${this.baseUrl}/api/state?dev=${this.dev}`, {
        method: http.RequestMethod.GET,
        connectTimeout: 5000,
        readTimeout: 5000
      });
      if (resp.responseCode === 200) {
        const obj = JSON.parse(resp.result as string) as Record<string, Object>;
        const s = new CarState();
        s.x = obj['x'] as number;
        s.y = obj['y'] as number;
        s.theta = obj['theta'] as number;
        s.speed = obj['speed'] as number;
        s.battery = obj['battery'] as number;
        s.online = obj['online'] as boolean;
        this.failCount = 0;
        this.onState(s);
      } else {
        this.failCount++;
      }
    } catch (e) {
      this.failCount++;
    } finally {
      req.destroy();
    }
    if (this.failCount >= MAX_FAIL && this.polling) {
      this.disconnect(`连续 ${MAX_FAIL} 次获取状态失败`);
    }
  }

  private async postCmd(action: string, paras: Record<string, number>): Promise<void> {
    const req = http.createHttp();
    try {
      const resp = await req.request(`${this.baseUrl}/api/${action}?dev=${this.dev}`, {
        method: http.RequestMethod.POST,
        header: { 'Content-Type': 'application/json' },
        extraData: JSON.stringify(paras),
        connectTimeout: 5000,
        readTimeout: 5000
      });
      if (resp.responseCode !== 200) {
        throw new Error(`HTTP ${resp.responseCode}`);
      }
    } finally {
      req.destroy();
    }
  }
}
