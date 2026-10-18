"""JSON-RPC/ACP transport for AionUi, with persistent sessions and cancellation."""

from __future__ import annotations

import contextlib
import json
import re
import sys
import threading
import uuid
from pathlib import Path

from .executor import Cancelled, CodexExecutor
from .tasks import run_request


def request_mode(message: str, default: str) -> tuple[bool, str]:
    """Honor a user command even when AionUi prepends its assistant instructions."""
    commands = list(re.finditer(r"(?m)^/(fast|deep)[ \t]+", message))
    if not commands:
        return default == "deep", message
    command = commands[-1]
    return command[1] == "deep", message[: command.start()] + message[command.end() :]


class ACPServer:
    def __init__(self, state: Path, model="default", *, input_stream=None, output_stream=None):
        self.state = state
        self.model = model
        self.input = input_stream or sys.stdin
        self.output = output_stream or sys.stdout
        self.write_lock = threading.Lock()
        self.sessions = {}
        self.workers = []
        (state / "sessions").mkdir(parents=True, exist_ok=True)

    def send(self, value):
        with self.write_lock:
            self.output.write(json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False) + "\n")
            self.output.flush()

    def update(self, sid, update):
        self.send({"method": "session/update", "params": {"sessionId": sid, "update": update}})

    def result(self, request_id, value):
        self.send({"id": request_id, "result": value})

    def error(self, request_id, code, message):
        self.send({"id": request_id, "error": {"code": code, "message": str(message)}})

    def session_path(self, sid):
        # A session identifier is a UUID, never a caller-controlled path.
        return self.state / "sessions" / (str(uuid.UUID(sid)) + ".json")

    def save(self, sid):
        session = self.sessions[sid]
        data = {k: v for k, v in session.items() if k not in {"cancel", "busy"}}
        path = self.session_path(sid)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        temp.replace(path)

    def session_response(self, sid):
        return {
            "sessionId": sid,
            "modes": {
                "currentModeId": self.sessions[sid]["mode"],
                "availableModes": [
                    {
                        "id": "deep",
                        "name": "Harness²",
                        "description": "Propose–Probe–Compose, k=1; сохраняет уроки",
                    },
                    {
                        "id": "fast",
                        "name": "Быстрый",
                        "description": "Одно выполнение Codex, без рекурсии",
                    },
                ],
            },
            "models": {
                "currentModelId": self.model,
                "availableModels": [{"modelId": self.model, "name": self.model}],
            },
        }

    def prompt(self, request_id, params):
        sid = params["sessionId"]
        session = self.sessions[sid]
        progress_id = None
        succeeded = False

        def progress(message):
            nonlocal progress_id
            if progress_id:
                self.update(
                    sid,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": progress_id,
                        "status": "completed",
                    },
                )
            progress_id = uuid.uuid4().hex
            self.update(
                sid,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": progress_id,
                    "title": message,
                    "kind": "other",
                    "status": "in_progress",
                },
            )

        try:
            blocks = params.get("prompt", [])
            parts = []
            for block in blocks:
                if block.get("type") == "text":
                    parts.append(block["text"])
                elif block.get("type") == "resource":
                    resource = block.get("resource", {})
                    if "text" in resource:
                        parts.append(f"{resource.get('uri', 'attachment')}:\n{resource['text']}")
                elif block.get("type") == "resource_link":
                    parts.append("Attached file: " + block.get("uri", ""))
            message = "\n".join(parts).strip()
            if not message:
                raise ValueError("Send a text task or a text attachment")
            deep, message = request_mode(message, session["mode"])
            previous = session["history"][-12:]
            conversation = "\n\n".join(f"{p['role']}: {p['text']}" for p in previous)
            prompt = f"Previous conversation:\n{conversation}\n\n" if conversation else ""
            prompt += "Current request:\n" + message
            executor = CodexExecutor(
                self.model,
                state=self.state,
                cancel=session["cancel"],
                progress=progress,
            )
            # Upstream progress prints must never corrupt the JSON-RPC stdout channel.
            with contextlib.redirect_stdout(sys.stderr):
                answer, report = run_request(
                    prompt,
                    Path(session["cwd"]),
                    self.state,
                    executor,
                    deep=deep,
                )
            session["history"].extend(
                [
                    {"role": "user", "text": message},
                    {"role": "assistant", "text": answer},
                ]
            )
            session["last_result"] = report
            self.save(sid)
            result_dir = Path(report["result"])
            files = [p for p in sorted(result_dir.rglob("*")) if p.is_file()]
            links = "\n".join(
                f"- [{p.relative_to(result_dir).as_posix()}]({p.as_uri()})" for p in files
            )
            text = answer + "\n\nФайлы результата:\n\n" + links
            self.update(
                sid,
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
            )
            succeeded = True
            self.result(request_id, {"stopReason": "end_turn"})
        except Cancelled:
            self.result(request_id, {"stopReason": "cancelled"})
        except Exception as exc:
            self.error(request_id, -32603, str(exc))
        finally:
            if progress_id:
                self.update(
                    sid,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": progress_id,
                        "status": "completed" if succeeded else "failed",
                    },
                )
            session["busy"] = False

    def handle(self, message):
        method = message.get("method")
        params = message.get("params", {})
        rid = message.get("id")
        try:
            if method == "initialize":
                self.result(
                    rid,
                    {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": True,
                            "promptCapabilities": {"embeddedContext": True},
                        },
                        "agentInfo": {
                            "name": "harness2",
                            "title": "Harness² + Codex",
                            "version": "0.1.0",
                        },
                        "authMethods": [],
                    },
                )
            elif method in {"session/new", "session/load"}:
                sid = params.get("sessionId") if method == "session/load" else str(uuid.uuid4())
                if method == "session/load":
                    data = json.loads(self.session_path(sid).read_text())
                else:
                    cwd = Path(params.get("cwd", str(Path.cwd()))).resolve()
                    if not cwd.is_dir():
                        raise ValueError("Workspace directory does not exist")
                    data = {"cwd": str(cwd), "mode": "deep", "history": []}
                self.sessions[sid] = {**data, "cancel": threading.Event(), "busy": False}
                self.save(sid)
                if method == "session/load":
                    for entry in data["history"]:
                        role = "user" if entry["role"] == "user" else "agent"
                        self.update(
                            sid,
                            {
                                "sessionUpdate": role + "_message_chunk",
                                "content": {"type": "text", "text": entry["text"]},
                            },
                        )
                self.result(rid, self.session_response(sid))
            elif method == "session/prompt":
                session = self.sessions[params["sessionId"]]
                if session["busy"]:
                    raise ValueError("This conversation is already running")
                session["busy"] = True
                session["cancel"].clear()
                worker = threading.Thread(target=self.prompt, args=(rid, params))
                self.workers.append(worker)
                worker.start()
            elif method == "session/cancel":
                self.sessions[params["sessionId"]]["cancel"].set()
                if rid is not None:
                    self.result(rid, {})
            elif method == "session/set_mode":
                mode = params["modeId"]
                if mode not in {"deep", "fast"}:
                    raise ValueError("Unknown mode")
                sid = params["sessionId"]
                self.sessions[sid]["mode"] = mode
                self.save(sid)
                self.result(rid, {})
            elif method == "session/set_model":
                if params["modelId"] != self.model:
                    raise ValueError("Set HARNESS2_LOCAL_MODEL in the service to change the model")
                self.result(rid, {})
            elif rid is not None:
                self.error(rid, -32601, f"Unsupported ACP method: {method}")
        except (KeyError, ValueError, OSError) as exc:
            if rid is not None:
                self.error(rid, -32602, str(exc))

    def serve(self):
        try:
            for line in self.input:
                try:
                    message = json.loads(line)
                    self.handle(message)
                except json.JSONDecodeError:
                    self.error(None, -32700, "Invalid JSON")
        finally:
            for session in self.sessions.values():
                session["cancel"].set()
            for worker in self.workers:
                worker.join()
