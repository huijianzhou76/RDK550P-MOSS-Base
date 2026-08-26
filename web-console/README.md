# MOSS 550W Web Control Plane

这是在原 `RDK550P-MOSS` 基础上新增的可视化控制层和自主智能体编排层。它不替换原项目的 Voice Assistant、OpenClaw、摄像头和舵机脚本，而是把这些能力统一编排成一个可观察、可审批、可验证、可持续扩展的 MOSS 系统。

当前版本：**0.3.0**

## 当前架构

```text
Browser
  |-- MOSS Central Console  /
  `-- Autonomy Decision Center  /autonomy
                 |
                 v
          FastAPI Control Plane
                 |
      +----------+-----------+
      |                      |
MossRuntime              PolicyEngine
WebSocket / State        Risk / Approval
      |                      |
      +----------+-----------+
                 |
            Mission Core
                 |
        PLAN -> EXECUTE -> VERIFY
          |         |        |
       Planner   Operator  Verifier
          |         |        |
          +---- OpenClaw ----+
                 |
       EvidenceStore / SHA-256
                 |
      +----------+-----------+
      |          |           |
   Memory      Voice      Hardware
 SOUL etc.   systemd   Camera / Servo
```

## 已实现功能

- **中央控制台**：查看 Agent、Mission、Voice、Camera、Hardware 实时状态
- **自主决策中枢**：`/autonomy` 展示计划树、风险、审批、Agent 角色轨迹和证据链
- **OpenClaw Agent 会话**：通过真实 `openclaw agent --session-id ... -m ...` 调用 Agent
- **三角色任务执行**：Planner 负责规划、Operator 负责执行、Verifier 独立复核
- **任务中枢**：创建任务、排队、自动执行、进度状态、结果持久化
- **风险策略层**：本地确定性规则在 LLM 之前评估 low / medium / high / critical 风险
- **Human-in-the-loop**：high / critical 任务必须人工批准，未批准不会进入 Agent 执行阶段
- **安全 Chat 通道**：高风险请求不能通过普通 Web Chat 绕过 Mission 审批
- **Evidence Chain**：Planner、Operator、Verifier、审批、最终交付均写入 JSONL 证据记录并计算 SHA-256
- **Heartbeat**：后台周期观察系统指标和语音服务状态，只告警、不自动执行破坏性修复
- **WebSocket 实时事件总线**：任务、Agent、Heartbeat、摄像头、语音、硬件事件实时推送
- **视觉系统**：调用原 `scripts/snap.py` 并在网页显示最近快照
- **语音系统**：查看 / 启动 / 停止 / 重启 `voice-assistant.service`
- **硬件执行**：白名单方式调用原 `dance.py`、`idle_motion.py`
- **人格 / 记忆中心**：读取和保存 `SOUL.md.template`、`IDENTITY.md.template`、`MEMORY.md.template`、`USER.md.template`、`TOOLS.md.template`、`HEARTBEAT.md.template`、`AGENTS.md.template`
- **系统运行指标**：CPU load、内存、磁盘、温度、WebSocket 客户端数
- **Mock / RDK 双模式**：普通电脑可运行 UI Demo，RDK X5 切换到真机执行

## 任务生命周期

```text
Mission Created
      |
      v
Local Policy Assessment
      |
      +-- low / medium --------------------------+
      |                                          |
      +-- high / critical -> Awaiting Approval --+-- approved
                               |
                               `-- rejected -> STOP
                                                  |
                                                  v
                                               Planner
                                                  |
                                               Operator
                                                  |
                                               Verifier
                                             /          \
                                           PASS        REVIEW
                                             \          /
                                              Evidence
                                                  |
                                               Delivery
```

`Verifier` 使用与执行者不同的 session，并被明确要求不得继续执行工具，只负责独立检查任务满足程度、越权行为和不可验证断言。

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
http://localhost:5500/autonomy
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
http://<RDK-X5-IP>:5500/autonomy
```

查看日志：

```bash
journalctl -u moss-web-console.service -f
```

Heartbeat 默认 30 秒检查一次，可通过环境变量调整：

```bash
MOSS_HEARTBEAT_INTERVAL=60
```

最小值会被限制为 10 秒。

## API

### System

- `GET /api/health`
- `GET /api/capabilities`
- `GET /api/system/metrics`
- `GET /api/events?limit=100`

### Policy / Evidence

- `POST /api/policy/assess`：预判文本风险
- `GET /api/evidence?mission_id=<id>&limit=200`：查看审计证据链

### Agent

- `POST /api/agent/chat`

普通 Chat 是低风险交互通道。策略判定为 high / critical 时返回 HTTP 403，要求改用 Mission 审批流程。

### Missions

- `GET /api/missions`
- `POST /api/missions`
- `GET /api/missions/{mission_id}`：同时返回该任务 Evidence
- `POST /api/missions/{mission_id}/run`
- `POST /api/missions/{mission_id}/approval`

批准：

```json
{
  "decision": "approve",
  "note": "已确认任务范围"
}
```

拒绝：

```json
{
  "decision": "reject",
  "note": "不允许执行系统级变更"
}
```

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

新增事件包括：

- `system.autonomy_ready`
- `heartbeat.tick`
- `heartbeat.alert`
- `mission.approval_required`
- `mission.approved`
- `mission.rejected`
- `mission.progress`
- `mission.completed`
- `mission.review_required`
- `agent.blocked_by_policy`

## 运行时数据

```text
web-console/data/missions.json   # Mission 状态
web-console/data/evidence.jsonl  # 执行审计证据链
```

`web-console/data/` 已加入 `.gitignore`，避免运行数据进入源码仓库。

## 当前安全边界

1. 风险判断位于 LLM 之前，避免完全依赖模型自律。
2. high / critical Mission 必须有 `approved_at` 才会进入 Planner / Operator / Verifier。
3. Heartbeat 只发现与报告异常，不自动执行破坏性修复。
4. 硬件直接 API 仍采用动作白名单。
5. Evidence 使用 SHA-256 记录内容摘要，便于发现审计记录被意外修改；它不是数字签名，不能替代可信签名系统。
6. 当前 Web Console 面向可信局域网开发环境；暴露公网前仍需认证、TLS、用户角色与 CSRF 防护。
7. OpenClaw 本身可拥有更多工具权限，因此生产环境还应在 OpenClaw 工具层增加对应的权限策略，而不是只依赖 Web 层。

## 分支策略

- `main`：保留上游 Fork 基线
- `develop`：MOSS 二创开发主线
- `feature/web-console`：当前控制平面 / 自主系统开发分支

## 下一阶段

- OpenClaw Tool 调用结构化追踪，而不是只保存最终文本
- RDK BPU YOLO 持续视觉感知
- VLM 场景理解与环境事件
- Memory 运行时 workspace 双向同步
- 子 Agent 能力注册表和动态路由
- 用户身份、RBAC、设备级权限
- Evidence 签名 / append-only 审计
- 任务取消、暂停、恢复和超时预算
- Heartbeat / Cron 可视化规则编辑器
