# MOSS Hardware V1.1 — 接线图与 GPIO 映射

> 本文件是 Hardware V1.1 的权威接线版本。若早期 README/BOM 示意与这里不一致，以这里为准。

## 1. ESP32-S3 默认 GPIO

| 功能 | ESP32-S3 GPIO | 方向 | 电平/协议 |
|---|---:|---|---|
| RDK Link TX | 17 | OUT | UART 3.3V TTL / 115200 |
| RDK Link RX | 18 | IN | UART 3.3V TTL / 115200 |
| Yaw Servo | 13 | OUT | 50Hz PWM |
| Pitch Servo | 14 | OUT | 50Hz PWM |
| Servo Power EN | 12 | OUT | Active-High 3.3V control |
| Physical E-STOP Sense | 4 | IN | Fail-safe active LOW |
| I2C SDA | 8 | I/O | 3.3V I2C |
| I2C SCL | 9 | OUT | 3.3V I2C |
| IR Receiver | 10 | IN | Demodulated 38kHz IR |
| IR Transmitter | 11 | OUT | RMT + 38kHz carrier, drives transistor |
| Red Eye PWM | 21 | OUT | 5kHz PWM, drives MOSFET gate |
| MCU Status LED | 48 | OUT | Dev-board LED where available |

所有 GPIO 都是 3.3V 逻辑。**任何 ESP32 GPIO 都不允许直接输入 5V。**

## 2. RDK X5 ↔ ESP32 通讯（首台样机推荐）

为了避免依赖某个 RDK GPIO UART 的 Linux 设备名，首台样机推荐使用一个 3.3V USB-UART：

```text
RDK X5 USB-A
    │
    └── USB-UART (CP2102 / CH340, 3.3V TTL)
           TX ─────────────> ESP32 GPIO18 (RX)
           RX <───────────── ESP32 GPIO17 (TX)
           GND ───────────── ESP32 GND
```

如果 ESP32 已经由自己的 USB 口供电，USB-UART 的 VCC **不要连接**，只接 TX、RX、GND，避免双路电源回灌。

RDK 端默认自动寻找 `/dev/ttyACM*`、`/dev/ttyUSB*`、`/dev/serial/by-id/*`。也可以显式设置：

```bash
export MOSS_HARDWARE_BACKEND=mcu
export MOSS_MCU_PORT=/dev/ttyUSB0
export MOSS_MCU_BAUD=115200
```

## 3. 舵机电源：必须独立

推荐最终电源链：

```text
独立 5V/5A Servo PSU
       +5V
        │
        ▼
3.3V 控制兼容 High-Side Power Switch
        │  EN <──── ESP32 GPIO12
        ▼
     INA219
   VIN+    VIN-
    │       │
    └───────┴──────── +5V Servo Rail
                         │
                  ┌──────┴──────┐
                  │             │
             Yaw PTK7465   Pitch PTK7465

Servo PSU GND ─────────── ESP32 GND ───────── USB-UART GND
```

INA219 放在电源开关**之后**，这样急停断电后 `servo_bus_v` 应该接近 0V，可以用传感器验证真正断电。

两个舵机信号：Yaw Signal→GPIO13、Pitch Signal→GPIO14。舵机 V+ 只接 Servo Rail，不接 RDK GPIO 5V。

建议在舵机附近的 Servo Rail 与 GND 并联 1000–2200µF / 10V 电解电容，注意极性。

## 4. Servo Power Switch

选购要求：5V 直流、高边/负载开关、持续电流至少 5A、控制输入兼容 3.3V、**Active-High**。

```text
GPIO12 LOW  -> Servo 5V OFF
GPIO12 HIGH -> Servo 5V ON
```

固件在以下情况会拉低 GPIO12：软件急停、物理急停、RDK heartbeat 超时进入 Watchdog Stop、MCU 启动尚未通过安全检查。

如果你买到的是 Active-Low 模块，不要直接接；先修改固件极性配置再使用。

## 5. 物理急停：Fail-Safe 接法

推荐使用自锁蘑菇头急停按钮的 NC 常闭触点：

```text
ESP32 3.3V
    │
    └──── [ E-STOP NC ] ───── GPIO4
                                │
                              10kΩ
                                │
                               GND
```

健康：NC 闭合 → GPIO4=HIGH。

按下急停：NC 断开 → 10k 下拉 → GPIO4=LOW。

插头掉落/线断：GPIO4 同样被下拉为 LOW，因此也会急停。

ESP32 固件同时启用了内部 pull-down，外部 10k 是推荐的硬件冗余。

如果购买 2NC 急停按钮，第二组 NC 可以在后续硬件版本中进入独立电源安全链，但第一版不要把未知额定的按钮触点直接串进 5A 电机母线。

## 6. MOSS 红眼

推荐购买 5V 单色红色 LED 环/模块，并确认模块自身已有 LED 限流。使用逻辑级 N-MOSFET做低边 PWM：

```text
Servo PSU +5V ───── LED Ring +
LED Ring - ───────── MOSFET Drain
MOSFET Source ────── GND
ESP32 GPIO21 ─100Ω── MOSFET Gate
                         │
                       10kΩ
                         │
                        GND
```

不要让 GPIO21 直接承担 LED 电流。

急停时固件将红眼提升到告警亮度；正常在线默认约 35%。

## 7. I2C 总线

```text
ESP32 GPIO8  SDA ──┬── SSD1306 SDA (0x3C)
                   ├── INA219 SDA  (0x40)
                   └── AHT20 SDA   (0x38)

ESP32 GPIO9  SCL ──┬── SSD1306 SCL
                   ├── INA219 SCL
                   └── AHT20 SCL

ESP32 3.3V ─────────── OLED/AHT20/INA219 logic supply
ESP32 GND  ─────────── GND
```

多数成品模块已经带 I2C 上拉；如果多个模块都带很强上拉导致总并联阻值过低，再移除多余上拉。首台样机先按模块默认连接。

## 8. IR 学习

```text
VS1838B / TSOP38238
VCC ── 3.3V
GND ── GND
OUT ── GPIO10
```

将遥控器对准接收头，在 Web/API 调 `ir.learn`，学习窗口默认 4 秒。

## 9. IR 发射

GPIO 不直接驱动红外 LED：

```text
ESP32 GPIO11 ── 1kΩ ── NPN Base (2N2222/S8050)
NPN Emitter ─────────── GND
NPN Collector ───────── IR LED Cathode
Servo PSU +5V ─ 100Ω ── IR LED Anode
```

固件 RMT 发射端会在 mark 期间叠加约 38kHz carrier。首台样机建议先用 1 颗 940nm LED；如果距离不足，再根据晶体管和 LED 脉冲额定值调整驱动，不要先盲目减小限流电阻。

## 10. OLED

SSD1306 128x64 I2C：VCC→3.3V、GND→GND、SDA→GPIO8、SCL→GPIO9。V1.1 内置轻量 ASCII 字库，主要显示 `MOSS ONLINE`、状态、短英文/数字信息。

## 11. 摄像头与音频

IMX477 直接连接 RDK X5 MIPI CSI，不经过 ESP32。USB Mic/Speaker 同样直接连接 RDK X5。

```text
IMX477 ── MIPI ──> RDK X5
USB Audio ────────> RDK X5 USB

ESP32 只做实时硬件控制，不处理主摄像头和主语音推理。
```

## 12. 共地规则

必须共地的是：ESP32 GND、Servo PSU GND、USB-UART GND、红眼 MOSFET Source、IR NPN Emitter、所有 I2C 模块 GND、两只舵机 GND。

RDK 与 ESP32 通过 USB-UART/USB 已形成信号地连接。不要把 RDK 5V 和 Servo PSU 5V 正极并联。

## 13. 首次上电前用万用表检查

- Servo Rail 对 GND 无短路。
- ESP32 GPIO 到 GND 无 5V 直连。
- 急停健康状态 GPIO4≈3.3V，按下后≈0V。
- Servo Power EN=LOW 时 INA219 后端 Servo Rail≈0V。
- Servo Power EN=HIGH 时 Servo Rail≈5V。
- 所有电源正负极性正确。

完成这些检查后，才插舵机。
