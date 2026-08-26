# MOSS Hardware Layer

`moss-hardware` 是 RDK X5 上的硬件抽象层（HAL）。它位于 MOSS AI / Web Control Plane 与实时 MCU 之间，目标是让上层只表达“我要做什么”，而不是直接操纵 GPIO。

## 设计目标

```text
OpenClaw / Mission / Planner
            |
            v
MOSS Control Plane (RDK X5)
            |
            v
moss-hardware / MCU Gateway
            |
      USB CDC / UART
            |
            v
moss-mcu (ESP32 / STM32)
            |
    +-------+-------+---------+---------+
    |       |       |         |         |
  Motor    LED    OLED       IR      Sensors
```

RDK X5 负责：LLM、视觉、长期记忆、任务规划、风险判断、工具编排、Web 控制台。

MCU 负责：电机实时控制、限位、灯光、显示、按键、传感器、电源状态、Watchdog、急停。

## 为什么增加 MCU 层

原 RDK550P-MOSS 直接通过 Linux PWM 控制两个舵机，适合第一代 Demo。但随着电机、LED、OLED、红外、传感器数量增加，实时执行不应该继续由 Linux/LLM 进程直接承担。

`moss-xiaozhi` 的硬件实现证明了 ESP32 很适合承担这一层：其板级代码已经包含 I2S 音频、OLED、LED、按钮以及 MCP 工具；双轴步进电机也通过 FreeRTOS 独立任务执行。我们的实现不绑定小智应用层，而是保留“MCU 负责实时设备”的设计思路。

## Hardware V1 支持范围

- `system.ping`：MCU 链路检查
- `system.status`：读取 MCU / 设备状态
- `system.estop`：进入急停锁定状态
- `system.estop_clear`：人工清除急停
- `head.move`：控制 yaw / pitch
- `head.center`：头部回中心
- `light.set`：MOSS 眼灯 / 状态灯
- `display.text`：OLED / 数码管状态文字
- `sensor.read`：读取指定传感器

后续可扩展：红外学习/发射、风扇、电源管理、电池、触摸、环境传感器、底盘、机械臂。

## 传输方式

V1 使用 JSON Lines（每行一个 JSON 对象），推荐优先级：

1. USB CDC Serial（开发最方便）
2. UART TTL（机器人内部稳定连接）
3. CAN / RS485（后续大型机器人）

协议详见 [`PROTOCOL.md`](PROTOCOL.md)。

## 安全原则

1. MOSS 上层不能发送“直接写 GPIO”命令。
2. MCU 必须再次检查电机角度和速度范围。
3. `system.estop` 优先级最高，触发后所有运动命令拒绝执行。
4. MCU 超过 Watchdog 时间未收到 RDK 心跳时，停止运动并进入安全状态。
5. 危险动作在 MOSS Mission 层仍需 Human-in-the-loop；MCU 是第二道本地安全闸门。
6. 所有硬件动作返回 `command_id`、结果、设备状态，供 Evidence Chain 记录。

## 与原 RDK 直控模式的关系

V1 会保留原 `scripts/dance.py` / `idle_motion.py` 作为 `direct-rdk` 兼容模式。

产品模式建议使用：

```text
MOSS_HARDWARE_BACKEND=mcu
MOSS_MCU_PORT=/dev/ttyACM0
MOSS_MCU_BAUD=115200
```

如果没有 MCU，开发机仍可使用 `mock`；RDK 早期样机可以继续使用 `direct-rdk`。
