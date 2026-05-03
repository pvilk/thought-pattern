"""Tests for _parsers (week file parsing). Uses synthetic fixture data only."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# _parsers and _llm both import _config, which requires config.local.toml.
# Provide a minimal one for tests.
TMP = Path(tempfile.mkdtemp(prefix="vb-test-"))
(TMP / "config.local.toml").write_text("""
[user]
email = "test@example.com"
[llm]
backend = "cli"
model = "claude-sonnet-4-6"
[email]
smtp_host = "localhost"
smtp_port = 0
smtp_user = "test@example.com"
smtp_to = "test@example.com"
[sources]
[sources.wispr]
enabled = true
db_path = "/tmp/nonexistent.sqlite"
[sources.fathom]
enabled = false
[sources.granola]
enabled = false
[paths]
entries_dir = "data/entries"
weeks_dir = "data/weeks"
meetings_dir = "data/meetings"
digests_dir = "data/digests"
master_dir = "data/master"
logs_dir = "data/logs"
[schedule]
weekday = 0
hour = 8
minute = 0
""")

# Patch CONFIG_LOCAL to point at our temp config
import _config  # noqa: E402
_config.CONFIG_LOCAL = TMP / "config.local.toml"

import _parsers  # noqa: E402


class WeekFileParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures_dir = ROOT / "tests" / "fixtures"
        cls.alice = cls.fixtures_dir / "2026-W04-alice.md"

    def test_parses_synthetic_week(self):
        # Copy the fixture to a temporary weeks/ dir so load_all_weeks finds it
        weeks_dir = TMP / "weeks"
        weeks_dir.mkdir(exist_ok=True)
        target = weeks_dir / "2026-W04.md"
        shutil.copy(self.alice, target)

        record = _parsers.parse_week_file(target)
        self.assertEqual(record.label, "2026-W04")
        self.assertEqual(len(record.themes), 3)
        self.assertEqual(len(record.problems), 2)
        self.assertEqual(record.themes[0].name, "Brand voice automation pipeline")
        self.assertEqual(record.stats.entries, 312)
        self.assertEqual(record.stats.words, 9847)
        self.assertEqual(record.stats.days_active, 6)

    def test_load_all_weeks_returns_sorted(self):
        weeks_dir = TMP / "weeks"
        weeks_dir.mkdir(exist_ok=True)
        shutil.copy(self.alice, weeks_dir / "2026-W04.md")
        shutil.copy(self.alice, weeks_dir / "2026-W05.md")
        records = _parsers.load_all_weeks(weeks_dir)
        self.assertEqual([r.label for r in records], ["2026-W04", "2026-W05"])


if __name__ == "__main__":
    unittest.main()
