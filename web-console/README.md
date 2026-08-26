# MOSS 550W Web Control Plane

这是在原 `RDK550P-MOSS` 基础上新增的可视化控制层。它不替换原项目的 Voice Assistant、OpenClaw、摄像头和舵机脚本，而是把这些能力统一编排成一个可观察、可控制、可持续扩展的 MOSS 系统。

## 当前架构

```text
Browser / MOSS Web Console
        |
        v
FastAPI Control Plane
        |
        +--> MossRuntime / WebSocket Event Bus
        |
        +--> MissionStore ---------> OpenClaw Agent
        |
        +--> MemoryService --------> SOUL / IDENTITY / MEMORY / USER / TOOLS
        |
        +--> VoiceService ---------> voice-assistant.service
        |
        +--> HardwareService ------> scripts/snap.py / dance.py / idle_motion.py
        |
        +--> System Metrics -------> /proc / disk / thermal sensors
```

## 已实现功能

- **中央控制台**：查看 Agent、Mission、Voice、Camera、Hardware 实时状态
- **OpenClaw Agent 会话**：通过真实 `openclaw agent --session-id ... -m ...` 调用 Agent
- **任务中枢**：创建任务、排队、自动执行、进度状态、结果持久化
- **WebSocket 实时事件总线**：任务、Agent、摄像头、语音、硬件事件实时推送
- **视觉系统**：调用原 `scripts/snap.py` 并在网页显示最近快照
- **语音系统**：查看 / 启动 / 停止 / 重启 `voice-assistant.service`
- **硬件执行**：白名单方式调用原 `dance.py`、`idle_motion.py`
- **人格 / 记忆中心**：读取和保存 `SOUL.md.template`、`IDENTITY.md.template`、`MEMORY.md.template`、`USER.md.template`、`TOOLS.md.template`、`HEARTBEAT.md.template`、`AGENTS.md.template`
- **系统运行指标**：CPU load、内存、磁盘、温度、WebSocket 客户端数
- **Mock / RDK 双模式**：普通电脑可运行 UI Demo，RDK X5 切换到真机执行

## 本地 Mock 模式

```bash
cd web-console
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
MOSS_MODE=mock python app.py
```

打开：

```text
http://localhost:5500
```

Mock 模式不会调用真实摄像头、舵机和 OpenClaw，适合开发 UI 和验证任务流程。

## RDK X5 真机模式

前提：原 `RDK550P-MOSS` 已按主项目文档部署完成，以下能力可用：

- `openclaw` CLI
- `voice-assistant.service`
- `scripts/snap.py`
- `scripts/dance.py`
- `scripts/idle_motion.py`
- MIPI 摄像头 / USB Audio / PWM 舵机

手动启动：

```bash
cd web-console
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
MOSS_MODE=rdk python app.py
```

或者安装成 systemd 常驻服务：

```bash
sudo bash setup/install_web_console.sh
```

之后访问：

```text
http://<RDK-X5-IP>:5500
```

查看日志：

```bash
journalctl -u moss-web-console.service -f
```

## API

### System

- `GET /api/health`
- `GET /api/capabilities`
- `GET /api/system/metrics`
- `GET /api/events?limit=100`

### Agent

- `POST /api/agent/chat`

请求：

```json
{
  "message": "MOSS，分析当前环境",
  "session_id": null
}
```

### Missions

- `GET /api/missions`
- `POST /api/missions`
- `GET /api/missions/{mission_id}`
- `POST /api/missions/{mission_id}/run`

### Vision

- `POST /api/camera/snapshot`
- 最近快照通过 `/media/web-snapshot.jpg` 提供给前端

### Voice

- `GET /api/voice/status`
- `POST /api/voice/control`

支持动作：`start`、`stop`、`restart`

### Hardware

- `POST /api/hardware/servo`

当前白名单动作：`dance`、`idle`

### Memory

- `GET /api/memory`
- `GET /api/memory/{key}`
- `PUT /api/memory/{key}`

### Realtime

- `WS /ws`

连接后首先收到 `system.state` 与最近事件历史，随后持续收到任务、Agent、视觉、语音与硬件事件。

## 运行时数据

任务历史存储在：

```text
web-console/data/missions.json
```

该目录已加入 `.gitignore`，避免运行数据被提交到源码仓库。

## 重要说明

1. `main` 应继续保留为上游 Fork 基线。
2. 二创功能先在 `develop` / `feature/*` 分支开发。
3. 真机语音控制调用 `sudo systemctl`，部署账户需要具备相应 systemd 权限。
4. 当前 Web Console 面向可信局域网开发环境；如果暴露到公网，需要在下一阶段加入认证、TLS、审计与权限分级。
5. Memory 编辑器当前修改的是仓库中的 OpenClaw 模板。如果实际运行时 OpenClaw 使用独立 workspace 文件，应在部署阶段将模板与运行时人格文件做同步或切换为运行时路径。

## 下一阶段建议

- 连续摄像头视频流 / MJPEG / WebRTC
- RDK BPU YOLO 目标检测
- VLM 场景理解
- OpenClaw Tool 调用过程可视化
- 多 Agent 子智能体调度
- 任务计划树与决策证据链
- 主动 Heartbeat / Cron 任务面板
- 用户、权限与危险动作确认机制
- 运行时 SOUL/MEMORY 与模板双向同步
