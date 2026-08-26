import asyncio
import tempfile
import unittest
from pathlib import Path

from moss_services import MemoryService, MissionStore


class MissionStoreTests(unittest.TestCase):
    def test_create_and_update_mission(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MissionStore(Path(tmp))

            async def scenario():
                mission = await store.create("test", "check system", "high")
                self.assertEqual(mission["status"], "queued")
                self.assertEqual(mission["priority"], "high")
                updated = await store.update(mission["id"], status="completed", progress=100)
                self.assertIsNotNone(updated)
                self.assertEqual(updated["status"], "completed")
                rows = await store.list()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["progress"], 100)

            asyncio.run(scenario())


class MemoryServiceTests(unittest.TestCase):
    def test_memory_whitelist_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SOUL.md.template").write_text("hello", encoding="utf-8")
            service = MemoryService(root)
            doc = service.read("SOUL")
            self.assertEqual(doc["content"], "hello")
            saved = service.write("SOUL", "updated")
            self.assertEqual(saved["content"], "updated")
            with self.assertRaises(KeyError):
                service.read("../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
