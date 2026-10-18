"""Bounded Codex executions with cancellation and process-wide serialization."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable


class Cancelled(RuntimeError):
    """The user stopped the current task."""


class CodexExecutor:
    def __init__(
        self,
        model: str = "default",
        *,
        effort: str = "medium",
        state: Path | None = None,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        self.model = model
        self.effort = effort
        self.state = state or Path.home() / ".local/share/aion-h2"
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda _message: None)
        self.state.mkdir(parents=True, exist_ok=True)

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Task cancelled")

    def run(self, prompt: str, cwd: Path, audit: Path, *, timeout: int = 600) -> str:
        """Keep authentication, isolate configuration, and write only in the staged workspace."""
        self.check_cancelled()
        audit.mkdir(parents=True, exist_ok=True)
        answer = audit / "answer.md"
        cmd = [
            os.environ.get("HARNESS2_LOCAL_CODEX", "codex"),
            "exec",
            "-",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--json",
            "--color",
            "never",
            "--cd",
            str(cwd),
            "--output-last-message",
            str(answer),
            "-c",
            'approval_policy="never"',
            "-c",
            f'model_reasoning_effort="{self.effort}"',
            "-c",
            "features.multi_agent=false",
        ]
        if self.model != "default":
            cmd.extend(["--model", self.model])
        # flock also serializes executions from different AionUi conversations.
        with (self.state / "executor.lock").open("w") as lock:
            while True:
                self.check_cancelled()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.cancel.wait(0.2)
            self.progress(f"Codex: {audit.parent.name}/{audit.name}")
            with (
                (audit / "events.jsonl").open("w") as events,
                (audit / "stderr.log").open("w") as errors,
            ):
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=events,
                    stderr=errors,
                    cwd=cwd,
                    text=True,
                    start_new_session=True,
                )
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                    deadline = time.monotonic() + timeout
                    while proc.poll() is None:
                        self.check_cancelled()
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"Codex exceeded {timeout}s")
                        self.cancel.wait(0.2)
                except BaseException:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=3)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
                    raise
            if proc.returncode:
                message = (audit / "stderr.log").read_text()[-2000:]
                raise RuntimeError(f"Codex exited {proc.returncode}: {message}")
        result = answer.read_text() if answer.is_file() else ""
        if not result.strip():
            raise RuntimeError("Codex returned no answer; see " + str(audit))
        # Preserve usage without pretending that subscription usage has a measured USD price.
        usage = []
        for line in (audit / "events.jsonl").read_text().splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "turn.completed":
                    usage.append(event.get("usage", {}))
            except json.JSONDecodeError:
                continue
        (audit / "usage.json").write_text(json.dumps({"turns": usage, "usd": None}, indent=2))
        return result
