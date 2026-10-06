"""Run the built workbench and its worker on loopback with one command."""
from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start local Paper Alpha workbench; no AI required")
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("PAPER_ALPHA_HOME", ROOT / "var/workbench")))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be in 1024..65535")
    if not (ROOT / "frontend/dist/index.html").is_file():
        parser.error("Build the frontend first: cd frontend && npm ci && npm run build")
    try:
        # BSD/macOS can allow a reused loopback bind alongside a wildcard
        # listener. Refuse a live listener before considering TIME_WAIT reuse.
        # Send no application data; unknown/timeout results also fail closed.
        with socket.socket() as active_probe:
            active_probe.settimeout(.25)
            if active_probe.connect_ex(("127.0.0.1", args.port)) != errno.ECONNREFUSED:
                raise OSError("Port has an active listener or could not be checked")
        with socket.socket() as probe:
            # Match the API server: a recently closed connection in TIME_WAIT
            # must not look like a live listener. No SO_REUSEPORT is enabled.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", args.port))
    except OSError:
        parser.error(f"Port {args.port} is already occupied; select --port with a free port")
    env = {**os.environ, "PAPER_ALPHA_HOME": str(args.home.resolve()), "PYTHONDONTWRITEBYTECODE": "1"}
    commands = [
        [sys.executable, "-m", "paper_alpha.server.runner", "--home", str(args.home.resolve())],
        [sys.executable, "-m", "uvicorn", "paper_alpha.server.api:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(args.port)],
    ]
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    children = []
    code = 0
    try:
        for command in commands:
            children.append(subprocess.Popen(command, cwd=ROOT, env=env))
        print(f"\nPaper Alpha: http://127.0.0.1:{args.port}\nAPI docs: http://127.0.0.1:{args.port}/docs\n"
              f"State: {args.home.resolve()}\nCtrl+C stops API and worker; queued jobs remain saved.\n", flush=True)
        while not stopped.wait(.3):
            if any(child.poll() is not None for child in children):
                code = 1
                print("A service exited; stopping the workbench. Check the preceding error.", file=sys.stderr)
                break
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 8
        for child in children:
            try:
                child.wait(timeout=max(.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
