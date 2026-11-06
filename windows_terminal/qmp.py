import fcntl
import json
import socket
import time
from pathlib import Path


class QMPError(RuntimeError):
    pass


class QMPClient:
    def __init__(self, path: str | Path, timeout: float = 5):
        self.path = Path(path)
        self.timeout = timeout
        self.socket = None
        self.stream = None
        self.lock = None
        self.sequence = 0

    def __enter__(self):
        try:
            self.lock = open(str(self.path) + ".lock", "a")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise QMPError("VM control is busy; retry shortly.")
                    time.sleep(0.01)
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.settimeout(self.timeout)
            self.socket.connect(str(self.path))
            self.stream = self.socket.makefile("rb")
            if "QMP" not in self._read():
                raise QMPError("The socket is not a QEMU QMP monitor.")
            self.execute("qmp_capabilities")
            return self
        except (OSError, ValueError, QMPError) as error:
            self.close()
            if isinstance(error, QMPError):
                raise
            raise QMPError(f"Cannot connect to VM at {self.path}: {error}") from error

    def _read(self):
        limit = 4 * 1024 * 1024
        line = self.stream.readline(limit + 1)
        if not line:
            raise QMPError("QEMU closed the control connection.")
        if len(line) > limit or not line.endswith(b"\n"):
            raise QMPError("Oversized or incomplete QMP response.")
        try:
            reply = json.loads(line)
            if not isinstance(reply, dict):
                raise QMPError("QMP response must be an object.")
            return reply
        except (ValueError, UnicodeError) as error:
            raise QMPError("Invalid QMP response.") from error

    def execute(self, command: str, arguments: dict | None = None):
        self.sequence += 1
        request = {"execute": command, "id": self.sequence}
        if arguments is not None:
            request["arguments"] = arguments
        try:
            self.socket.sendall(json.dumps(request).encode() + b"\n")
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                self.socket.settimeout(max(0.01, deadline - time.monotonic()))
                reply = self._read()
                if reply.get("id") != self.sequence:
                    continue
                if "error" in reply:
                    raise QMPError(reply["error"].get("desc", str(reply["error"])))
                if "return" not in reply:
                    raise QMPError("QMP response has no result.")
                return reply["return"]
            raise QMPError(f"QMP command timed out: {command}")
        except OSError as error:
            raise QMPError(f"QMP command {command} failed: {error}") from error

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def __exit__(self, *_):
        self.close()
