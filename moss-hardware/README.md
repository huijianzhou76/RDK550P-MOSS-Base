# MOSS Hardware Layer — V1.1

`moss-hardware` 是 RDK X5 上的硬件抽象层（HAL）。它位于 MOSS AI / Web Control Plane 与实时 MCU 之间，让上层只表达“要做什么”，不允许 Agent 直接操纵 GPIO。

## 实体机从这里开始

准备制作第一台实体 MOSS 时，请按这个顺序阅读：

1. [`../docs/HARDWARE_V1_1_BOM.md`](../docs/HARDWARE_V1_1_BOM.md) — 锁定采购清单，以及这次明确不要购买的器件。
2. [`../docs/HARDWARE_V1_1_WIRING.md`](../docs/HARDWARE_V1_1_WIRING.md) — 权威 GPIO、电源、共地、急停、IR 与 I2C 接线。
3. [`../docs/HARDWARE_V1_1_BRINGUP.md`](../docs/HARDWARE_V1_1_BRINGUP.md) — 零件到货后的逐阶段点亮/验收顺序。
4. [`PROTOCOL.md`](PROTOCOL.md) — RDK ↔ MCU Protocol 1.1。

不要一收到零件就把全部模块同时接上。Bring-up 必须先验证电源和 E-STOP，再接单个舵机。

## 架构

```text
OpenClaw / Mission / Planner / Verifier
                  |
                  v
       MOSS Control Plane (RDK X5)
                  |
                  v
       moss-hardware / MCU Gateway
                  |
          USB-UART / Serial
                  |
                  v
          moss-mcu / ESP32-S3
                  |
       +----------+----------+----------+
       |          |          |          |
   Head Motion   Safety    Peripherals   IR
   Yaw/Pitch     E-STOP    Eye/OLED      Learn
   Queue/Ease    Power     Sensors       Replay
```

RDK X5 负责：LLM、视觉、长期记忆、任务规划、风险判断、工具编排、Web 控制台、Evidence。

ESP32-S3 负责：实时舵机控制、机械限位、运动队列、物理急停、舵机电源切断、红眼、OLED、红外、传感器、链路 Watchdog。

## 为什么增加 MCU 层

原 RDK550P-MOSS 直接通过 Linux PWM 控制两个舵机，适合第一代 Demo。但实体机器人增加灯光、显示、红外、传感器、安全输入后，实时执行不应继续由 Linux/LLM 进程承担。

`moss-xiaozhi` 的 ESP32 板级实现作为 MIT 许可的工程参考；Hardware V1.1 不绑定小智应用层，而是重新定义了适合 RDK X5 + MOSS Core 的协议、安全状态机和 Gateway。

## Hardware V1.1 已实现能力

- `system.heartbeat`：RDK Gateway 自动周期心跳
- `system.ping`：链路与 firmware/protocol 版本检查
- `system.capabilities`：实际设备能力、限位、GPIO、外设探测握手
- `system.status`：急停、运动、电源、链路和外设状态
- `system.estop` / `system.estop_clear`：锁存急停与人工恢复
- `head.move` / `head.center`：FreeRTOS 队列 + 余弦 easing 双轴运动
- `light.set`：单色红眼 PWM
- `display.text`：SSD1306 128x64 状态显示
- `sensor.read`：INA219 舵机母线 + AHT20 温湿度
- `ir.learn` / `ir.send` / `ir.list`：38kHz RMT 红外学习与重放

## 安全层

1. 上层没有任意 GPIO 写接口。
2. MCU 再次检查 yaw/pitch/speed。
3. 物理 E-STOP 推荐 NC 常闭 fail-safe 接法；断线也视为故障。
4. E-STOP 会清空运动队列、停止 PWM 并通过 GPIO12 切断舵机电源。
5. RDK Gateway 默认每 2 秒 heartbeat；5 秒失联拒绝新运动，10 秒失联切断舵机电源。
6. 恢复通讯不会继续旧运动队列。
7. Mission 层 Human-in-the-loop 与 MCU 本地安全闸门同时存在。

## 运行后端

兼容模式仍保留原 `scripts/dance.py` / `idle_motion.py` 的 `direct-rdk`。

实体 MCU 模式：

```text
MOSS_MODE=rdk
MOSS_HARDWARE_BACKEND=mcu
MOSS_MCU_PORT=auto
MOSS_MCU_BAUD=115200
```

`auto` 会优先发现 `/dev/ttyACM*`、`/dev/ttyUSB*`、`/dev/serial/by-id/*`。

没有 MCU 时使用 `mock`；硬件未完成安全验收前，systemd 默认仍保持 `direct-rdk`，避免误动作。
