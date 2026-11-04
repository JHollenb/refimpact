"""Manage only this checkout's Ref Impact HTTP servers; never delete data."""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / ".venv/bin/refimpact"
LOG = ROOT / ".cache/server.log"


def instances() -> list[tuple[int, list[str]]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="], check=True, capture_output=True, text=True
    )
    found = []
    for line in result.stdout.splitlines():
        try:
            pid, command = line.strip().split(None, 1)
            args = shlex.split(command)
        except ValueError:
            continue
        # Match an executable token followed by the serve subcommand. Matching
        # the full absolute checkout path avoids other projects and shell text.
        if any(a == str(ENTRY) and args[i + 1 : i + 2] == ["serve"] for i, a in enumerate(args)):
            found.append((int(pid), args))
    return found


def alive(pid: int) -> bool:
    # An exited child can remain a zombie until its parent reaps it; ps-based
    # identity checks in clean handle that without trusting a stale PID file.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def address(host: str, port: int) -> str:
    if host == "0.0.0.0":
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("192.0.2.1", 1))  # Route lookup; sends no packets.
                host = sock.getsockname()[0]
        except OSError:
            host = "<this-computer-IP>"
    return f"http://{host}:{port}"


def serve(host: str, port: int, background: bool) -> int:
    if not ENTRY.exists():
        raise SystemExit("Dependencies are missing. Run make setup first.")
    current = instances()
    if current:
        print("Ref Impact is already running. Use make clean before changing its bind/port.")
        for pid, args in current:
            print(f"PID {pid}: {shlex.join(args)}")
        return 0
    args = [str(ENTRY), "serve", "--host", host, "--port", str(port)]
    print(f"Ref Impact: {address(host, port)}", flush=True)
    if not background:
        os.chdir(ROOT)
        os.execv(str(ENTRY), args)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("ab") as output:
        child = subprocess.Popen(
            args,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    # Check the actual TCP listener, not only whether the process spawned.
    target = "127.0.0.1" if host == "0.0.0.0" else host
    for _ in range(100):
        if child.poll() is not None:
            raise SystemExit(f"Server failed to start. Read {LOG}")
        try:
            with socket.create_connection((target, port), timeout=0.1):
                print(f"Started PID {child.pid}. Log: {LOG}")
                return 0
        except OSError:
            time.sleep(0.1)
    child.terminate()
    child.wait(timeout=5)
    raise SystemExit(f"Server did not become ready. Read {LOG}")


def clean() -> int:
    current = instances()
    for pid, _ in current:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Stopping Ref Impact PID {pid}")
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    pending = {pid for pid, _ in current}
    while pending and time.monotonic() < deadline:
        pending &= {pid for pid, _ in instances()}
        if pending:
            time.sleep(0.1)
    # Recheck identity before escalating, so a reused PID cannot target another app.
    pending &= {pid for pid, _ in instances()}
    for pid in pending:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
            print(f"Stopped unresponsive Ref Impact PID {pid}")
    print(
        "Ref Impact stopped. Cached data and reports preserved."
        if current
        else "No Ref Impact servers running."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "start", "status", "clean"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, choices=range(1, 65536), default=8787, metavar="PORT")
    args = parser.parse_args()
    if args.action in {"run", "start"}:
        return serve(args.host, args.port, args.action == "start")
    if args.action == "clean":
        return clean()
    rows = instances()
    for pid, command in rows:
        print(f"PID {pid}: {shlex.join(command)}")
    if not rows:
        print("No Ref Impact servers running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
