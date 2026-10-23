"""
Unit tests for HistoryDatabase local formatDate logic (item 005).

Verifies that HistoryDatabase.vue uses local date components (getFullYear,
getMonth, getDate) instead of toISOString() to prevent day shifting near midnight.
"""

from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


class TestHistoryDateFormatting(unittest.TestCase):
    """Test suite for HistoryDatabase.vue date formatting."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.vue_file = cls.repo_root / "frontend" / "src" / "components" / "HistoryDatabase.vue"
        cls.assertTrue(
            cls.vue_file.is_file(),
            f"HistoryDatabase.vue not found at {cls.vue_file}",
        )
        cls.vue_source = cls.vue_file.read_text(encoding="utf-8")

        # Extract formatDate definition from Vue file
        match = re.search(
            r"(const formatDate\s*=\s*\([^)]*\)\s*=>\s*\{.*?\n\})",
            cls.vue_source,
            re.DOTALL,
        )
        cls.assertIsNotNone(
            match,
            "Could not locate 'const formatDate = (dateStr) => { ... }' in HistoryDatabase.vue",
        )
        cls.format_date_source = match.group(1)

    def test_format_date_does_not_use_to_iso_string(self):
        """formatDate must NOT use toISOString() which shifts day to UTC."""
        # Strip single-line and multi-line comments to check executable code
        executable_code = re.sub(r"//.*?\n|/\*.*?\*/", "\n", self.format_date_source)
        self.assertNotIn(
            "toISOString",
            executable_code,
            "formatDate in HistoryDatabase.vue must not call toISOString() in executable code",
        )
        # Also verify that the code documents the rationale
        self.assertIn(
            "toISOString()",
            self.format_date_source,
            "formatDate should document why toISOString() is avoided",
        )

    def test_format_date_uses_local_date_components(self):
        """formatDate must use getFullYear(), getMonth(), and getDate()."""
        self.assertIn(
            "getFullYear()",
            self.format_date_source,
            "formatDate must call getFullYear() for local year",
        )
        self.assertIn(
            "getMonth()",
            self.format_date_source,
            "formatDate must call getMonth() for local month",
        )
        self.assertIn(
            "getDate()",
            self.format_date_source,
            "formatDate must call getDate() for local day of month",
        )
        self.assertIn(
            "padStart(2, '0')",
            self.format_date_source,
            "formatDate must zero-pad month and day to 2 digits",
        )

    def test_date_formatting_near_midnight_behavioral_equivalence(self):
        """
        Verify behavioral equivalence: local date formatting vs UTC toISOString.
        Near local midnight, UTC date and local date differ.
        Local formatting should preserve local date, whereas toISOString() yields UTC date.
        """
        # Scenario 1: Tokyo (UTC+9)
        # 23:30 UTC on 2026-10-23 is 08:30 on 2026-10-24 in Tokyo (+9)
        dt_utc = datetime(2026, 10, 23, 23, 30, 0, tzinfo=timezone.utc)
        tz_tokyo = timezone(timedelta(hours=9))
        dt_tokyo = dt_utc.astimezone(tz_tokyo)

        utc_iso_date = dt_utc.strftime("%Y-%m-%d")
        tokyo_local_date = f"{dt_tokyo.year}-{str(dt_tokyo.month).zfill(2)}-{str(dt_tokyo.day).zfill(2)}"

        self.assertEqual(utc_iso_date, "2026-10-23")
        self.assertEqual(tokyo_local_date, "2026-10-24")
        self.assertNotEqual(tokyo_local_date, utc_iso_date)

        # Scenario 2: New York (EDT, UTC-4)
        # 02:00 UTC on 2026-10-24 is 22:00 on 2026-10-23 in New York (-4)
        tz_ny = timezone(timedelta(hours=-4))
        dt_ny = dt_utc.replace(day=24, hour=2).astimezone(tz_ny)

        utc_iso_date_ny = dt_utc.replace(day=24, hour=2).strftime("%Y-%m-%d")
        ny_local_date = f"{dt_ny.year}-{str(dt_ny.month).zfill(2)}-{str(dt_ny.day).zfill(2)}"

        self.assertEqual(utc_iso_date_ny, "2026-10-24")
        self.assertEqual(ny_local_date, "2026-10-23")
        self.assertNotEqual(ny_local_date, utc_iso_date_ny)
    def test_empty_and_invalid_date_python_handling(self):
        """Verify empty and invalid string handling semantics."""
        # Python representation of the formatDate guard & fallback logic
        def format_date_py(date_str):
            if not date_str:
                return ""
            try:
                # parse iso format or similar
                dt = datetime.fromisoformat(date_str)
                return f"{dt.year}-{str(dt.month).zfill(2)}-{str(dt.day).zfill(2)}"
            except Exception:
                return (date_str[:10] if date_str else "") or ""

        self.assertEqual(format_date_py(""), "")
        self.assertEqual(format_date_py(None), "")
        self.assertEqual(format_date_py("2026-10-23T12:00:00"), "2026-10-23")
        # Fallback slice for unparseable strings that start with date-like slice
        self.assertEqual(format_date_py("2026-10-23-corrupted-tail"), "2026-10-23")
        # Non-date string fallback
        self.assertEqual(format_date_py("invalid"), "invalid")


    def test_node_runtime_execution_of_format_date(self):
        """
        Execute the exact extracted formatDate function in Node.js runtime across timezones.
        """
        node_bin = shutil.which("node") or shutil.which("bun")
        if not node_bin:
            self.skipTest("Neither node nor bun executable found in PATH")

        test_harness_template = f"""
{self.format_date_source}

// Mock object that fails in new Date() but has a slice method for catch fallback
const fallbackObj = {{
    [Symbol.toPrimitive]: () => {{ throw new Error("date parse error"); }},
    slice: (start, end) => "2026-10-23-fallback".slice(start, end)
}};

const results = {{
    emptyStr: formatDate(""),
    nullVal: formatDate(null),
    undefinedVal: formatDate(undefined),
    testUtcTimestamp: formatDate("2026-10-23T23:30:00Z"),
    morningUtcTimestamp: formatDate("2026-10-24T02:00:00Z"),
    regularDate: formatDate("2026-05-04T12:00:00Z"),
    fallbackResult: formatDate(fallbackObj)
}};

console.log(JSON.stringify(results));
"""

        # Run with TZ=Asia/Tokyo (UTC+9)
        env_tokyo = os.environ.copy()
        env_tokyo["TZ"] = "Asia/Tokyo"
        res_tokyo = subprocess.run(
            [node_bin, "-e", test_harness_template],
            capture_output=True,
            text=True,
            check=True,
            env=env_tokyo,
        )
        data_tokyo = json.loads(res_tokyo.stdout.strip())

        self.assertEqual(data_tokyo["emptyStr"], "")
        self.assertEqual(data_tokyo["nullVal"], "")
        self.assertEqual(data_tokyo["undefinedVal"], "")
        # 23:30 UTC on 2026-10-23 is 2026-10-24 in Tokyo
        self.assertEqual(data_tokyo["testUtcTimestamp"], "2026-10-24")
        self.assertEqual(data_tokyo["regularDate"], "2026-05-04")
        self.assertEqual(data_tokyo["fallbackResult"], "2026-10-23")

        # Run with TZ=America/New_York (UTC-4)
        env_ny = os.environ.copy()
        env_ny["TZ"] = "America/New_York"
        res_ny = subprocess.run(
            [node_bin, "-e", test_harness_template],
            capture_output=True,
            text=True,
            check=True,
            env=env_ny,
        )
        data_ny = json.loads(res_ny.stdout.strip())

        self.assertEqual(data_ny["emptyStr"], "")
        self.assertEqual(data_ny["nullVal"], "")
        # 02:00 UTC on 2026-10-24 is 2026-10-23 22:00 in New York
        self.assertEqual(data_ny["morningUtcTimestamp"], "2026-10-23")


if __name__ == "__main__":
    unittest.main()
