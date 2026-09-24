from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("jev_social_agent", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def decision(route: str = "tiktok_search", confidence: float = 0.87) -> dict:
    return {
        "model": "~typesafe/jev-latest",
        "answers": {
            "route": {
                "type": "choice",
                "choice": route,
                "confidence": confidence,
                "probabilities": {route: confidence},
            }
        },
    }


class DecisionTests(unittest.TestCase):
    def test_accepts_typed_route(self) -> None:
        chosen = MODULE.validate_decision(decision(), "auto")
        self.assertEqual(chosen["platform"], "tiktok")
        self.assertEqual(chosen["confidence"], 0.87)

    def test_rejects_malformed_or_conflicting_routes(self) -> None:
        malformed = ([], {"answers": []}, {"answers": {"route": []}})
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(MODULE.JevSocialError):
                MODULE.validate_decision(payload, "auto")
        with self.assertRaises(MODULE.JevSocialError):
            MODULE.validate_decision(decision("unsupported", 0.9), "auto")
        with self.assertRaises(MODULE.JevSocialError):
            MODULE.validate_decision(decision("instagram_search", 0.9), "tiktok")
        with self.assertRaises(MODULE.JevSocialError):
            MODULE.validate_decision(decision("tiktok_search", 1.1), "auto")


class ExecutionTests(unittest.TestCase):
    def make_socai(self, root: Path) -> Path:
        script = root / "socai"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "assert 'OPENROUTER_API_KEY' not in os.environ\n"
            "assert sys.argv[1:3] == ['tiktok', 'search']\n"
            "assert sys.argv[3:6] == ['--num', '4', '--pretty']\n"
            "assert sys.argv[6] == '--'\n"
            "assert sys.argv[7] == '--help; touch /tmp/not-executed'\n"
            "print(json.dumps({'results': ["
            "{'username': 'maker', 'caption': 'Useful demo', "
            "'url': 'https://www.tiktok.com/@maker/video/1', "
            "'local_path': '/private/run'}]}))\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_fixed_command_and_credential_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = self.make_socai(Path(temp_dir))
            command = MODULE.build_socai_command(
                str(executable), "tiktok", "--help; touch /tmp/not-executed", 4
            )
            self.assertEqual(command[1:3], ["tiktok", "search"])
            self.assertEqual(command[3:6], ["--num", "4", "--pretty"])
            self.assertEqual(command[6], "--")
            self.assertEqual(command[7], "--help; touch /tmp/not-executed")
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "do-not-forward"}):
                payload, _elapsed = MODULE.run_socai(
                    str(executable), "tiktok", command[7], 4
                )
            items = MODULE.project_records(payload, "tiktok", 4)
            self.assertEqual(len(items), 1)
            self.assertNotIn("local_path", json.dumps(items))

    def test_combined_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "socai-flood"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdout.write('x' * 512)\n"
                "sys.stderr.write('y' * 512)\n",
                encoding="utf-8",
            )
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            with self.assertRaisesRegex(MODULE.JevSocialError, "size limit"):
                MODULE.run_socai(
                    str(script),
                    "tiktok",
                    "topic",
                    4,
                    max_output_bytes=128,
                )

    def test_inherited_output_handles_do_not_defeat_timeout(self) -> None:
        child = (
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)']); "
            "print('complete')"
        )
        started = time.monotonic()
        returncode, stdout, _stderr = MODULE.collect_bounded(
            [sys.executable, "-c", child], os.environ.copy(), 1, 1024
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(stdout, "complete\n")
        self.assertLess(time.monotonic() - started, 0.8)

    def test_descendant_cannot_write_after_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "escaped-write"
            grandchild = (
                "import pathlib, time; "
                "time.sleep(0.25); "
                f"pathlib.Path({str(marker)!r}).write_text('escaped')"
            )
            leader = (
                "import subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])"
            )
            returncode, _stdout, _stderr = MODULE.collect_bounded(
                [sys.executable, "-c", leader], os.environ.copy(), 1, 1024
            )
            self.assertEqual(returncode, 0)
            time.sleep(0.4)
            self.assertFalse(marker.exists())

    def test_timeout_terminates_the_process_group(self) -> None:
        child = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); "
            "time.sleep(5)"
        )
        started = time.monotonic()
        with self.assertRaisesRegex(MODULE.JevSocialError, "timed out"):
            MODULE.collect_bounded(
                [sys.executable, "-c", child], os.environ.copy(), 0.2, 1024
            )
        self.assertLess(time.monotonic() - started, 1.0)


class EvidenceTests(unittest.TestCase):
    def test_safe_text_preserves_neutralized_urls(self) -> None:
        rendered = MODULE.safe_text("visit https://example.com/#topic")
        self.assertEqual(rendered, "visit https&#58;//example.com/\\#topic")
        self.assertNotIn("&\\#58;", rendered)

    def test_filters_wrong_hosts_and_neutralizes_markdown(self) -> None:
        payload = {
            "results": [
                {
                    "author": "[fake](https://attacker.invalid)",
                    "caption": "*untrusted* | text",
                    "url": "https://www.tiktok.com/@maker/video/1) [Injected](https://attacker.invalid/)",
                },
                {"caption": "wrong platform", "url": "https://www.instagram.com/p/1"},
                {"url": "https://127.0.0.1/private"},
                {"url": "https://[malformed"},
                {"url": "https://www.tiktok.com.attacker.invalid/video/1"},
            ]
        }
        items = MODULE.project_records(payload, "tiktok", 4)
        self.assertEqual(len(items), 1)
        report = MODULE.build_report(
            "test", {"platform": "tiktok", "confidence": 1.0}, items, 1, 1
        )
        self.assertNotIn("[fake](", report)
        self.assertNotIn("*untrusted*", report)
        self.assertNotIn(") [Injected](", report)
        self.assertNotIn("instagram.com", report)

    def test_fixture_path_runs_without_network_or_browser(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = MODULE.main(
                [
                    "Find emerging AI creator formats on Instagram",
                    "--fixture",
                ]
            )
        report = stdout.getvalue()
        self.assertEqual(status, 0)
        self.assertIn("Jev × socai social research brief", report)
        self.assertIn("Captured 4 source-linked records", report)
        self.assertNotIn("local_path", report)
        self.assertNotIn("raw_debug", report)


if __name__ == "__main__":
    unittest.main()
