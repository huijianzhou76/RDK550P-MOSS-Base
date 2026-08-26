# MOSS 550W Web Console

在原 RDK550P-MOSS 基础上新增的 Web 控制层。

## 架构

Web Console → FastAPI → WebSocket → OpenClaw Agent → RDK scripts → Camera / Servo / Voice

## 本地 Demo 模式

```bash
cd web-console
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
MOSS_MODE=mock python app.py
```

浏览器打开：`http://localhost:5500`

## RDK X5 真机模式

确保原项目已经完成部署，`openclaw` CLI、摄像头与 PWM 脚本均可正常运行：

```bash
cd web-console
source .venv/bin/activate
MOSS_MODE=rdk python app.py
```

## 当前 API

- `GET /api/health`：系统状态
- `POST /api/agent/chat`：调用 OpenClaw Agent
- `POST /api/camera/snapshot`：调用原 `scripts/snap.py`
- `POST /api/hardware/servo`：执行 `dance.py` 或 `idle_motion.py`
- `WS /ws`：实时状态与执行事件

当前版本是第一阶段 MVP。后续将继续加入：语音守护进程控制、实时摄像头预览、任务中心、Memory/SOUL 编辑器、权限系统、OpenClaw 会话管理、设备拓扑与运行指标。
