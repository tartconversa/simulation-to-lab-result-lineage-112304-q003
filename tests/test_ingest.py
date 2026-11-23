import csv
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from simulation_to_lab_result_lineage.ingest import (
    LAB_FILE,
    LINK_FILE,
    MEASUREMENT_FILE,
    SIMULATION_FILE,
    ingest_directory,
    ingest_file,
)
from simulation_to_lab_result_lineage.schema import initialize
from simulation_to_lab_result_lineage.synthetic import write_fixture

import sqlite3


class IngestFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Path(self.tmp.name) / "fixture"
        write_fixture(self.fixture)
        self.db = Path(self.tmp.name) / "app.sqlite"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        initialize(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_full_fixture_ingest_counts_and_issues(self):
        reports = ingest_directory(self.conn, self.fixture)
        by_file = {report.file_name: report for report in reports}
        # 19 条测量中：1 条重复主键 + 1 条负不确定度被隔离，17 条写入
        self.assertEqual(by_file[MEASUREMENT_FILE].inserted_count, 17)
        self.assertEqual(by_file[MEASUREMENT_FILE].status, "accepted_with_issues")
        # 8 条链接全部有效
        self.assertEqual(by_file[LINK_FILE].inserted_count, 8)

        issues = self.conn.execute("SELECT issue_code, COUNT(*) AS n FROM ingest_issues GROUP BY issue_code").fetchall()
        counts = {row["issue_code"]: row["n"] for row in issues}
        self.assertEqual(counts["duplicate_pk"], 1)
        self.assertEqual(counts["invalid_numeric"], 1)
        # 孤立引用 LR-9999：测量行保留但登记 orphan 问题
        self.assertEqual(counts["orphan_result_ref"], 1)
        orphan = self.conn.execute(
            "SELECT * FROM measurements WHERE result_ref = 'LR-9999'"
        ).fetchall()
        self.assertEqual(len(orphan), 1)

    def test_reingest_same_content_is_noop(self):
        first = ingest_file(self.conn, self.fixture / SIMULATION_FILE)
        second = ingest_file(self.conn, self.fixture / SIMULATION_FILE)
        self.assertEqual(first.status, "accepted")
        self.assertEqual(second.status, "skipped_unchanged")
        self.assertEqual(second.inserted_count, 0)
        batches = self.conn.execute("SELECT COUNT(*) AS n FROM ingest_batches").fetchone()
        self.assertEqual(batches["n"], 1)

    def test_raw_rows_are_never_overwritten(self):
        ingest_directory(self.conn, self.fixture)
        row = self.conn.execute(
            "SELECT value FROM measurements WHERE result_ref='SR-3001' AND metric='binding' AND replicate_no=1"
        ).fetchone()
        self.assertEqual(row["value"], 1.00)  # 重复行取值 1.01 被隔离，首值保留

    def test_dangling_link_is_quarantined(self):
        extra_dir = Path(self.tmp.name) / "extra"
        extra_dir.mkdir()
        bad_link = [
            ["link_id", "simulation_id", "experiment_id", "condition_map_version",
             "review_state", "reviewer", "decision_at"],
            ["L-BAD", "SIM-3001", "EXP-0000", "CM-9", "proposed", "REV-X", "2026-10-19"],
        ]
        with (extra_dir / LINK_FILE).open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows(bad_link)
        report = ingest_file(self.conn, extra_dir / LINK_FILE)
        self.assertEqual(report.inserted_count, 0)
        self.assertEqual(report.issues[0].issue_code, "dangling_link")
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM validation_links WHERE link_id='L-BAD'").fetchone()
        )

    def test_bad_header_raises(self):
        bad_dir = Path(self.tmp.name) / "bad"
        bad_dir.mkdir()
        with (bad_dir / LAB_FILE).open("w", encoding="utf-8", newline="") as stream:
            stream.write("experiment_id,wrong\nEXP-X,1\n")
        with self.assertRaises(ValueError):
            ingest_file(self.conn, bad_dir / LAB_FILE)

    def test_failed_and_aborted_runs_are_retained(self):
        ingest_directory(self.conn, self.fixture)
        states = dict(
            (row["experiment_id"], row["run_state"])
            for row in self.conn.execute("SELECT experiment_id, run_state FROM lab_runs")
        )
        self.assertEqual(states["EXP-3004"], "failed")
        self.assertEqual(states["EXP-3007"], "aborted")


if __name__ == "__main__":
    unittest.main()
