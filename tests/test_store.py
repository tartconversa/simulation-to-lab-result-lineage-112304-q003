import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from simulation_to_lab_result_lineage.ingest import ingest_directory, ingest_file
from simulation_to_lab_result_lineage.store import (
    apply_sample_correction,
    build_report,
    fact_uid_for,
    flag_affected_for_rereview,
    get_fact_versions,
    links_for_measurements,
    open_db,
    recompute_pending,
    rebuild_facts,
    signoff,
    list_facts,
)
from simulation_to_lab_result_lineage.synthetic import write_fixture, write_update_fixture


class StoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.fixture = root / "fixture"
        self.db_path = root / "app.sqlite"
        write_fixture(self.fixture)
        self.conn = open_db(self.db_path)
        ingest_directory(self.conn, self.fixture)
        rebuild_facts(self.conn, now="2026-10-20T00:00:00Z")

    def tearDown(self):
        self.conn.close()

    def _fact(self, link_id: str, metric: str):
        facts = list_facts(self.conn, link_id=link_id)
        chosen = [fact for fact in facts if fact["metric"] == metric]
        self.assertEqual(len(chosen), 1, f"{link_id}/{metric} 事实不唯一: {facts}")
        return chosen[0]

    def test_initial_verdicts_match_scenario_index(self):
        expectations = {
            "L-3001": ("binding", "validated"),
            "L-3002": ("binding", "contradicted"),
            "L-3003": ("energy", "not_comparable"),
            "L-3004": ("binding", "not_comparable"),
            "L-3005": ("energy", "not_comparable"),
            "L-3006": ("binding", "not_comparable"),
            "L-3008": ("yield", "validated"),
        }
        for link_id, (metric, verdict) in expectations.items():
            with self.subTest(link_id=link_id):
                self.assertEqual(self._fact(link_id, metric)["verdict"], verdict)

        self.assertEqual(self._fact("L-3003", "energy")["reason_code"], "unit_mismatch")
        self.assertEqual(self._fact("L-3004", "binding")["reason_code"], "lab_run_not_complete")
        self.assertEqual(self._fact("L-3005", "energy")["reason_code"], "missing_lab_measurement")
        self.assertEqual(self._fact("L-3006", "binding")["reason_code"], "missing_simulation_measurement")
        none_fact = self._fact("L-3007", "(none)")
        self.assertEqual(none_fact["reason_code"], "no_shared_metric")

    def test_replicates_are_aggregated_not_flattened(self):
        fact = self._fact("L-3002", "binding")
        self.assertEqual(fact["sim_replicate_count"], 2)
        self.assertEqual(fact["lab_replicate_count"], 2)
        self.assertAlmostEqual(fact["sim_value"], 2.05)
        self.assertAlmostEqual(fact["lab_value"], 2.90)

    def test_facts_carry_lineage_fields(self):
        fact = self._fact("L-3001", "binding")
        self.assertEqual(fact["model_version"], "v3")
        self.assertEqual(fact["protocol_version"], "P-01-v2")
        self.assertEqual(fact["condition_map_version"], "CM-7")
        self.assertEqual(fact["sample_id"], "SAMPLE-A1")
        self.assertEqual(fact["sim_result_ref"], "SR-3001")
        self.assertEqual(fact["lab_result_ref"], "LR-3001")

    def test_rebuild_is_idempotent(self):
        again = rebuild_facts(self.conn, now="2026-10-21T00:00:00Z")
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["skipped_links"], 8)

    def test_dual_owner_signoffs_are_separate(self):
        fact = self._fact("L-3001", "binding")
        signoff(self.conn, fact["fact_uid"], fact["version_no"],
                "model_owner", "OWNER-M1", "confirmed", "预测版本 v3 适用条件确认",
                now="2026-10-22T00:00:00Z")
        signoff(self.conn, fact["fact_uid"], fact["version_no"],
                "experiment_owner", "OWNER-E1", "confirmed", "样品与设备证据有效",
                now="2026-10-22T01:00:00Z")
        report = build_report(self.conn)
        confirmed = [item for item in report["facts"] if item["dual_confirmed"]]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["link_id"], "L-3001")

        # 同一负责人可更正自己的签收（UPSERT），不会出现两行同角色记录
        signoff(self.conn, fact["fact_uid"], fact["version_no"],
                "model_owner", "OWNER-M1", "rejected", "复查后撤回确认",
                now="2026-10-22T02:00:00Z")
        report = build_report(self.conn)
        self.assertEqual(report["dual_confirmed_count"], 0)
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM signoffs WHERE fact_uid=? AND role='model_owner'",
            (fact["fact_uid"],),
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_refresh_flags_pending_then_recomputes_new_version(self):
        update_dir = Path(self.tmp.name) / "update"
        write_update_fixture(update_dir)

        before = self._fact("L-3008", "yield")
        self.assertEqual(before["lab_replicate_count"], 1)

        report = ingest_file(self.conn, update_dir / "measurements.csv")
        self.assertEqual(report.inserted_count, 4)
        affected = links_for_measurements(self.conn, ["LR-3008", "SR-3008"])
        self.assertIn("L-3008", affected)
        flagged = flag_affected_for_rereview(
            self.conn, affected, triggered_by="test",
            detail="新增实测重复与 stability 指标", now="2026-10-23T00:00:00Z",
        )
        self.assertGreaterEqual(flagged, 1)
        pending = list_facts(self.conn, status="re_review_pending", active_only=False)
        self.assertTrue(any(f["link_id"] == "L-3008" for f in pending))

        result = recompute_pending(self.conn, now="2026-10-24T00:00:00Z")
        self.assertGreaterEqual(result["recomputed_facts"], 2)

        versions = get_fact_versions(self.conn, fact_uid_for("L-3008", "yield"))
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["status"], "superseded")
        self.assertEqual(versions[1]["status"], "active")
        # 旧版本原始取值完整保留
        self.assertEqual(versions[0]["lab_replicate_count"], 1)
        self.assertEqual(versions[1]["lab_replicate_count"], 3)
        # 新指标 stability 也建立了事实
        stability = self._fact("L-3008", "stability")
        self.assertEqual(stability["verdict"], "validated")

    def test_historical_report_reconstruction_as_of(self):
        # 先签收，再做增量修订
        fact = self._fact("L-3001", "binding")
        signoff(self.conn, fact["fact_uid"], fact["version_no"],
                "model_owner", "OWNER-M1", "confirmed", "初始确认",
                now="2026-10-22T00:00:00Z")

        update_dir = Path(self.tmp.name) / "update"
        write_update_fixture(update_dir)
        ingest_file(self.conn, update_dir / "measurements.csv")
        affected = links_for_measurements(self.conn, ["LR-3008", "SR-3008"])
        flag_affected_for_rereview(
            self.conn, affected, triggered_by="test", detail="增量",
            now="2026-10-23T00:00:00Z",
        )
        recompute_pending(self.conn, now="2026-10-24T00:00:00Z")

        current = build_report(self.conn)
        historical = build_report(self.conn, as_of="2026-10-21T00:00:00Z")
        # 历史时点看不到 10-22 的签收
        self.assertEqual(historical["dual_confirmed_count"], 0)
        # 历史时点 L-3008/yield 只有一个版本，且重复数为 1
        hist_3008 = [f for f in historical["facts"] if f["link_id"] == "L-3008"]
        self.assertEqual({f["metric"] for f in hist_3008}, {"yield"})
        self.assertEqual(hist_3008[0]["lab_replicate_count"], 1)
        # 当前视图包含 stability 新事实与 yield 新版本
        cur_3008 = [f for f in current["facts"] if f["link_id"] == "L-3008"]
        self.assertEqual({f["metric"] for f in cur_3008}, {"yield", "stability"})
        self.assertEqual(current["as_of"], None)

    def test_sample_correction_appends_and_flags_without_touching_raw_row(self):
        original = self.conn.execute(
            "SELECT sample_id FROM lab_runs WHERE experiment_id='EXP-3002'"
        ).fetchone()["sample_id"]
        payload = apply_sample_correction(
            self.conn, "EXP-3002", "SAMPLE-B2",
            reason="发现样品标签贴反", corrected_by="DATA-STEWARD-1",
            now="2026-10-25T00:00:00Z",
        )
        self.assertEqual(payload["old_sample_id"], "SAMPLE-A2")
        self.assertIn("L-3002", payload["affected_links"])
        # 原始行不变
        still = self.conn.execute(
            "SELECT sample_id FROM lab_runs WHERE experiment_id='EXP-3002'"
        ).fetchone()["sample_id"]
        self.assertEqual(still, original)

        # 待复核事实上记录了新的有效样品（重算后体现）
        recompute_pending(self.conn, now="2026-10-26T00:00:00Z")
        new_fact = self._fact("L-3002", "binding")
        self.assertEqual(new_fact["sample_id"], "SAMPLE-B2")
        versions = get_fact_versions(self.conn, new_fact["fact_uid"])
        self.assertEqual(versions[0]["sample_id"], "SAMPLE-A2")
        self.assertEqual(versions[1]["sample_id"], "SAMPLE-B2")
        revision_types = {
            rev["change_type"]
            for version in versions for rev in version["revisions"]
        }
        self.assertIn("re_review_pending", revision_types)
        self.assertIn("recomputed", revision_types)

    def test_sample_correction_same_value_rejected(self):
        with self.assertRaises(ValueError):
            apply_sample_correction(
                self.conn, "EXP-3001", "SAMPLE-A1",
                reason="重复提交", corrected_by="DATA-STEWARD-1",
            )

    def test_report_counts_and_query_filters(self):
        report = build_report(self.conn)
        total = sum(report["verdict_counts"].values())
        self.assertEqual(total, report["fact_count"])
        validated = list_facts(self.conn, verdict="validated")
        self.assertEqual(len(validated), report["verdict_counts"]["validated"])
        not_comparable = list_facts(self.conn, verdict="not_comparable")
        reason_codes = {fact["reason_code"] for fact in not_comparable}
        # 基线夹具覆盖全部不可比较原因码（no_shared_metric 亦在其中）
        self.assertIn("unit_mismatch", reason_codes)
        self.assertIn("lab_run_not_complete", reason_codes)
        self.assertIn("no_shared_metric", reason_codes)


if __name__ == "__main__":
    unittest.main()
