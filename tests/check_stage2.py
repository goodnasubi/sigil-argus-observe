#!/usr/bin/env python3
"""Verify storage durability and liveness against the evaluator and native code."""

from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "argus_health.sigil",
    "argus_health_test.sigil",
    "argus_collect.sigil",
    "argus_collect_test.sigil",
)


def check(command, directory, **environment):
    completed = subprocess.run(
        command,
        cwd=directory,
        env={**os.environ, **environment},
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=300,
    )
    if completed.stdout.strip() != "0":
        raise AssertionError(f"{command}: {completed.stdout}")


def loopback(command, directory):
    with ExitStack() as resources:
        release = threading.Event()
        errors = []
        ports = {}
        threads = []
        for name, status in (
            ("TCP", None),
            ("HTTP", b"HTTP/1.1 204 No Content\r\n"),
            ("ERROR", b"HTTP/1.1 503 Unavailable\r\n"),
            ("SLOW", b""),
        ):
            listener = resources.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(10)
            port = listener.getsockname()[1]
            ports[f"ARGUS_{name}_PORT"] = str(port)

            def respond(listener=listener, status=status, port=port):
                try:
                    client, _ = listener.accept()
                    with client:
                        if status is None:
                            return
                        client.settimeout(3)
                        request = b""
                        while not request.endswith(b"\r\n\r\n") and len(request) < 1024:
                            block = client.recv(1024)
                            if not block:
                                break
                            request += block
                        expected = (
                            f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode()
                        if request != expected:
                            raise AssertionError(f"unexpected HTTP probe {request!r}")
                        if status:
                            client.sendall(status + b"Content-Length: 0\r\n\r\n")
                        elif not release.wait(10):
                            raise AssertionError("the stalled HTTP probe did not time out")
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=respond, daemon=True)
            thread.start()
            threads.append(thread)
        try:
            check(command, directory, **ports)
        finally:
            release.set()
        for thread in threads:
            thread.join(timeout=11)
            if thread.is_alive():
                raise AssertionError("loopback server did not stop")
        if errors:
            raise AssertionError(errors)


def main():
    compiler = shutil.which(os.environ.get("SIGIL", "sigil"))
    if compiler is None:
        raise SystemExit("Put sigil on PATH or set SIGIL.")
    db_checkout = os.environ.get("SIGIL_DB")
    if not db_checkout:
        raise SystemExit("Set SIGIL_DB to the pinned sigil-db checkout.")
    subprocess.run([compiler, "fmt", "--check", *SOURCES], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory() as directory:
        stage = Path(directory)
        shutil.copytree(Path(db_checkout) / "db", stage / "db")
        for source in (*SOURCES, "argus_observe.sigil", "Sigil.toml"):
            shutil.copy2(ROOT / source, stage)
        for source in ("argus_health_test.sigil", "argus_collect_test.sigil"):
            subprocess.run([compiler, "check", "--root", ".", source], cwd=stage, check=True)
        for source, entry in (
            ("argus_health_test.sigil", "main"),
            ("argus_health_test.sigil", "loopback"),
            ("argus_collect_test.sigil", "main"),
        ):
            native = stage / f"{Path(source).stem}-{entry}"
            subprocess.run(
                [compiler, "build", "--root", ".", source, entry, "-o", native],
                cwd=stage,
                check=True,
            )
            evaluator = [compiler, "run", "--root", ".", source, entry]
            for mode, command in (("eval", evaluator), ("native", [native])):
                if entry == "loopback":
                    loopback(command, stage)
                elif source == "argus_collect_test.sigil":
                    database = str(stage / f"{mode}-database")
                    for phase in ("write", "verify"):
                        check(command, stage, ARGUS_TEST_DB=database, ARGUS_TEST_MODE=phase)
                else:
                    check(command, stage)
    print("Stage 2 tests passed")


if __name__ == "__main__":
    main()
