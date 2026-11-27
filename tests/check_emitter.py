#!/usr/bin/env python3
"""Run the Sigil tests and independently validate the emitted UTF-8 NDJSON."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "argus_observe_test.sigil"
TS = "2026-11-27T11:22:40.123456Z"
COMPONENT = "sigil-herald-queue"


def run(command):
    return subprocess.run(command, cwd=ROOT, check=True, stdout=subprocess.PIPE).stdout


def read_unique_keys(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError(f"duplicate key in {pairs!r}")
    return fields


class EmitterContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which(os.environ.get("SIGIL", "sigil"))
        if compiler is None:
            raise RuntimeError("Put sigil on PATH or set SIGIL to its path.")
        run([compiler, "fmt", "--check", "argus_observe.sigil", SOURCE])
        run([compiler, "check", "--root", ".", SOURCE])
        cls.evaluated = run([compiler, "run", "--root", ".", SOURCE, "main"])
        with tempfile.TemporaryDirectory() as scratch:
            executable = Path(scratch) / "argus-emitter-test"
            run([compiler, "build", "--root", ".", SOURCE, "main", "-o", executable])
            cls.native = run([executable])

    def fixtures(self, output):
        # The Sigil CLI and native harness print the final Int after the records.
        lines = output.splitlines(keepends=True)
        self.assertEqual(lines.pop(), b"0\n", "Sigil contract assertions failed")
        self.assertTrue(all(line.endswith(b"\n") and b"\r" not in line for line in lines))
        parsed = [
            json.loads(line.decode("utf-8"), object_pairs_hook=read_unique_keys)
            for line in lines
        ]
        return lines, parsed

    def test_evaluator_and_native_match(self):
        self.assertTrue(self.evaluated == self.native, "emitted bytes differ")

    def test_log_shape_and_boundaries(self):
        lines, records = self.fixtures(self.native)
        logs = records[:6]
        self.assertEqual(logs[0], {
            "ts": TS,
            "component": COMPONENT,
            "level": "DEBUG",
            "msg": 'quoted "path" \\root  次\t行',
            "trace_id": "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
            "span_id": "span",
            "target": "worker.run",
            "err": {"code": 42, "message": "読み込み失敗"},
            "fields": {"ok": False, "nested": [None, {"ratio": 0.5}]},
        })
        self.assertEqual(logs[1], {
            "ts": "2024-02-29T00:00:00.001Z",
            "component": COMPONENT,
            "level": "INFO",
            "msg": "",
        })
        self.assertEqual([log["level"] for log in logs[2:]], ["INFO", "WARN", "ERROR", "FATAL"])
        self.assertTrue(all(len(line) <= 65536 for line in lines[:6]))
        self.assertEqual(len(lines[2]), 65536)
        self.assertEqual(len(lines[3]), 65536)
        self.assertNotIn("truncated", logs[2])
        self.assertEqual(logs[2]["msg"], "a" * (65536 - len(lines[1])))
        self.assertEqual(len(lines[4]), 65535)
        self.assertEqual(logs[4]["msg"], logs[2]["msg"][:-19])
        for log in logs[3:]:
            self.assertIs(log["truncated"], True)
        escapes = '"\\\t' * 15000
        self.assertTrue(escapes.startswith(logs[5]["msg"]))
        self.assertLess(len(logs[5]["msg"]), len(escapes))

    def test_metric_contract_and_series(self):
        _, records = self.fixtures(self.native)
        counter, gauge, other, *histograms = records[6:]
        self.assertEqual(counter, {
            "ts": TS,
            "component": COMPONENT,
            "name": "requests_total",
            "type": "counter",
            "value": 3.5,
            "labels": {
                "component": COMPONENT,
                "target_component": "sigil-shield-tls",
                **dict.fromkeys("abcdefgh", "v"),
            },
        })
        for sample, component in ((gauge, COMPONENT), (other, "sigil-shield-tls")):
            self.assertEqual(sample, {
                "ts": TS,
                "component": component,
                "name": "queue_depth",
                "type": "gauge",
                "value": -2.5,
                "labels": {"component": component},
            })

        self.assertEqual(
            [(m["name"], m["value"], m["labels"].get("le")) for m in histograms],
            [
                ("duration_seconds_count", 4, None),
                ("duration_seconds_sum", 3.75, None),
                ("duration_seconds_bucket", 2, "0.5"),
                ("duration_seconds_bucket", 3, "2.0"),
                ("duration_seconds_bucket", 4, "+Inf"),
                ("bytes_total", sys.float_info.max, None),
                ("bytes_count", 1, None),
                ("bytes_sum", sys.float_info.max, None),
                ("bytes_bucket", 1, "+Inf"),
            ],
        )
        for sample in histograms:
            self.assertEqual(sample["ts"], TS)
            self.assertEqual(sample["component"], COMPONENT)
            self.assertEqual(sample["type"], "counter" if sample["name"] == "bytes_total" else "histogram")
            labels = {"component": COMPONENT}
            if sample["name"].startswith("duration_seconds_"):
                labels.update(dict.fromkeys("abcdefgh", "v"))
            if "le" in sample["labels"]:
                labels["le"] = sample["labels"]["le"]
            self.assertEqual(sample["labels"], labels)
            self.assertLessEqual(len(labels), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
