# Hardware Migration Review: moss-xiaozhi → MOSS 550W

Source reviewed: `yokochen222/moss-xiaozhi` (MIT License).

本文件记录哪些设计适合吸收进我们的 MOSS，哪些不应该直接搬。

## 1. 建议吸收 / 重写成我们的 HAL

### ESP32 板级配置

`main/boards/bread-compact-wifi/config.h` 把 I2S 麦克风、I2S 扬声器、按钮、LED、I2C OLED 等 GPIO 集中配置。这种“Board Profile”模式值得保留。

我们的做法：在 `moss-mcu` 中用 ESP-IDF Kconfig / board profile 管理 UART、Servo、LED、OLED、IR、Sensor 引脚，不让业务代码散落硬编码 GPIO。

### OLED / 状态显示

源项目在 `compact_wifi_board.cc` 中初始化 SSD1306/SH1106 OLED，并把显示对象抽象为 `Display`。

我们的做法：后续实现 `display.text` / `display.state` 驱动，显示 MOSS 状态、网络、录音、任务等级、错误码。显示器只接受有限状态命令，不运行任意 UI 代码。

### 双轴运动

源项目 `main/mcp/tools/motor.cc` 使用 FreeRTOS Task 驱动 Pitch / Yaw 双步进电机，并把上层工具语义定义为“角度移动”。这一点非常适合 MOSS。

我们的做法：V1 先支持现有 PTK7465 双舵机；协议保持 `head.move(yaw_deg, pitch_deg, speed)`。后续切换 24BYJ48 / ULN2003 或其他电机时，仅更换 MCU motor backend，不改变 MOSS Core。

### LED / MOSS 眼灯

源项目有 `lamp_eye.cc`、`lamp_bar.cc` 等 MCP Tool。

我们的做法：统一成 `light.set(target, mode, brightness, rgb)`，由 MCU 根据硬件类型映射到 GPIO / PWM / WS2812 / 数码管。

### 红外

源项目 `infrared.cc` 已经实现红外相关工具，但其中包含具体家庭设备学习得到的红外波形数据，这些数据不应直接作为我们的默认产品数据。

我们的做法：后续实现 `ir.learn`、`ir.send`、`ir.delete`，红外码作为运行时用户数据存储，不把个人设备码写死在源码中。

## 2. 不直接移植的部分

### 小智 Application / Chat 生命周期

我们的 AI Core 已经采用 RDK X5 + OpenClaw + Mission / Planner / Operator / Verifier，不需要引入小智自己的 Application 状态机作为主大脑。

### 小智专用 MCP Server Glue

源项目硬件工具继承其 `McpTool` 并直接注册到小智 MCP Server。我们的硬件能力应该先进入 `moss-hardware` HAL，再由 MOSS Control Plane / OpenClaw Tool Adapter 暴露，避免硬件和某一个 Agent Runtime 强绑定。

### GPIO 硬编码

源项目部分工具直接在实现文件中定义 GPIO。我们不复制这种方式，统一通过 board profile / Kconfig。

### 红外波形样本

不复制作者个人设备的学习码，只参考驱动和学习/发送流程。

## 3. 当前我们的目标结构

```text
MOSS Core / OpenClaw
        |
        v
Mission / Risk / Evidence
        |
        v
MOSS Hardware Tool Adapter
        |
        v
moss-hardware (RDK X5 HAL)
        |
   JSONL Serial v1
        |
        v
moss-mcu (ESP32)
        |
  +-----+-----+------+-------+
  |     |     |      |       |
Servo  LED  OLED    IR     Sensors
```

## 4. License 策略

- `moss-xiaozhi`: MIT，可在保留版权和许可文本的条件下复用/修改代码。
- 我们当前 Hardware V1 的协议、Gateway 和 ESP32 固件主体为重新设计实现，不直接复制其硬件工具源码。
- 如果未来直接移植其某个驱动文件，会在文件头标注来源，并在 `THIRD_PARTY_NOTICES.md` 保留 MIT 归属。
