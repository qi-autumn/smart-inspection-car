/**
 * 模拟小车 TCP 客户端封装
 *
 * 职责：
 *  1. 建立与模拟小车服务端的 TCP 长连接
 *  2. 摇杆指令 / 停车指令的发送
 *  3. 心跳保活（每 5 秒一次 ping，服务端回 pong）
 *  4. 接收线程：粘包拆包（按 '\n' 分帧）→ 状态回调
 *
 * 设计要点：UI 页面只依赖本类的回调，不直接接触 socket API；
 * 后期切换到华为云 IoT（MQTT）时只需替换本类实现。
 */
import { socket } from '@kit.NetworkKit';
import { CarState, buildMoveCmd, buildStopCmd, buildPing, buildMoveByAngle,
  splitFrames, parseState, isPong } from '../model/Protocol';

const HEARTBEAT_INTERVAL_MS: number = 5000;

export class CarTcpClient {
  private tcpSocket: socket.TCPSocket | null = null;
  private recvBuffer: string = '';
  private heartbeatTimer: number = -1;
  private isConnected: boolean = false;

  // ---- 对外回调（页面注册） ----
  onState: (state: CarState) => void = (_s: CarState) => {};
  onConnected: () => void = () => {};
  onDisconnected: (reason: string) => void = (_r: string) => {};

  /**
   * 连接模拟小车服务端
   * @param host 服务端 IP（模拟器访问宿主机可填 10.0.2.2 或宿主机局域网 IP）
   * @param port 服务端监听端口
   */
  async connect(host: string, port: number): Promise<void> {
    if (this.isConnected) {
      await this.disconnect('reconnect');
    }
    this.tcpSocket = socket.constructTCPSocketInstance();

    // bind 到随机本地端口
    const localAddr: socket.NetAddress = { address: '0.0.0.0', port: 0, family: 1 };
    await this.tcpSocket.bind(localAddr);

    const remoteAddr: socket.NetAddress = { address: host, port: port, family: 1 };
    const connOpts: socket.TCPConnectOptions = { address: remoteAddr, timeout: 5000 };
    await this.tcpSocket.connect(connOpts);

    // 注册接收与关闭事件
    this.tcpSocket.on('message', (value: socket.SocketMessageInfo) => {
      this.onMessageData(value.message);
    });
    this.tcpSocket.on('close', () => {
      this.handleClosed('对端关闭连接');
    });
    this.tcpSocket.on('error', () => {
      this.handleClosed('连接异常');
    });

    this.isConnected = true;
    this.recvBuffer = '';
    this.startHeartbeat();
    this.onConnected();
  }

  /** 发送摇杆矢量指令（各分量 -1 ~ 1） */
  async sendMove(vx: number, vy: number): Promise<void> {
    await this.rawSend(buildMoveCmd(vx, vy));
  }

  /**
   * 按角度发送移动指令（360° 全方向）
   * @param angleDeg 角度 0~360，0° = 正北，顺时针为正
   * @param speedRatio 速度比例 0 ~ 1
   */
  async sendMoveByAngle(angleDeg: number, speedRatio: number): Promise<void> {
    await this.rawSend(buildMoveByAngle(angleDeg, speedRatio));
  }

  /** 发送停车指令 */
  async sendStop(): Promise<void> {
    await this.rawSend(buildStopCmd());
  }

  /** 主动断开 */
  async disconnect(_reason: string = '主动断开'): Promise<void> {
    this.stopHeartbeat();
    if (this.tcpSocket) {
      try {
        await this.tcpSocket.close();
      } catch (e) {
        // 忽略重复关闭异常
      }
      this.tcpSocket = null;
    }
    if (this.isConnected) {
      this.isConnected = false;
      this.onDisconnected(_reason);
    }
  }

  // ---------------- 内部实现 ----------------

  private async rawSend(data: string): Promise<void> {
    if (!this.tcpSocket || !this.isConnected) {
      return;
    }
    const opts: socket.TCPSendOptions = { data: data };
    try {
      await this.tcpSocket.send(opts);
    } catch (e) {
      this.handleClosed('发送失败');
    }
  }

  /** 收到原始字节流：解码 → 追加缓冲区 → 按 '\n' 分帧解析 */
  private onMessageData(buf: ArrayBuffer): void {
    const text: string = this.decode(buf);
    this.recvBuffer += text;
    const frames = splitFrames(this.recvBuffer);
    if (frames.length === 0) {
      return;
    }
    // 最后一段可能是被截断的半条消息，回存到缓冲区等待后续数据
    const lastIncomplete = this.recvBuffer.endsWith('\n') ? '' : frames.pop() ?? '';
    for (const frame of frames) {
      this.dispatchFrame(frame);
    }
    this.recvBuffer = lastIncomplete;
  }

  private dispatchFrame(frame: string): void {
    if (isPong(frame)) {
      return; // 心跳应答，仅需保活，无需通知 UI
    }
    const state: CarState | null = parseState(frame);
    if (state !== null) {
      this.onState(state);
    }
  }

  private decode(buf: ArrayBuffer): string {
    const u8 = new Uint8Array(buf);
    let out: string = '';
    for (let i = 0; i < u8.length; i++) {
      out += String.fromCharCode(u8[i]);
    }
    return out;
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      this.rawSend(buildPing());
    }, HEARTBEAT_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== -1) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = -1;
    }
  }

  private handleClosed(reason: string): void {
    if (!this.isConnected) {
      return;
    }
    this.stopHeartbeat();
    this.isConnected = false;
    this.tcpSocket = null;
    this.onDisconnected(reason);
  }
}
