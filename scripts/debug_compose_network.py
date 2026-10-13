#!/usr/bin/env python3
"""
Docker / network diagnostics for the Fidelis compose stack.

Run from the repo root while compose is up:

    docker compose up -d --wait
    python scripts/debug_compose_network.py

Checks host → published ports, prints internal service URLs from compose,
and optionally runs in-container probes (webhook → orchestrator on fidelis_net).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent

HOST_CHECKS = [
    ("gateway /healthz", "http://localhost:8000/healthz"),
    ("orchestrator /health", "http://localhost:8001/health"),
    ("webhook /healthz", "http://localhost:8002/healthz"),
    ("reviewer /healthz", "http://localhost:8003/healthz"),
]

INTERNAL_URLS = """
Internal DNS (container → container on network fidelis_net):
  gateway      listens :8000  (not used as upstream; external entry only)
  webhook      http://webhook:8000       ORCHESTRATOR_URL=http://orchestrator:8000
  orchestrator http://orchestrator:8000  DATABASE_URL=...@postgres:5432/...
  postgres     postgres:5432

Host machine → published ports (what integration tests and smoke_test use):
  gateway      localhost:8000
  orchestrator localhost:8001
  webhook      localhost:8002
  reviewer     localhost:8003
  postgres     localhost:5432
"""


def _http_get(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        with request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(512).decode("utf-8", errors="replace")
            return resp.status == 200, f"HTTP {resp.status} {body[:200]}"
    except error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except error.URLError as exc:
        return False, str(exc.reason)
    except TimeoutError:
        return False, "timeout"


def _run(cmd: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "command timed out"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def main() -> int:
    print("Fidelis compose network debug")
    print("=" * 72)
    print(INTERNAL_URLS.strip())
    print("-" * 72)

    failures = 0
    print("Host port probes:")
    for label, url in HOST_CHECKS:
        ok, detail = _http_get(url)
        tag = "OK" if ok else "FAIL"
        print(f"  [{tag}] {label}: {detail}")
        if not ok:
            failures += 1

    compose = shutil.which("docker")
    if not compose:
        print("\n[SKIP] docker not on PATH — in-container probes not run")
        return 1 if failures else 0

    print("-" * 72)
    print("Compose service status:")
    code, out = _run(["docker", "compose", "ps", "--format", "json"])
    if code != 0:
        print(f"  [FAIL] docker compose ps: {out}")
        failures += 1
    else:
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                print(f"  {line}")
                continue
            name = row.get("Name") or row.get("Service") or "?"
            state = row.get("State") or row.get("Status") or "?"
            health = row.get("Health") or ""
            extra = f" ({health})" if health else ""
            print(f"  {name}: {state}{extra}")

    print("-" * 72)
    print("In-container: webhook → orchestrator (internal HTTP):")
    probe = (
        "import os, urllib.request; "
        "url=os.environ.get('ORCHESTRATOR_URL','http://orchestrator:8000')+'/health'; "
        "r=urllib.request.urlopen(url, timeout=5); "
        "print(r.status, r.read()[:80])"
    )
    code, out = _run(
        ["docker", "compose", "exec", "-T", "webhook", "python", "-c", probe]
    )
    if code != 0:
        print(f"  [FAIL] {out or 'exec failed — is the webhook service running?'}")
        failures += 1
    else:
        print(f"  [OK] webhook container: {out}")

    print("-" * 72)
    print("Webhook env (ORCHESTRATOR_URL must be http://orchestrator:8000 in compose):")
    code, out = _run(["docker", "compose", "exec", "-T", "webhook", "printenv"])
    if code == 0:
        for line in sorted(out.splitlines()):
            if line.startswith(("ORCHESTRATOR_URL=", "GITHUB_WEBHOOK_SECRET=", "ENV=")):
                key, _, val = line.partition("=")
                if key == "GITHUB_WEBHOOK_SECRET":
                    val = val[:4] + "…" if val else "(empty)"
                print(f"  {key}={val}")
    else:
        print(f"  [FAIL] could not read webhook env: {out}")
        failures += 1

    if failures:
        print("-" * 72)
        print(
            "Hints: `docker compose logs webhook orchestrator gateway` — "
            "502 on webhook usually means orchestrator unreachable on fidelis_net; "
            "401 means GITHUB_WEBHOOK_SECRET mismatch vs your signed request."
        )
        print(f"RESULT: {failures} check(s) failed")
        return 1

    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
