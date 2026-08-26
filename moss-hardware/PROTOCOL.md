# MOSS Hardware Protocol v1.1

传输层：UTF-8 JSON Lines，每条消息以 `\n` 结束。默认串口速率 115200。RDK 端每 2 秒发送一次 `system.heartbeat`，MCU 端独立执行 Watchdog。

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
    "queued": true,
    "target_yaw_deg": 20,
    "target_pitch_deg": -8
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

## MCU 主动事件

MCU 可主动上报安全事件：

```json
{
  "v": 1,
  "type": "event",
  "event": "safety.estop",
  "ts": 1770000000200,
  "data": {
    "source": "physical",
    "estop": true,
    "servo_power": false
  }
}
```

当前事件：`safety.estop`、`safety.estop_cleared`、`safety.link_degraded`、`safety.link_lost`、`safety.link_recovered`。

## System actions

### `system.heartbeat`

RDK Gateway 自动每 2 秒发送。用于喂 MCU Watchdog。

### `system.ping`

返回 board、firmware、protocol、uptime 和当前状态。

### `system.capabilities`

握手接口。返回固件版本、协议版本、实际支持的 capability、机械限位、Watchdog 参数、外设探测结果和 GPIO 映射。RDK 在第一次连接实体 MCU 后应先调用此接口，不应假设某个 OLED/传感器一定存在。

### `system.status`

返回：急停、物理急停输入、servo power、head 当前角度、Watchdog、外设探测状态。

### `system.estop`

立即：清空运动队列、停止 PWM、关闭 `SERVO_POWER_EN`、红眼进入告警亮度并锁存急停状态。

### `system.estop_clear`

必须包含：

```json
{"operator_confirmed": true}
```

同时物理急停输入必须恢复为健康高电平。解除后 MCU 先恢复舵机电源，等待约 200ms，再恢复 PWM。

## Motion actions

### `head.move`

```json
{
  "yaw_deg": 0,
  "pitch_deg": 0,
  "speed": 0.5
}
```

固件 0.2.0 将运动加入 FreeRTOS 队列，并用余弦 easing 插值执行。默认机械安全范围：yaw `-70° ~ +70°`、pitch `-25° ~ +35°`、speed `0.05 ~ 1.0`。最终装配后必须重新校准范围。

### `head.center`

```json
{"speed": 0.45}
```

头部缓动回到逻辑中心位。

## Light / Display

### `light.set`

```json
{
  "target": "eye",
  "brightness": 0.65
}
```

Hardware V1.1 的 eye 是单色红光 PWM，不要求可寻址 RGB 灯珠；推荐使用 5V 红色 LED 环/模块 + 3.3V 逻辑兼容 MOSFET 开关。

### `display.text`

```json
{
  "text": "MOSS ONLINE",
  "duration_ms": 3000
}
```

V1.1 使用 SSD1306 128x64 I2C OLED，固件内置轻量 ASCII 字库。中文显示留到后续字体资源版本。

## Sensor actions

### `sensor.read`

返回：

```json
{
  "power_valid": true,
  "servo_bus_v": 5.03,
  "servo_current_ma": 310.4,
  "environment_valid": true,
  "temperature_c": 26.5,
  "humidity_percent": 48.2
}
```

电源监测目标设备为 INA219，默认按常见 0.1Ω shunt 模块估算电流；环境传感器为 AHT20。

## IR actions

### `ir.learn`

```json
{"slot":"desk_ac_power","timeout_ms":4000}
```

ESP32 RMT 捕获 38kHz 解调接收器输出的原始 mark/space symbol。最多 8 个 slot，每帧最多 256 个 RMT symbol。

### `ir.send`

```json
{"slot":"desk_ac_power","repeat":1}
```

### `ir.list`

返回当前已学习的 slot。V1.1 slot 暂存 RAM，重启后清空；NVS 持久化属于后续增强，不影响第一阶段实体 Bring-up。

## Watchdog

RDK Gateway 默认 2 秒 heartbeat。MCU 默认策略：5 秒无心跳进入 `link_degraded` 并拒绝新运动；10 秒无心跳清空运动、停止 PWM 并拉低 `SERVO_POWER_EN`。恢复有效 RDK 命令后，若没有物理/人工急停，恢复舵机电源与控制，但不会继续之前被中断的运动队列。

## Physical E-STOP

推荐失效安全接法：急停按钮使用 NC 常闭触点，健康状态把 ESP32 `E_STOP` GPIO 接至 3.3V；按钮按下、接头脱落或线缆断开时，由 ESP32 下拉把输入变为低电平并锁存急停。

重要：GPIO 触点只接 3.3V 逻辑，不允许接 5V 舵机电源。

## Evidence

RDK 收到 response 后，把 command id、action、脱敏后的 params、开始/结束时间、MCU response、关键设备状态写入 MOSS Evidence Chain。物理急停和 Watchdog 事件同样进入 WebSocket/Event Log。

## 禁止的接口

协议层不提供任意 GPIO 写入、任意内存访问、任意 shell/firmware 命令、关闭 Watchdog、绕过角度/速度限制。Agent 只能调用高层硬件动作。

## 当前边界

V1.1 Gateway 不自动重试物理动作，因此不会主动重复执行 `head.move` / `ir.send`。MCU command-id 去重缓存尚未加入；如后续增加链路自动重试，必须先实现 MCU idempotency cache。
