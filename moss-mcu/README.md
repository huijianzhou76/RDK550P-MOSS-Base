# MOSS MCU

`moss-mcu` 是实体 MOSS 的实时控制“小脑”。V1 目标平台为 ESP32 / ESP32-S3，使用 ESP-IDF。

## V1 已实现

- UART JSONL 协议
- `system.ping`
- `system.status`
- `system.estop`
- `system.estop_clear`
- `head.move`
- `head.center`
- 双轴舵机 50Hz PWM
- Yaw / Pitch 安全角度限制
- RDK 心跳超时保护
- 状态 LED
- `light.set` 基础开关映射

`display.text`、`sensor.read` 已保留协议位，但固件 0.1.0 先返回 `NOT_IMPLEMENTED`。

## 重要：不要直接按默认 GPIO 接线

`main/Kconfig.projbuild` 中 GPIO 只是开发默认值。刷机前必须根据最终 ESP32 开发板与 PCB 修改：

- RDK Link TX / RX
- Yaw Servo Signal
- Pitch Servo Signal
- Status LED
- 安全角度范围

建议先不接舵机电源，只连接串口完成协议测试。

## 编译

安装 ESP-IDF 5.x 后：

```bash
cd moss-mcu/esp32
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
```

在 `MOSS MCU Hardware` 菜单中确认 GPIO 与机械安全角度。

## 第一次 Bring-up 顺序

1. 只连接 ESP32 USB，完成编译/刷机。
2. RDK X5 与 ESP32 只连接 UART TX/RX/GND，不接舵机。
3. 从 RDK 发送 `system.ping`，确认能收到 JSON response。
4. 发送 `system.status`，检查 `estop=false`。
5. 接一只舵机的信号线和独立 5V 电源，共 GND。
6. 将安全范围临时缩到 ±10°，测试 `head.move`。
7. 再接第二只舵机。
8. 完成机械限位测量后再扩大角度。
9. 测试拔掉 UART/关闭 RDK 后 Watchdog 是否阻止继续运动。
10. 测试 `system.estop` 后运动命令是否全部被拒绝。

## 电源原则

舵机不要从 ESP32 3.3V 供电，也不建议由开发板 USB 5V 承担最终双舵机峰值电流。

推荐：

```text
5V Servo PSU
  +5V  -> Servo VCC x2
  GND  -> Servo GND x2
  GND  -> ESP32 GND
  GND  -> RDK X5 GND (UART TTL 模式)

ESP32 GPIO -> Servo Signal
```

如果使用 USB 隔离串口或其他隔离通信，应按对应方案重新设计地线。

## 后续固件模块

下一阶段计划：

- FreeRTOS Motion Queue + 平滑缓动
- 舵机 / 24BYJ48 步进电机 backend 可切换
- OLED SSD1306 / SH1106
- MOSS Eye RGB / 灯条
- 红外学习与发送
- 温度、电源、电池、电流传感器
- 物理 E-STOP 按键
- 固件版本握手与 capabilities
- OTA / 维护模式（不暴露给普通 Agent Tool）
