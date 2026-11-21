/**
 * 应用层协议定义
 *
 * TCP 只保证字节流可靠到达，不保证消息边界。
 * 本协议约定：每条消息为一行 JSON，以 '\n' 结尾（换行符即消息分隔符，
 * 用于解决粘包/拆包问题）。
 *
 * 消息类型一览：
 *  APP → 服务端：
 *    {"type":"cmd","action":"move","vx":0.5,"vy":-1.0}  摇杆矢量(各分量 -1~1)
 *    {"type":"cmd","action":"stop"}                      停车
 *    {"type":"ping"}                                     心跳
 *  服务端 → APP：
 *    {"type":"pong"}                                     心跳应答
 *    {"type":"state","x":1.2,"y":0.5,"theta":90,"speed":0.8,"battery":87}
 */

export const MSG_DELIMITER: string = '\n';

/** 小车实时状态 */
export class CarState {
  x: number = 0;        // 位置坐标（米）
  y: number = 0;
  theta: number = 0;    // 朝向角（度，0 = 正北）
  speed: number = 0;    // 当前速度（米/秒）
  battery: number = 100; // 电量百分比
  online: boolean = true; // 设备是否在线（云端模式使用）
}

/** 构造摇杆移动指令 */
export function buildMoveCmd(vx: number, vy: number): string {
  const obj: Record<string, Object> = {
    'type': 'cmd',
    'action': 'move',
    'vx': Math.round(vx * 100) / 100,
    'vy': Math.round(vy * 100) / 100
  };
  return JSON.stringify(obj) + MSG_DELIMITER;
}

/** 构造停车指令 */
export function buildStopCmd(): string {
  const obj: Record<string, Object> = { 'type': 'cmd', 'action': 'stop' };
  return JSON.stringify(obj) + MSG_DELIMITER;
}

/**
 * 角度 → 摇杆矢量换算（360° 全方向）
 * @param angleDeg  角度，0° = 正北（画布正上方），顺时针为正，0 ~ 360
 * @param speedRatio 速度比例 0 ~ 1（1 = 满速）
 * 服务端 theta 定义为 atan2(vx, vy)：vx = sin(θ)、vy = cos(θ) 时
 * 小车朝向恰好等于输入角度 θ，实现任意方向行驶。
 */
export function buildMoveByAngle(angleDeg: number, speedRatio: number): string {
  const rad = angleDeg * Math.PI / 180;
  return buildMoveCmd(Math.sin(rad) * speedRatio, Math.cos(rad) * speedRatio);
}

/** 纯换算（供 UI 显示预览）：返回 [vx, vy] */
export function angleToVector(angleDeg: number, speedRatio: number): number[] {
  const rad = angleDeg * Math.PI / 180;
  return [Math.sin(rad) * speedRatio, Math.cos(rad) * speedRatio];
}

/** 构造心跳 */
export function buildPing(): string {
  const obj: Record<string, Object> = { 'type': 'ping' };
  return JSON.stringify(obj) + MSG_DELIMITER;
}

/**
 * 按换行符切分缓冲区，解析出完整消息。
 * 返回解析后的消息数组，剩余不完整的部分保留在 buffer 中（由调用方保存）。
 */
export function splitFrames(buffer: string): string[] {
  const frames: string[] = buffer.split(MSG_DELIMITER);
  return frames.filter((f: string) => f.trim().length > 0);
}

/** 从一行 JSON 中解析小车状态，解析失败返回 null */
export function parseState(frame: string): CarState | null {
  try {
    const obj: Record<string, Object> = JSON.parse(frame) as Record<string, Object>;
    if (obj['type'] !== 'state') {
      return null;
    }
    const s = new CarState();
    s.x = obj['x'] as number;
    s.y = obj['y'] as number;
    s.theta = obj['theta'] as number;
    s.speed = obj['speed'] as number;
    s.battery = obj['battery'] as number;
    return s;
  } catch (e) {
    return null;
  }
}

/** 判断是否为心跳应答 */
export function isPong(frame: string): boolean {
  try {
    const obj: Record<string, Object> = JSON.parse(frame) as Record<string, Object>;
    return obj['type'] === 'pong';
  } catch (e) {
    return false;
  }
}
