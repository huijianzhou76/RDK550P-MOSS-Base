# MOSS Hardware Protocol v1

传输：UTF-8 JSON Lines。每个消息以 `\n` 结束。

## Command

```json
{
  "v": 1,
  "id": "cmd-7f31c4",
  "type": "command",
  "action": "head.move",
  "ts": 1770000000000,
  "params": {
    "yaw_deg": 20,
    "pitch_deg": -8,
    "speed": 0.45
  }
}
```

## Response

```json
{
  "v": 1,
  "id": "cmd-7f31c4",
  "type": "response",
  "ok": true,
  "ts": 1770000000120,
  "data": {
    "yaw_deg": 20,
    "pitch_deg": -8
  }
}
```

错误响应：

```json
{
  "v": 1,
  "id": "cmd-7f31c4",
  "type": "response",
  "ok": false,
  "error": "ANGLE_LIMIT",
  "message": "yaw exceeds configured safe range",
  "ts": 1770000000120
}
```

## Telemetry Event

MCU 可以主动上报：

```json
{
  "v": 1,
  "type": "event",
  "event": "sensor.update",
  "ts": 1770000000200,
  "data": {
    "temperature_c": 42.1,
    "supply_v": 5.04
  }
}
```

## Actions

### `system.ping`

无参数。返回 MCU 固件版本、uptime、board id。

### `system.status`

返回：急停状态、head 位置、灯光、显示器、传感器、电源和 watchdog 状态。

### `system.estop`

立即停止全部运动，进入锁定。

### `system.estop_clear`

必须包含人工确认字段：

```json
{"operator_confirmed": true}
```

MCU 仍可要求物理按键确认。

### `head.move`

参数：

```json
{
  "yaw_deg": 0,
  "pitch_deg": 0,
  "speed": 0.5
}
```

推荐 MCU 默认限制：

- yaw: `-70° ~ +70°`
- pitch: `-25° ~ +35°`
- speed: `0.05 ~ 1.0`

实际限制应根据最终机械结构校准。

### `head.center`

头部移动到校准中心位。

### `light.set`

```json
{
  "target": "eye",
  "mode": "solid",
  "brightness": 0.65,
  "rgb": [220, 38, 38]
}
```

V1 target：`eye`、`status`。

### `display.text`

```json
{
  "text": "MOSS ONLINE",
  "duration_ms": 3000
}
```

### `sensor.read`

```json
{"sensor": "temperature"}
```

## Watchdog

RDK 每 2 秒至少发送一次 `system.ping` 或独立 heartbeat。

MCU 默认：

- 5 秒无心跳：停止新运动
- 10 秒无心跳：停止全部运动并回安全灯光状态
- 恢复连接后不自动恢复被中断的运动任务

## Idempotency

MCU 应缓存最近至少 32 个 `id`。如果收到重复 command id，不重复执行物理动作，而是返回上一次结果。

## Evidence

RDK 在收到 response 后，把以下字段写入 MOSS Evidence Chain：

- command id
- action
- sanitized params
- start/end timestamp
- MCU response
- device state before/after（如可用）

## 禁止的接口

协议层不提供：

- 任意 GPIO 写入
- 任意内存访问
- 任意 shell/firmware 命令
- 关闭 watchdog
- 绕过角度/速度限制

需要升级固件时必须走独立维护流程，而不是通过 Agent Tool 任意执行。
