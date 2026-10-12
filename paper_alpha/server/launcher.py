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
import tempfile
import threading
import time

from .provider_contract import canonical_json
from .provider_runtime import (CONFIG_ENV, FIXTURE_ENV, REAL_ENV, CONFIG_DIGEST_ENV,
                               load_provider_registry)
from .native_correction_start import CATALOG_ENV

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start local Paper Alpha workbench; no AI required")
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("PAPER_ALPHA_HOME", ROOT / "var/workbench")))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--allow-fixture-provider", action="store_true")
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--native-correction-catalog", type=Path,
                        help="Host-managed Native start catalog; HTTP cannot choose a path")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be in 1024..65535")
    if not (ROOT / "frontend/dist/index.html").is_file():
        parser.error("Build the frontend first: cd frontend && npm ci && npm run build")
    try:
        registry = load_provider_registry(args.provider_config, allow_fixture=args.allow_fixture_provider,
                                          allow_real=args.allow_real_provider)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
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
    for key in (CONFIG_ENV, FIXTURE_ENV, REAL_ENV, CONFIG_DIGEST_ENV):
        env.pop(key, None)
    env.pop(CATALOG_ENV, None)
    if args.native_correction_catalog is not None:
        if not args.native_correction_catalog.is_absolute():
            parser.error("--native-correction-catalog must be an absolute host path")
        env[CATALOG_ENV] = str(args.native_correction_catalog)
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
    config_snapshot = tempfile.TemporaryDirectory(prefix="paper-alpha-provider-host-")
    try:
        if registry.configs or registry.allow_fixture or registry.allow_real:
            # Both services read the same startup snapshot, never a live edited
            # user configuration. This file contains no authentication data.
            snapshot_path = Path(config_snapshot.name) / "providers.json"
            snapshot = registry.snapshot()
            with snapshot_path.open("x", encoding="utf-8") as stream:
                os.chmod(snapshot_path, 0o600)
                stream.write(canonical_json(snapshot) + "\n")
            env[CONFIG_ENV] = str(snapshot_path)
            env[CONFIG_DIGEST_ENV] = registry.startup_digest()
            env[FIXTURE_ENV] = "1" if registry.allow_fixture else "0"
            env[REAL_ENV] = "1" if registry.allow_real else "0"
        if registry.dispatch_config_digests():
            commands.insert(1, [sys.executable, "-m", "paper_alpha.server.proposal_worker",
                                "--home", str(args.home.resolve())])
        for command in commands:
            children.append(subprocess.Popen(command, cwd=ROOT, env=env))
        print(f"\nPaper Alpha: http://127.0.0.1:{args.port}\nAPI docs: http://127.0.0.1:{args.port}/docs\n"
              f"State: {args.home.resolve()}\nCtrl+C stops all local services; queued jobs remain saved.\n", flush=True)
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
        config_snapshot.cleanup()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
