"""Local integration contracts: isolation, complete recursion, transport and cancellation."""

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.local.acp import ACPServer, request_mode
from harness2.local.executor import Cancelled, CodexExecutor
from harness2.local.tasks import run_request, snapshot


class FakeExecutor(CodexExecutor):
    def run(self, prompt, cwd, audit, *, timeout=600):
        self.check_cancelled()
        if (cwd / "context/harness").is_dir():
            agent_file = cwd / "context/harness/AGENTS.md"
            agent_file.write_text(agent_file.read_text() + "\nCheck all signed amounts.\n")
            return "<notes>Include signed amounts and verify the total.</notes>"
        if "experience" in prompt.lower() and (cwd / "context").is_dir():
            return "NO_ENTRY"
        audit.mkdir(parents=True, exist_ok=True)
        (audit / "events.jsonl").write_text(
            '{"type":"item.completed","item":{"type":"agent_message","text":"Total: 85.15"}}\n'
        )
        (cwd / "output").mkdir(exist_ok=True)
        (cwd / "output/total.txt").write_text("85.15\n")
        return "Total: 85.15"


class LocalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_snapshot_omits_secrets_links_and_dependencies(self):
        source = self.root / "workspace"
        (source / "node_modules").mkdir(parents=True)
        (source / "data.csv").write_text("amount\n1\n")
        (source / ".env").write_text("PRIVATE=secret")
        (source / "node_modules/dependency.js").write_text("ignored")
        (source / "outside").symlink_to("/etc/passwd")
        copied = self.root / "copy"
        self.assertEqual(snapshot(source, copied), 1)
        self.assertEqual([p.name for p in copied.iterdir()], ["data.csv"])

    def test_snapshot_size_limit_is_explicit(self):
        source = self.root / "workspace"
        source.mkdir()
        (source / "large").write_bytes(b"12345")
        with self.assertRaises(ValueError):
            snapshot(source, self.root / "copy", max_bytes=4)

    def test_actual_upstream_recursion_exports_only_selected_output(self):
        source = self.root / "workspace"
        source.mkdir()
        original = "amount\n100.25\n-20.10\n5.00\n"
        (source / "ledger.csv").write_text(original)
        executor = FakeExecutor("test", state=self.root / "state")
        answer, report = run_request("Sum every signed amount", source, executor.state, executor)
        self.assertEqual(answer, "Total: 85.15")
        self.assertEqual((source / "ledger.csv").read_text(), original)
        self.assertEqual((Path(report["result"]) / "output/total.txt").read_text(), "85.15\n")
        summaries = list(executor.state.glob("tasks/*/runs/*/request/summary.json"))
        data = json.loads(summaries[0].read_text())
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["selections"]["seq"]["chosen_label"], "c1")
        self.assertEqual(
            len(list(executor.state.glob("tasks/*/executions/*/request/*/answer.md"))), 4
        )

    def test_acp_persists_and_restores_mode(self):
        output = io.StringIO()
        server = ACPServer(self.root, output_stream=output)
        server.handle({"id": 1, "method": "session/new", "params": {"cwd": str(self.root)}})
        sid = json.loads(output.getvalue().splitlines()[-1])["result"]["sessionId"]
        server.handle(
            {"id": 2, "method": "session/set_mode", "params": {"sessionId": sid, "modeId": "fast"}}
        )
        other = ACPServer(self.root, output_stream=output)
        other.handle({"id": 3, "method": "session/load", "params": {"sessionId": sid}})
        self.assertEqual(other.sessions[sid]["mode"], "fast")

    def test_fast_command_survives_aionui_instruction_prefix(self):
        original = "[Assistant Rules]\nUse tools\n[/Assistant Rules]\n\n/fast Say hello"
        deep, message = request_mode(original, "deep")
        self.assertFalse(deep)
        self.assertTrue(message.endswith("Say hello"))
        self.assertNotIn("/fast", message)

    def test_cancel_kills_running_executor_process(self):
        fake = self.root / "fake-codex"
        fake.write_text(
            "#!/usr/bin/env python3\nimport pathlib,time\npathlib.Path('started').write_text('yes')\ntime.sleep(30)\npathlib.Path('finished').write_text('bad')\n"
        )
        fake.chmod(0o755)
        cancel = threading.Event()
        executor = CodexExecutor(state=self.root / "state", cancel=cancel)
        errors = []

        def run():
            try:
                executor.run("test", self.root, self.root / "audit")
            except Exception as exc:
                errors.append(exc)

        with patch.dict("os.environ", {"HARNESS2_LOCAL_CODEX": str(fake)}):
            worker = threading.Thread(target=run)
            worker.start()
            for _ in range(100):
                if (self.root / "started").exists():
                    break
                threading.Event().wait(0.02)
            self.assertTrue((self.root / "started").is_file())
            cancel.set()
            worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], Cancelled)
        self.assertFalse((self.root / "finished").exists())


if __name__ == "__main__":
    unittest.main()
