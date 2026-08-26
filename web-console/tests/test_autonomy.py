import asyncio
import tempfile
import unittest
from pathlib import Path

from moss_autonomy import AutonomyEngine, EvidenceStore, PolicyEngine


class DummyAgent:
    async def chat(self, message, session_id=None):
        if "Verifier Role" in message:
            reply = "VERDICT: PASS\n任务满足原始要求，未发现越权动作。"
        elif "Planner Role" in message:
            reply = "目标：完成任务。\n步骤：分析、执行、验证。\n成功标准：结果完整可验证。"
        else:
            reply = "执行完成。工具调用：无。可验证事实：mock result."
        return {"ok": True, "session_id": session_id, "reply": reply}


class PolicyEngineTests(unittest.TestCase):
    def test_low_risk_task_is_auto_allowed(self):
        result = PolicyEngine().assess("总结今天的项目进展")
        self.assertEqual(result["level"], "low")
        self.assertFalse(result["approval_required"])

    def test_high_risk_system_task_requires_approval(self):
        result = PolicyEngine().assess("请执行命令并重启系统")
        self.assertIn(result["level"], {"high", "critical"})
        self.assertTrue(result["approval_required"])

    def test_hardware_task_is_medium_risk(self):
        result = PolicyEngine().assess("让舵机跳舞")
        self.assertEqual(result["level"], "medium")
        self.assertFalse(result["approval_required"])


class AutonomyEngineTests(unittest.TestCase):
    def test_high_risk_mission_stops_for_approval(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                store = EvidenceStore(Path(tmp))
                engine = AutonomyEngine(DummyAgent(), PolicyEngine(), store, "mock")
                mission = {
                    "id": "m-high",
                    "title": "system action",
                    "prompt": "执行命令并重启系统",
                    "priority": "normal",
                }
                stages = []

                async def on_stage(stage, progress, payload):
                    stages.append((stage, progress))

                result = await engine.execute(mission, on_stage)
                self.assertFalse(result["ok"])
                self.assertTrue(result["awaiting_approval"])
                self.assertEqual(stages, [])
                evidence = await store.list("m-high")
                kinds = {row["kind"] for row in evidence}
                self.assertIn("mission.approval_required", kinds)

        asyncio.run(scenario())

    def test_approved_mission_runs_three_roles_and_verifies(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                store = EvidenceStore(Path(tmp))
                engine = AutonomyEngine(DummyAgent(), PolicyEngine(), store, "mock")
                mission = {
                    "id": "m-approved",
                    "title": "system action",
                    "prompt": "执行命令并重启系统",
                    "priority": "normal",
                    "approved_at": 123,
                }
                stages = []

                async def on_stage(stage, progress, payload):
                    stages.append(stage)

                result = await engine.execute(mission, on_stage)
                self.assertTrue(result["ok"])
                self.assertTrue(result["verification_passed"])
                self.assertEqual(stages, ["planning", "execution", "verification", "evidence"])
                evidence = await store.list("m-approved")
                kinds = [row["kind"] for row in evidence]
                self.assertIn("agent.planner", kinds)
                self.assertIn("agent.operator", kinds)
                self.assertIn("agent.verifier", kinds)
                self.assertIn("mission.delivery", kinds)
                self.assertTrue(all(len(row["sha256"]) == 64 for row in evidence))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
