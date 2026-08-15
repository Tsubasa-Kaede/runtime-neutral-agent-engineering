import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"))

from logging_utils import log


class LoggingUtilsTests(unittest.TestCase):
    def test_log_writes_level_and_message_to_supplied_stream(self):
        stream = io.StringIO()

        log("route selected", level="debug", stream=stream)

        self.assertEqual(stream.getvalue(), "[DEBUG] route selected\n")

    def test_log_defaults_to_standard_error(self):
        original_stderr = sys.stderr
        stream = io.StringIO()
        try:
            sys.stderr = stream
            log("adapter unavailable")
        finally:
            sys.stderr = original_stderr

        self.assertEqual(stream.getvalue(), "[INFO] adapter unavailable\n")

    def test_log_rejects_empty_message(self):
        with self.assertRaises(ValueError):
            log("   ", stream=io.StringIO())

    def test_log_rejects_unknown_level(self):
        with self.assertRaises(ValueError):
            log("unexpected state", level="verbose", stream=io.StringIO())


if __name__ == "__main__":
    unittest.main()
