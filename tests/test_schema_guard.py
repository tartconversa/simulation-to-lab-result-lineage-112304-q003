import sqlite3
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from simulation_to_lab_result_lineage.ingest import ingest_directory
from simulation_to_lab_result_lineage.schema import initialize
from simulation_to_lab_result_lineage.synthetic import write_fixture


class ImmutableRawTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Path(self.tmp.name) / "fixture"
        write_fixture(self.fixture)
        self.conn = sqlite3.connect(Path(self.tmp.name) / "app.sqlite")
        initialize(self.conn)
        ingest_directory(self.conn, self.fixture)

    def tearDown(self):
        self.conn.close()

    def test_update_and_delete_blocked_on_raw_tables(self):
        cases = [
            ("simulation_runs", "UPDATE simulation_runs SET model_version = 'v9' WHERE simulation_id = 'SIM-3001'"),
            ("lab_runs", "DELETE FROM lab_runs WHERE experiment_id = 'EXP-3001'"),
            ("measurements", "UPDATE measurements SET value = 0 WHERE result_ref = 'SR-3001'"),
            ("validation_links", "DELETE FROM validation_links WHERE link_id = 'L-3001'"),
        ]
        for table, statement in cases:
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(statement)
        self.conn.rollback()
        # 原始值未受影响
        value = self.conn.execute(
            "SELECT value FROM measurements WHERE result_ref='SR-3001' AND replicate_no=1"
        ).fetchone()[0]
        self.assertEqual(value, 1.00)

    def test_insert_and_select_remain_allowed(self):
        n_before = self.conn.execute("SELECT COUNT(*) FROM lab_runs").fetchone()[0]
        self.conn.execute(
            """INSERT INTO lab_runs (
                experiment_id, sample_id, protocol_version, instrument_id,
                started_at, run_state, result_ref
            ) VALUES ('EXP-NEW', 'S-NEW', 'P-x', 'I-x', '2026-11-01T00:00:00Z', 'complete', 'LR-NEW')"""
        )
        n_after = self.conn.execute("SELECT COUNT(*) FROM lab_runs").fetchone()[0]
        self.assertEqual(n_after, n_before + 1)

    def test_versioned_fact_table_is_updatable_by_design(self):
        # 事实表需要状态迁移（active → superseded），防改写触发器只建在原始表上
        triggers = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        self.assertIn("trg_lab_runs_block_update", triggers)
        self.assertFalse(any("comparison_facts" in name for name in triggers))


if __name__ == "__main__":
    unittest.main()
