"""Local CLI and ACP entry point."""

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from .acp import ACPServer
from .executor import CodexExecutor
from .tasks import run_request


def main():
    parser = argparse.ArgumentParser(description="Harness² local tasks / AionUi ACP agent")
    parser.add_argument("--acp", action="store_true")
    parser.add_argument(
        "--fast", action="store_true", help="One execution, without recursive refinement"
    )
    parser.add_argument("--model", default=os.environ.get("HARNESS2_LOCAL_MODEL", "default"))
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(
            os.environ.get("HARNESS2_LOCAL_STATE_DIR", str(Path.home() / ".local/share/aion-h2"))
        ),
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("prompt", nargs="?")
    args = parser.parse_args()
    if args.acp:
        ACPServer(args.state_dir, args.model).serve()
    else:
        prompt = args.prompt or sys.stdin.read()
        if not prompt.strip():
            parser.error("A task is required")
        executor = CodexExecutor(
            args.model, state=args.state_dir, progress=lambda s: print(s, file=sys.stderr)
        )
        with contextlib.redirect_stdout(sys.stderr):
            answer, report = run_request(
                prompt, args.cwd, args.state_dir, executor, deep=not args.fast
            )
        print(answer)
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
