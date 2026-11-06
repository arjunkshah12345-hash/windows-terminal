#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python=${PYTHON:-python3}
temporary=$(mktemp -d "${TMPDIR:-/tmp}/windows-terminal-release.XXXXXX")
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

"$python" -m pip wheel --no-deps --wheel-dir "$temporary/wheels" "$root"
"$python" -m venv "$temporary/venv"
"$temporary/venv/bin/python" -m pip install "$temporary"/wheels/windows_terminal_vm-*.whl
"$temporary/venv/bin/python" -m pip check

cd "$temporary"
"$temporary/venv/bin/python" -I - <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
from windows_terminal.checkpoints import Checkpoints

command = [sys.executable, "-I", "-m", "windows_terminal"]
version = subprocess.run([*command, "--version"], capture_output=True, text=True, check=True, timeout=10)
environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
entrypoint = subprocess.run([str(Path(sys.executable).with_name("windows-terminal")), "--version"], env=environment, capture_output=True, text=True, check=True, timeout=10)
assert entrypoint.stdout == version.stdout
doctor = subprocess.run([*command, "doctor"], capture_output=True, text=True, check=True, timeout=10)
assert "host" in json.loads(doctor.stdout)
checkpoint = subprocess.run([*command, "checkpoint", "restore", "--help"], capture_output=True, text=True, check=True, timeout=10)
assert "--as NEW_VM" in checkpoint.stdout
requests = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]
response = subprocess.run([*command, "mcp"], input="".join(json.dumps(request) + "\n" for request in requests), capture_output=True, text=True, check=True, timeout=10)
replies = [json.loads(line) for line in response.stdout.splitlines()]
assert len(replies) == 2
assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
assert len(replies[1]["result"]["tools"]) == 9
print(f"Clean-install checks passed: version {version.stdout.strip()}, doctor, checkpoints, MCP initialization and nine tools.")
PY

mkdir -p "$root/dist"
cp "$temporary"/wheels/windows_terminal_vm-*.whl "$root/dist/"
"$temporary/venv/bin/python" -I - "$root/dist" <<'PY'
import hashlib
from pathlib import Path
import sys

directory = Path(sys.argv[1])
for wheel in sorted(directory.glob("windows_terminal_vm-*.whl")):
    checksum = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel.with_suffix(wheel.suffix + ".sha256").write_text(f"{checksum}  {wheel.name}\n")
    print(f"Verified release: {wheel}")
PY
