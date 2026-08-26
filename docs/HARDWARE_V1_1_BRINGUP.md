# MOSS Hardware V1.1 — Bring-up Checklist

目标：零件到货后，不一次性全接。按“电源 → MCU → 安全 → 单舵机 → 双舵机 → 外设 → RDK → MOSS”逐层点亮。

## Phase 0 — 不接舵机，先准备

- RDK X5 暂时与 Servo PSU 分开。
- ESP32-S3 只通过开发电脑 USB 供电。
- 接好物理 E-STOP：3.3V → NC → GPIO4，GPIO4 外接 10k 下拉到 GND。
- GPIO12 的 Servo Power Switch 先不接舵机负载。
- 准备万用表。

**如果 E-STOP 没接，V1.1 固件会把开路视为故障并进入急停，这是正常行为。**

## Phase 1 — 编译并刷 ESP32

```bash
cd moss-mcu/esp32
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
idf.py -p <ESP32_DOWNLOAD_PORT> flash monitor
```

启动日志应该包含：

```text
MOSS MCU Hardware V1.1 boot firmware=0.2.0 protocol=1.1
MOSS MCU ready
```

如果 GPIO4 处于 LOW，应看到 physical_boot E-STOP。

## Phase 2 — 只测安全输出

不要接舵机。

1. 测 GPIO12：健康且未急停时应为 HIGH。
2. 按下物理 E-STOP：GPIO12 应变 LOW。
3. 旋转释放急停：GPIO12 不应自动恢复，必须由 RDK/CLI 发送 `estop-clear --confirm`。
4. 确认 Power Switch 输出：GPIO12 LOW 时 Servo Rail=0V，HIGH 时≈5V。

只有这一步通过，才能继续。

## Phase 3 — 接 RDK↔MCU 链路

推荐首台样机：RDK USB → CP2102/CH340 3.3V USB-UART → ESP32 GPIO17/18。

```text
USB-UART TX -> ESP32 GPIO18
USB-UART RX <- ESP32 GPIO17
USB-UART GND -> ESP32 GND
```

在 RDK：

```bash
ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
cd /root/.openclaw/workspace
python3 -m venv .hardware-venv
.hardware-venv/bin/pip install pyserial
PYTHONPATH=moss-hardware/python \
.hardware-venv/bin/python -m moss_hardware.cli --port /dev/ttyUSB0 ping
```

继续：

```bash
PYTHONPATH=moss-hardware/python .hardware-venv/bin/python -m moss_hardware.cli --port /dev/ttyUSB0 capabilities
PYTHONPATH=moss-hardware/python .hardware-venv/bin/python -m moss_hardware.cli --port /dev/ttyUSB0 status
```

预期：firmware=`0.2.0`、protocol=`1.1`。

保持连接 15 秒再查 `status`：由于 Gateway 每 2 秒 heartbeat，`link_degraded` 应保持 false。

## Phase 4 — 验证掉线 Watchdog

先保持**不接舵机**。

1. 正常启动 CLI/Web Gateway。
2. 确认 GPIO12 HIGH。
3. 断开 RDK↔MCU 串口。
4. 约 5 秒后 MCU 进入 `link_degraded`。
5. 约 10 秒后 GPIO12 应 LOW，Servo Power Switch 输出=0V。
6. 恢复链接后，如果没有 E-STOP，Gateway heartbeat 会恢复控制链。

## Phase 5 — 只接一个 Yaw 舵机

先断电。

- Servo PSU→Power Switch→INA219→Yaw Servo V+。
- Yaw Signal→GPIO13。
- Servo GND 与 ESP32 GND 共地。
- 先不要接 Pitch Servo。

上电后只做小角度：

```bash
... cli --port /dev/ttyUSB0 move --yaw 10 --pitch 0 --speed 0.2
... cli --port /dev/ttyUSB0 move --yaw -10 --pitch 0 --speed 0.2
... cli --port /dev/ttyUSB0 center
```

确认没有机械顶死、抖动、供电掉压，再扩大到 ±30°。

**不要第一下就跑 ±70°。**

## Phase 6 — 接 Pitch 舵机

Pitch Signal→GPIO14。重复小角度测试：

```bash
... move --yaw 0 --pitch 8 --speed 0.2
... move --yaw 0 --pitch -8 --speed 0.2
```

装进最终外壳之后才重新确定 `MOSS_YAW_MIN/MAX_DEG` 和 `MOSS_PITCH_MIN/MAX_DEG`。

## Phase 7 — 传感器

接 SSD1306、INA219、AHT20 后：

```bash
... sensors
... display "MOSS ONLINE"
```

预期：

- `power_valid=true`
- `servo_bus_v` 接近 5V（Servo Power 开启时）
- 急停后 `servo_bus_v` 应接近 0V（取决于 INA219 最终安装位置）
- `environment_valid=true`
- 温湿度值处于合理范围

如果某个模块缺失，`system.capabilities`/status 的 peripheral detection 会体现，不应导致 MCU 整体启动失败。

## Phase 8 — 红眼

先把 brightness 调低：

```bash
... light --brightness 0.15
... light --brightness 0.5
```

确认 MOSFET 不发热、LED 模块不超过额定电流。随后测试 E-STOP：红眼应进入告警亮度，舵机电源应关闭。

## Phase 9 — 红外

接 VS1838B/TSOP38238 与 940nm IR TX：

```bash
... ir-learn desk_tv_power --timeout-ms 4000
... ir-list
... ir-send desk_tv_power --repeat 1
```

学习时遥控器距离接收头约 10–30cm，按键保持正常短按。V1.1 IR slot 在 RAM 中，ESP32 重启后会清空。

## Phase 10 — 摄像头与语音

这两部分接 RDK，不接 ESP32：

- IMX477 → RDK X5 MIPI CSI
- USB Mic/Speaker → RDK USB

先分别跑原 RDK550P-MOSS 的 camera/audio 测试，再启动 MOSS Web Console。

## Phase 11 — 切换 Web Console 到 MCU 模式

systemd/环境配置：

```text
MOSS_MODE=rdk
MOSS_HARDWARE_BACKEND=mcu
MOSS_MCU_PORT=auto
MOSS_MCU_BAUD=115200
```

然后：

```bash
sudo systemctl restart moss-web-console.service
journalctl -u moss-web-console.service -f
```

测试：

```text
GET  /api/hardware/mcu/capabilities
GET  /api/hardware/mcu/status
GET  /api/hardware/mcu/sensors
POST /api/hardware/mcu/head/move
POST /api/hardware/mcu/estop
POST /api/hardware/mcu/estop/clear
POST /api/hardware/mcu/ir/learn
POST /api/hardware/mcu/ir/send
```

## Phase 12 — 最终安全验收

全部通过才允许 MOSS Agent 调真实运动：

- [ ] 软件 E-STOP 能在 1 秒内停止运动并切 Servo Power
- [ ] 物理 E-STOP 能锁存，释放按钮后不会自行恢复
- [ ] 串口断开约 10 秒 Servo Power 自动关闭
- [ ] 重新连接不会恢复旧运动队列
- [ ] Yaw/Pitch 极限不会撞机械结构
- [ ] 两个舵机同时运动时 RDK 不重启
- [ ] Servo Rail 无明显跌压/过热
- [ ] INA219 能读到合理电压/电流
- [ ] 红眼 MOSFET 和线材不过热
- [ ] 摄像头/音频/MCU 三条链路可同时运行
- [ ] MOSS Web Console 能看到 MCU status/capabilities

通过后，才进入下一阶段：Vision Event → Mission → MOSS 自动转头/观察/验证。
