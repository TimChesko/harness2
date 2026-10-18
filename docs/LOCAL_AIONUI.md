# Local AionUi + Harness²

This fork adds ordinary local tasks and an ACP agent on top of the original
Harness² recursion, selection and experience library. It does not require a
benchmark dataset, Docker, Vertex AI or a new model API key. Codex CLI uses its
existing login (`codex login status`). Actual model use consumes that account's
allowance; subscription usage is not assigned a fictitious dollar price.

## Install

Use Linux / WSL2, Python 3.12+, Node 22–24 and Bun. Keep sibling checkouts:

```text
aion-h2/
  AionUi/       # iOfficeAI/AionUi, pinned to a reviewed commit
  harness2/     # this fork
  .venv/
  .data/        # AionUi database and workspaces; private, never commit
  state/        # Harness² tasks, logs, sessions and experience; never commit
  workspace/    # optional user-selected project
```

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./harness2
cd AionUi
CI=true ELECTRON_SKIP_BINARY_DOWNLOAD=1 bun install --frozen-lockfile --ignore-scripts
node scripts/prepareAioncore.js
bun run package
NODE_ENV=production bun scripts/webui.ts --no-build --no-open --port 25808 --data-dir ../.data
```

Use the web address printed by AionUi. On a fresh install it prints local admin
credentials. The standard standalone launcher binds to loopback by default.
Do not add `--remote` unless network access is intended.

## Register the ACP agent

Create an executable launcher with your installation paths:

```bash
#!/usr/bin/env bash
set -euo pipefail
export HARNESS2_LOCAL_MODEL=gpt-6.1-sol  # or another model available to your Codex login
export HARNESS2_LOCAL_STATE_DIR=/absolute/path/aion-h2/state
exec /absolute/path/aion-h2/.venv/bin/harness2-local --acp
```

In AionUi, open Settings → Agents → add a custom agent. Name it `Harness² + Codex`
and set its command to the launcher's absolute path. Test the connection. Create
an assistant bound to that agent, then start a chat. The stock AionUi frontend
does not need to be rewritten.

The same registration is available through `POST /api/agents/custom`, followed
by `POST /api/agents/{id}/health-check` and `POST /api/assistants` with `agent_id`.

## Behavior

- **Harness²** mode uses the real upstream sequential Propose–Probe–Compose
  workflow with `k=1`: a baseline, two proposals, two probes and a composed final
  execution. An adoption reflection can add a supported lesson to `state/library`.
- **Быстрый** mode, or a message beginning `/fast `, runs Codex once without
  recursive improvement. `/deep ` opts a single message into Harness².
- Sessions and conversation context survive ACP process restarts through
  `session/load`. Stop cancels the active Codex process group, including children.
- Complete tasks are serialized across chats. Individual Codex processes also
  share a lock. This bounds CPU load and protects the shared experience library.
- Each trial works in a separate copy of the selected folder. Dependency trees,
  common secret files and symlinks are excluded. The input limit is 64 MiB and
  1,500 files; exceeding it produces an explicit error.
- Only the chosen result is copied into the original folder, under
  `h2-results/<task-id>/`. Original source files are not overwritten. The chat
  returns links to the result files. Trial scripts and patches remain in the
  audit tree; this adapter does not merge code changes into the original project.
- The fixed trial instructions prohibit messaging, publishing, purchases and
  other external effects. These instructions are a behavior constraint; they
  are not an external network sandbox. Codex uses `workspace-write` sandboxing,
  isolated config and no approvals, without bypassing the sandbox or hook trust.
- A successful execution does not prove the final answer correct or better than
  the baseline. Benchmark judging is not run for user requests. Inspect artifacts.
- This first adapter accepts text and textual context. It does not implement
  image/audio understanding, MCP forwarding or team delegation. Telegram is
  provided by AionUi and needs its own BotFather token and channel configuration.

CLI use:

```bash
harness2-local --model gpt-6.1-sol --cwd /path/to/project 'Your task'
harness2-local --fast --model gpt-6.1-sol --cwd /path/to/project 'Your task'
```

Verification:

```bash
python -m unittest discover -s tests -v
```

`tests/test_local.py` covers source isolation, limits, the real recursion driver
with a deterministic executor, exported artifacts, ACP session restoration and
cancellation of a running process.
