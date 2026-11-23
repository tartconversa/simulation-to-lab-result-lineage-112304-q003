import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import _bootstrap  # noqa: F401

from simulation_to_lab_result_lineage.cli import main
from simulation_to_lab_result_lineage.synthetic import write_fixture, write_update_fixture


class CliEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.fixture = root / "fixture"
        self.update = root / "update"
        self.db = root / "lineage.sqlite"
        write_fixture(self.fixture)
        write_update_fixture(self.update)

    def _run(self, *argv: str) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(list(argv))
        self.assertEqual(code, 0, buffer.getvalue())
        return buffer.getvalue()

    def test_full_workflow_text_and_json(self):
        out = self._run("ingest", "--data-dir", str(self.fixture), "--db", str(self.db))
        self.assertIn("accepted_with_issues", out)
        self.assertIn("orphan_result_ref", out)

        self._run("build-facts", "--db", str(self.db), "--now", "2026-10-20T00:00:00Z")
        query = self._run("query", "--verdict", "validated", "--db", str(self.db))
        self.assertIn("L-3001", query)
        self.assertIn("L-3008", query)

        json_out = self._run("report", "--db", str(self.db), "--json")
        report = json.loads(json_out)
        self.assertGreaterEqual(report["verdict_counts"]["validated"], 2)
        self.assertIn("reason_code_counts", report)

        # 增量接入 → 待复核 → 重算（时间戳显式注入，保证历史重建可断言）
        refresh = self._run(
            "refresh", "--data-dir", str(self.update),
            "--measurements-only", "--db", str(self.db),
            "--now", "2026-10-24T00:00:00Z",
        )
        self.assertIn("置待复核事实", refresh)

        versions = self._run("versions", "--link", "L-3008", "--metric", "yield", "--db", str(self.db))
        self.assertIn("v1", versions)
        self.assertIn("v2", versions)
        self.assertIn("superseded", versions)

        # 双负责人签收
        self._run(
            "signoff", "--link", "L-3001", "--metric", "binding", "--version", "1",
            "--role", "model_owner", "--owner", "M-OWNER",
            "--decision", "confirmed", "--comment", "模型版本适用", "--db", str(self.db),
        )
        self._run(
            "signoff", "--link", "L-3001", "--metric", "binding", "--version", "1",
            "--role", "experiment_owner", "--owner", "E-OWNER",
            "--decision", "confirmed", "--comment", "证据有效", "--db", str(self.db),
        )
        report_after = json.loads(self._run("report", "--db", str(self.db), "--json"))
        self.assertEqual(report_after["dual_confirmed_count"], 1)

        # 样品更正
        correction = self._run(
            "correct-sample", "--experiment", "EXP-3002", "--sample", "SAMPLE-B2",
            "--reason", "标签贴反", "--by", "STEWARD-1",
            "--db", str(self.db), "--json",
        )
        self.assertIn("SAMPLE-B2", correction)
        self.assertIn("flagged_facts", correction)

        # 历史重建：事实建立于 10-20、修订于 10-24；取 10-21 时点只见初始 yield
        history = json.loads(
            self._run("report", "--as-of", "2026-10-21T00:00:00Z", "--db", str(self.db), "--json")
        )
        metrics_3008 = {f["metric"] for f in history["facts"] if f["link_id"] == "L-3008"}
        self.assertEqual(metrics_3008, {"yield"})
        current = json.loads(self._run("report", "--db", str(self.db), "--json"))
        current_3008 = {f["metric"] for f in current["facts"] if f["link_id"] == "L-3008"}
        self.assertEqual(current_3008, {"yield", "stability"})

        issues = self._run("issues", "--db", str(self.db))
        self.assertIn("duplicate_pk", issues)
        batches = self._run("batches", "--db", str(self.db))
        self.assertIn("measurements.csv", batches)
        corrections = self._run("corrections", "--db", str(self.db))
        self.assertIn("SAMPLE-B2", corrections)

    def test_make_fixture_command(self):
        target = Path(self.tmp.name) / "generated"
        out = self._run("make-fixture", "--out", str(target))
        self.assertIn("已写入", out)
        self.assertTrue((target / "simulation_runs.csv").exists())

    def test_idempotent_ingest_via_cli(self):
        self._run("ingest", "--data-dir", str(self.fixture), "--db", str(self.db))
        second = self._run("ingest", "--data-dir", str(self.fixture), "--db", str(self.db))
        self.assertIn("skipped_unchanged", second)


if __name__ == "__main__":
    unittest.main()
