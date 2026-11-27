#!/usr/bin/env python3
"""Verify data retention rotation, rate limiting, and load boundary tests (ADR 0067 decision 7, stage 4).

Integration scenarios start the server and client within the same process on an ephemeral loopback port.
Tests are run against both the Sigil interpreter / evaluator and native compiled binaries.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "argus_observe.sigil",
    "argus_collect.sigil",
    "argus_health.sigil",
    "argus_query.sigil",
    "argus_receive.sigil",
    "argus_accept_test.sigil",
    "argus_receive_test.sigil",
    "argus_stage4_test.sigil",
)


def check(command, directory, **environment):
    completed = subprocess.run(
        command,
        cwd=directory,
        env={**os.environ, **environment},
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=1500,
    )
    if completed.stdout.strip() != "0":
        raise AssertionError(f"{command}: {completed.stdout}")


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
        for source in (*SOURCES, "Sigil.toml"):
            shutil.copy2(ROOT / source, stage)
        for source in ("argus_stage4_test.sigil",):
            subprocess.run([compiler, "check", "--root", ".", source], cwd=stage, check=True)
            native = stage / f"{Path(source).stem}-native"
            subprocess.run(
                [compiler, "build", "--root", ".", source, "main", "-o", native],
                cwd=stage,
                check=True,
            )
            evaluator = [compiler, "run", "--root", ".", source, "main"]
            for mode, command in (("eval", evaluator), ("native", [native])):
                database = str(stage / f"{mode}-{Path(source).stem}-database")
                check(command, stage, ARGUS_TEST_DB=database)
    print("Stage 4 tests passed")


if __name__ == "__main__":
    main()
