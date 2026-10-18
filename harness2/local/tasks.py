"""Adapt ordinary local requests to the upstream Propose–Probe–Compose workflow."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from ..core import library, schedule, tree
from ..core.proposer import Proposer
from ..core.spec import BenchPrompts, BenchSpec, ChannelSpec
from ..substrate.codex import CodexSubstrate
from .executor import CodexExecutor

BASE = """You are a practical assistant. Complete the user's task in its original language.
Use the files provided. Put deliverables in output/ and give a self-contained final answer.
Check calculations and claims. State uncertainty and distinguish completed work from proposals.
These are trial executions: do not send messages, publish, deploy, purchase, or change external
systems. Work only inside this staged workspace. Do not delegate to other agents.
"""
SKIP = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    ".next",
    "target",
    "dist",
    "build",
    "runs",
    ".data",
    "logs",
    "AionUi",
    "harness2",
    "state",
    "h2-results",
}


def snapshot(source: Path, target: Path, *, max_bytes: int = 64 * 1024 * 1024) -> int:
    """Copy regular workspace files, excluding secrets, links and dependency trees."""
    total = 0
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for parent, dirs, files in os.walk(source, followlinks=False):
        parent = Path(parent)
        dirs[:] = [d for d in dirs if d not in SKIP and not (parent / d).is_symlink()]
        for name in files:
            p = parent / name
            if p.is_symlink() or not p.is_file():
                continue
            if name.startswith(".env") or name in {"auth.json", "credentials.json"}:
                continue
            if p.suffix.lower() in {".pem", ".key", ".p12", ".sqlite", ".db"}:
                continue
            size = p.stat().st_size
            if total + size > max_bytes or count >= 1500:
                raise ValueError(
                    "Workspace snapshot exceeds 64 MiB / 1500 files; select a smaller folder"
                )
            dest = target / p.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            total += size
            count += 1
    return count


class LocalProposer(Proposer):
    def __init__(self, executor: CodexExecutor, audit_dir: Path):
        self.executor = executor
        super().__init__(
            ChannelSpec("codex_cli", executor.model, effort=executor.effort, timeout=600),
            audit_dir=audit_dir,
            attempts=1,
        )

    def _codex_cli(self, cwd, mission, system, wall):
        result = self.executor.run(f"{system}\n\n{mission}", cwd, cwd / ".audit", timeout=wall)
        return result, 0.0, ""


class LocalTaskAdapter:
    def __init__(self, root: Path, prompt: str, executor: CodexExecutor):
        self.root = root
        self.prompt = prompt
        self.executor = executor
        self.substrate = CodexSubstrate()
        channel = ChannelSpec("codex_cli", executor.model, effort=executor.effort, timeout=600)
        self.spec = BenchSpec(
            name="local",
            runs_root=root / "runs",
            install_mode="codex_root",
            prompts=BenchPrompts(
                work="complete the user's practical request using the supplied local files",
                domain_word="work area",
                domain_guidance="Reply in the user's language.",
            ),
            channels={"proposer": channel, "selector": channel},
        )
        tree.write_tree(self.substrate.surface(), {"AGENTS.md": BASE}, self.base_harness())

    def tasks(self, split="all"):
        return ["request"]

    def domain_of(self, task_id):
        return "general"

    def task_prompt(self, task_id):
        return self.prompt

    def task_files(self, task_id):
        return self.root / "source"

    def base_harness(self):
        return self.root / "base"

    def validate_tree(self, candidate):
        if not candidate.get("AGENTS.md", "").strip():
            raise ValueError("Keep a nonempty AGENTS.md")

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        self.executor.check_cancelled()
        run_dir = self.root / "executions" / run_id
        workspace = run_dir / "workspace"
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.task_files(task_id), workspace, dirs_exist_ok=True)
        shutil.copytree(harness_dir or self.base_harness(), workspace, dirs_exist_ok=True)
        fixed = (
            "This is an isolated trial. Do not send messages or change external systems. "
            "Keep files inside this workspace. Put finished files in output/.\n\n"
        )
        answer = self.executor.run(
            fixed + self.prompt, workspace, run_dir / "audit", timeout=timeout or 600
        )
        (run_dir / "answer.md").write_text(answer)
        return run_dir

    def delivered(self, run_dir):
        return (Path(run_dir) / "answer.md").is_file()

    def trajectory(self, run_dir, *, obs_trunc):
        lines = []
        for line in (Path(run_dir) / "audit/events.jsonl").read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item", {})
            if item.get("type") == "command_execution":
                lines.append("[ACTION] " + str(item.get("command", "")))
                lines.append("[OBSERVATION] " + str(item.get("aggregated_output", ""))[:obs_trunc])
            elif item.get("type") == "agent_message":
                lines.append("[OBSERVATION] " + str(item.get("text", ""))[:obs_trunc])
        return "\n".join(lines) or "[OBSERVATION] " + self.deliverable_text(run_dir)

    def deliverable_text(self, run_dir):
        return (Path(run_dir) / "answer.md").read_text()

    def deliverable_files(self, run_dir):
        result = {"answer.md": self.deliverable_text(run_dir)}
        for p in sorted((Path(run_dir) / "workspace/output").rglob("*")):
            if p.is_file() and not p.is_symlink() and p.stat().st_size < 200_000:
                try:
                    result[p.relative_to(Path(run_dir) / "workspace").as_posix()] = p.read_text()
                except UnicodeDecodeError:
                    pass
        return result

    def score(self, run_dir, task_id):
        return None


def run_request(prompt: str, cwd: Path, state: Path, executor: CodexExecutor, *, deep=True):
    """Serialize complete jobs, including updates to the shared experience library."""
    state.mkdir(parents=True, exist_ok=True)
    with (state / "task.lock").open("w") as lock:
        waiting = False
        while True:
            executor.check_cancelled()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waiting:
                    executor.progress("Ожидание завершения другой задачи Harness²")
                    waiting = True
                executor.cancel.wait(0.2)
        return _run_request(prompt, cwd, state, executor, deep=deep)


def _run_request(prompt: str, cwd: Path, state: Path, executor: CodexExecutor, *, deep=True):
    task_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    root = state / "tasks" / task_id
    root.mkdir(parents=True)
    (root / "request.txt").write_text(prompt)
    snapshot(cwd, root / "source")
    adapter = LocalTaskAdapter(root, prompt, executor)
    if deep:
        executor.progress("Harness²: базовое решение → предложения → пробы → итоговая обвязка")
        prop = LocalProposer(executor, root / "improver")
        lib = library.Library(state / "library", "general", adapter.substrate.surface())
        result = schedule.run_task(
            "request",
            1,
            adapter=adapter,
            prop=prop,
            selector_prop=prop,
            runs_root=root / "runs",
            modes=("seq",),
            library=lib,
        )
        executor.check_cancelled()
        chosen = result.selections.get("seq", {}).get("chosen_run_dir")
        if result.status != "ok" or not chosen:
            raise RuntimeError(f"Harness² did not finish: {result.errors}; logs: {root}")
        run_dir = Path(chosen)
    else:
        executor.progress("Быстрый режим: одно выполнение, без рекурсии Harness²")
        run_dir = adapter.execute("request", None, "fast")
    output = adapter.deliverable_text(run_dir)
    # Publish only the selected trial's deliverables, never intermediate variants.
    final_dir = root / "result"
    final_dir.mkdir()
    (final_dir / "answer.md").write_text(output)
    artifacts = run_dir / "workspace/output"
    if artifacts.is_dir():
        # Do not publish links created by a trial that point outside its output directory.
        snapshot(artifacts, final_dir / "output")
    visible_result = cwd / "h2-results" / task_id
    shutil.copytree(final_dir, visible_result)
    report = {
        "task": task_id,
        "deep": deep,
        "result": str(visible_result),
        "audit_result": str(final_dir),
        "run": str(run_dir),
    }
    (root / "result.json").write_text(json.dumps(report, indent=2))
    return output, report
