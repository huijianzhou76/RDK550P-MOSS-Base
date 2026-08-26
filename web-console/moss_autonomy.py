from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class RiskRule:
    level: str
    keywords: tuple[str, ...]
    reason: str


RISK_RULES = (
    RiskRule(
        "critical",
        (
            "rm -rf",
            "格式化",
            "format disk",
            "清空磁盘",
            "删除系统",
            "修改密码",
            "重置密码",
            "disable firewall",
            "关闭防火墙",
        ),
        "任务可能造成不可逆的数据、系统或安全配置变更",
    ),
    RiskRule(
        "high",
        (
            "shutdown",
            "reboot",
            "关机",
            "重启系统",
            "sudo",
            "systemctl disable",
            "删除文件",
            "delete file",
            "安装软件",
            "卸载软件",
            "执行命令",
            "shell",
        ),
        "任务涉及操作系统或持久化环境变更",
    ),
    RiskRule(
        "medium",
        (
            "舵机",
            "servo",
            "跳舞",
            "执行动作",
            "启动服务",
            "停止服务",
            "重启服务",
            "camera",
            "摄像头",
            "拍照",
            "录音",
            "麦克风",
        ),
        "任务涉及真实设备、传感器或服务控制",
    ),
)


class PolicyEngine:
    """Local deterministic policy layer placed before the LLM/agent runtime."""

    def assess(self, text: str, priority: str = "normal") -> dict[str, Any]:
        normalized = text.lower()
        matched: list[dict[str, str]] = []
        level = "low"
        for rule in RISK_RULES:
            hits = [keyword for keyword in rule.keywords if keyword.lower() in normalized]
            if hits:
                matched.append({"level": rule.level, "reason": rule.reason, "keywords": ", ".join(hits[:8])})
                if RISK_ORDER[rule.level] > RISK_ORDER[level]:
                    level = rule.level

        if priority == "critical" and RISK_ORDER[level] < RISK_ORDER["medium"]:
            level = "medium"
            matched.append({
                "level": "medium",
                "reason": "任务被标记为 critical 优先级，因此提升审计等级",
                "keywords": "priority:critical",
            })

        approval_required = level in {"high", "critical"}
        return {
            "level": level,
            "score": {"low": 15, "medium": 45, "high": 75, "critical": 95}[level],
            "approval_required": approval_required,
            "matched_rules": matched,
            "policy": "human-approval-required" if approval_required else "auto-execution-allowed",
        }


class EvidenceStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "evidence.jsonl"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def append(
        self,
        mission_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        actor: str = "moss-core",
    ) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex[:16],
            "mission_id": mission_id,
            "kind": kind,
            "actor": actor,
            "payload": payload or {},
            "ts": int(time.time() * 1000),
        }
        item["sha256"] = self._digest(item)
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return item

    async def list(self, mission_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        limit = max(1, min(limit, 1000))
        try:
            rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []
        if mission_id:
            rows = [row for row in rows if row.get("mission_id") == mission_id]
        return rows[-limit:]


class AutonomyEngine:
    """Planner / executor / verifier orchestration around OpenClaw."""

    def __init__(self, agent: Any, policy: PolicyEngine, evidence: EvidenceStore, mode: str) -> None:
        self.agent = agent
        self.policy = policy
        self.evidence = evidence
        self.mode = mode

    def build_plan(self, mission: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = mission.get("prompt", "")
        risk = self.policy.assess(prompt, mission.get("priority", "normal"))
        return [
            {
                "id": "understand",
                "role": "planner",
                "title": "理解目标与约束",
                "description": "提取任务目标、输入、边界条件与交付标准",
                "status": "pending",
            },
            {
                "id": "risk",
                "role": "policy",
                "title": "风险与权限检查",
                "description": f"本地策略引擎评估为 {risk['level']} 风险",
                "status": "pending",
            },
            {
                "id": "execute",
                "role": "operator",
                "title": "Agent 与工具执行",
                "description": "调用 OpenClaw Agent；需要时使用已注册工具完成任务",
                "status": "pending",
            },
            {
                "id": "verify",
                "role": "verifier",
                "title": "独立结果验证",
                "description": "由独立验证会话检查完整性、风险和是否满足任务要求",
                "status": "pending",
            },
            {
                "id": "deliver",
                "role": "moss-core",
                "title": "证据封装与交付",
                "description": "保存执行证据、验证结果和最终交付",
                "status": "pending",
            },
        ]

    async def analyze(self, mission: dict[str, Any]) -> dict[str, Any]:
        risk = self.policy.assess(mission.get("prompt", ""), mission.get("priority", "normal"))
        plan = self.build_plan(mission)
        evidence = await self.evidence.append(
            mission["id"],
            "mission.analysis",
            {"risk": risk, "plan": plan},
            actor="policy+planner",
        )
        return {"risk": risk, "plan": plan, "evidence": evidence}

    async def execute(
        self,
        mission: dict[str, Any],
        on_stage: Callable[[str, int, dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        mission_id = mission["id"]
        risk = mission.get("risk")
        plan = mission.get("plan")
        if not isinstance(risk, dict) or not isinstance(plan, list) or not plan:
            analysis = await self.analyze(mission)
            risk = analysis["risk"]
            plan = analysis["plan"]
        else:
            await self.evidence.append(
                mission_id,
                "mission.execution_started",
                {"risk": risk, "plan_steps": [step.get("id") for step in plan]},
                actor="moss-core",
            )

        if risk["approval_required"] and not mission.get("approved_at"):
            await self.evidence.append(
                mission_id,
                "mission.approval_required",
                {"risk": risk},
                actor="policy",
            )
            return {
                "ok": False,
                "awaiting_approval": True,
                "risk": risk,
                "plan": plan,
            }

        await on_stage("planning", 20, {"plan": plan, "risk": risk})
        planner_prompt = (
            "[MOSS Planner Role]\n"
            "你是任务规划角色，只负责形成简洁可执行的计划和成功标准，不执行工具。\n"
            f"任务：{mission['prompt']}\n"
            "请输出：目标、关键步骤、成功标准、潜在风险。"
        )
        planner = await self.agent.chat(planner_prompt, f"mission-{mission_id}-planner")
        await self.evidence.append(mission_id, "agent.planner", planner, actor="planner")
        if not planner.get("ok"):
            return {"ok": False, "error": planner.get("error", "planner failed"), "risk": risk, "plan": plan}

        await on_stage("execution", 48, {"role": "operator"})
        operator_prompt = (
            "[MOSS Operator Role]\n"
            "你是执行角色。严格依据任务要求和规划结果执行。"
            "不要执行规划之外的高风险系统变更。遇到需要新权限的动作时停止并说明。\n"
            f"任务：{mission['prompt']}\n\n"
            f"规划结果：\n{planner.get('reply', '')}\n\n"
            "完成后给出实际执行结果、调用过的工具和可验证事实。"
        )
        operator = await self.agent.chat(operator_prompt, f"mission-{mission_id}-operator")
        await self.evidence.append(mission_id, "agent.operator", operator, actor="operator")
        if not operator.get("ok"):
            return {"ok": False, "error": operator.get("error", "operator failed"), "risk": risk, "plan": plan}

        await on_stage("verification", 78, {"role": "verifier"})
        verifier_prompt = (
            "[MOSS Verifier Role]\n"
            "你是独立验证角色，不继续执行任务。检查执行结果是否满足原任务、是否有遗漏、"
            "是否包含无法验证的断言，以及是否出现超出授权范围的动作。\n"
            f"原任务：{mission['prompt']}\n\n"
            f"规划：\n{planner.get('reply', '')}\n\n"
            f"执行结果：\n{operator.get('reply', '')}\n\n"
            "最后明确给出 VERDICT: PASS 或 VERDICT: REVIEW，并说明理由。"
        )
        verifier = await self.agent.chat(verifier_prompt, f"mission-{mission_id}-verifier")
        await self.evidence.append(mission_id, "agent.verifier", verifier, actor="verifier")
        if not verifier.get("ok"):
            return {"ok": False, "error": verifier.get("error", "verifier failed"), "risk": risk, "plan": plan}

        verification_text = verifier.get("reply", "")
        passed = "VERDICT: PASS" in verification_text.upper()
        await on_stage("evidence", 94, {"verification_passed": passed})

        delivery = {
            "result": operator.get("reply", ""),
            "planner": planner.get("reply", ""),
            "verification": verification_text,
            "verification_passed": passed,
            "risk": risk,
            "plan": plan,
            "sessions": {
                "planner": planner.get("session_id"),
                "operator": operator.get("session_id"),
                "verifier": verifier.get("session_id"),
            },
        }
        final_evidence = await self.evidence.append(
            mission_id,
            "mission.delivery",
            delivery,
            actor="moss-core",
        )
        delivery["evidence_id"] = final_evidence["id"]
        return {"ok": True, **delivery}


class HeartbeatEngine:
    """Background health observer. It can alert, but never performs destructive remediation."""

    def __init__(
        self,
        interval_seconds: int,
        metrics_provider: Callable[[], dict[str, Any]],
        voice_status_provider: Callable[[], Awaitable[dict[str, Any]]],
        emit: Callable[[str, dict[str, Any] | None], Awaitable[None]],
    ) -> None:
        self.interval_seconds = max(10, interval_seconds)
        self.metrics_provider = metrics_provider
        self.voice_status_provider = voice_status_provider
        self.emit = emit
        self._task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            metrics = self.metrics_provider()
            voice = await self.voice_status_provider()
            alerts: list[dict[str, Any]] = []
            temp = metrics.get("temperature_c")
            mem = (metrics.get("memory") or {}).get("percent")
            disk = (metrics.get("disk") or {}).get("percent")
            if isinstance(temp, (int, float)) and temp >= 80:
                alerts.append({"severity": "high", "kind": "temperature", "value": temp, "message": "设备温度达到告警阈值"})
            if isinstance(mem, (int, float)) and mem >= 90:
                alerts.append({"severity": "high", "kind": "memory", "value": mem, "message": "内存使用率达到告警阈值"})
            if isinstance(disk, (int, float)) and disk >= 92:
                alerts.append({"severity": "high", "kind": "disk", "value": disk, "message": "磁盘使用率达到告警阈值"})
            if not voice.get("active"):
                alerts.append({"severity": "medium", "kind": "voice", "value": voice.get("status"), "message": "语音守护进程当前未运行"})

            await self.emit("heartbeat.tick", {"metrics": metrics, "voice": voice, "alerts": alerts})
            for alert in alerts:
                await self.emit("heartbeat.alert", alert)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
