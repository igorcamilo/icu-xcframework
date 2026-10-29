from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from job_log import stream_command
import job_log


class JobLogTests(unittest.TestCase):
    def test_silent_child_reports_progress_before_it_completes(self):
        with tempfile.TemporaryDirectory() as temporary:
            acknowledgement = Path(temporary) / "heartbeat-observed"
            class Observer(io.StringIO):
                def write(self, text):
                    if "Still running" in text:
                        acknowledgement.touch()
                    return super().write(text)
            source = (
                "from pathlib import Path\nimport sys, time\n"
                "deadline = time.monotonic() + 10\n"
                "while not Path(sys.argv[1]).exists():\n"
                "    if time.monotonic() > deadline: sys.exit(7)\n"
                "    time.sleep(0.01)\n"
                "print('finished', flush=True)\n"
            )
            output = Observer()
            with redirect_stdout(output), patch.object(job_log, "HEARTBEAT_SECONDS", 0.05):
                stream_command([sys.executable, "-u", "-c", source, acknowledgement], label="silent")
            self.assertIn("[silent] Still running", output.getvalue())
            self.assertIn("[silent] finished", output.getvalue())

    def test_output_is_visible_before_child_exits_and_also_saved_locally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            acknowledgement = root / "acknowledged"
            class Observer(io.StringIO):
                def write(self, text):
                    if "[arm64/build] ready" in text:
                        acknowledgement.touch()
                    return super().write(text)
            output = Observer()
            source = (
                "from pathlib import Path\nimport sys, time\n"
                "print('ready', flush=True)\n"
                "deadline = time.monotonic() + 10\n"
                "while not Path(sys.argv[1]).exists():\n"
                "    if time.monotonic() > deadline: sys.exit(7)\n"
                "    time.sleep(0.01)\n"
                "print('diagnostic', file=sys.stderr, flush=True)\n"
                "print('finished', flush=True)\n"
            )
            log = root / "build.log"
            with redirect_stdout(output):
                stream_command([sys.executable, "-u", "-c", source, acknowledgement],
                               label="arm64/build", log=log)
            self.assertTrue(acknowledgement.exists())
            self.assertIn("[arm64/build] diagnostic", output.getvalue())
            self.assertIn("[arm64/build] finished", output.getvalue())
            self.assertIn("DONE in", output.getvalue())
            self.assertIn("ready\ndiagnostic\nfinished\n", log.read_text())

    def test_failure_diagnostics_remain_visible_for_captured_inspections(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "exit 9"):
            stream_command([sys.executable, "-c", "import sys; print('broken object'); sys.exit(9)"],
                           label="metadata", capture=True, show_output=False)
        self.assertIn("[metadata] broken object", output.getvalue())
        self.assertIn("FAILED: exit 9", output.getvalue())

    def test_parallel_commands_keep_individual_line_labels(self):
        output = io.StringIO()
        with redirect_stdout(output), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(stream_command,
                                   [sys.executable, "-u", "-c", f"print('line for {arch}')"],
                                   label=arch, capture=True) for arch in ("arm64", "x86_64")]
            results = [future.result() for future in futures]
        self.assertEqual(results, ["line for arm64", "line for x86_64"])
        self.assertIn("[arm64] line for arm64\n", output.getvalue())
        self.assertIn("[x86_64] line for x86_64\n", output.getvalue())


if __name__ == "__main__":
    unittest.main()
