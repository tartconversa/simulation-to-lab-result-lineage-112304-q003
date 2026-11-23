import math
import unittest

import _bootstrap  # noqa: F401  导入路径引导

from simulation_to_lab_result_lineage.domain import (
    DEFAULT_TOLERANCE_Z,
    REASON_LAB_NOT_COMPLETE,
    REASON_MISSING_LAB_MEASUREMENT,
    REASON_MISSING_SIM_MEASUREMENT,
    REASON_NO_SHARED_METRIC,
    REASON_UNIT_MISMATCH,
    MeasurementPoint,
    aggregate_measurements,
    compare_link,
    compare_metric,
    effective_sample_id,
    summarize_verdicts,
)


def point(ref: str, metric: str, unit: str, values, uncertainties=None) -> MeasurementPoint:
    uncertainties = uncertainties or [0.05] * len(values)
    return MeasurementPoint(
        result_ref=ref,
        metric=metric,
        unit=unit,
        values=tuple(values),
        uncertainties=tuple(uncertainties),
        replicate_numbers=tuple(range(1, len(values) + 1)),
    )


class CompareMetricTests(unittest.TestCase):
    def test_validated_within_tolerance(self):
        sim = point("SR-1", "binding", "nM", [1.00])
        lab = point("LR-1", "binding", "nM", [1.10])
        verdict = compare_metric("binding", sim, lab, "complete")
        self.assertEqual(verdict.verdict, "validated")
        self.assertAlmostEqual(verdict.abs_diff, 0.10)
        self.assertAlmostEqual(verdict.tolerance, 2 * math.sqrt(0.05**2 + 0.05**2))

    def test_contradicted_outside_tolerance(self):
        sim = point("SR-2", "binding", "nM", [2.00, 2.10])
        lab = point("LR-2", "binding", "nM", [2.85, 2.95])
        verdict = compare_metric("binding", sim, lab, "complete")
        self.assertAlmostEqual(sim.mean, 2.05)
        self.assertAlmostEqual(lab.mean, 2.90)
        self.assertEqual(verdict.verdict, "contradicted")
        self.assertGreater(verdict.abs_diff, verdict.tolerance)

    def test_zero_uncertainty_means_zero_tolerance(self):
        sim = point("SR-3", "binding", "nM", [1.0], [0.0])
        lab = point("LR-3", "binding", "nM", [1.0001], [0.0])
        verdict = compare_metric("binding", sim, lab, "complete")
        self.assertEqual(verdict.tolerance, 0.0)
        self.assertEqual(verdict.verdict, "contradicted")

    def test_exact_match_zero_uncertainty_validated(self):
        sim = point("SR-4", "binding", "nM", [1.0], [0.0])
        lab = point("LR-4", "binding", "nM", [1.0], [0.0])
        verdict = compare_metric("binding", sim, lab, "complete")
        self.assertEqual(verdict.verdict, "validated")

    def test_unit_mismatch_never_converted(self):
        sim = point("SR-5", "energy", "kJ", [3.0])
        lab = point("LR-5", "energy", "J", [3000.0], [50.0])
        verdict = compare_metric("energy", sim, lab, "complete")
        self.assertEqual(verdict.verdict, "not_comparable")
        self.assertEqual(verdict.reason_code, REASON_UNIT_MISMATCH)
        self.assertIsNone(verdict.abs_diff)

    def test_failed_experiment_is_retained_but_not_evidence(self):
        sim = point("SR-6", "binding", "nM", [4.0])
        lab = point("LR-6", "binding", "nM", [4.05])
        for state in ("failed", "aborted"):
            with self.subTest(state=state):
                verdict = compare_metric("binding", sim, lab, state)
                self.assertEqual(verdict.verdict, "not_comparable")
                self.assertEqual(verdict.reason_code, REASON_LAB_NOT_COMPLETE)
                # 实测数据仍然随结论保留
                self.assertIsNotNone(verdict.lab)

    def test_missing_sides(self):
        sim = point("SR-7", "energy", "kJ", [5.0])
        lab = point("LR-7", "binding", "nM", [6.0])
        missing_lab = compare_metric("energy", sim, None, "complete")
        self.assertEqual(missing_lab.reason_code, REASON_MISSING_LAB_MEASUREMENT)
        missing_sim = compare_metric("binding", None, lab, "complete")
        self.assertEqual(missing_sim.reason_code, REASON_MISSING_SIM_MEASUREMENT)

    def test_unknown_run_state_rejected(self):
        with self.assertRaises(ValueError):
            compare_metric("binding", None, None, "running")


class CompareLinkTests(unittest.TestCase):
    def test_no_measurements_at_all(self):
        verdicts = compare_link("SR-X", "LR-X", "aborted", {})
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0].reason_code, REASON_NO_SHARED_METRIC)
        self.assertEqual(verdicts[0].metric, "(none)")

    def test_link_uses_union_of_metrics(self):
        rows = [
            {"result_ref": "SR-8", "metric": "yield", "value": 0.5, "unit": "percent",
             "uncertainty": 0.02, "replicate_no": 1},
            {"result_ref": "LR-8", "metric": "binding", "value": 6.0, "unit": "nM",
             "uncertainty": 0.1, "replicate_no": 1},
        ]
        points = aggregate_measurements(rows)
        verdicts = compare_link("SR-8", "LR-8", "complete", points)
        codes = {v.metric: v.reason_code for v in verdicts}
        self.assertEqual(codes["yield"], REASON_MISSING_LAB_MEASUREMENT)
        self.assertEqual(codes["binding"], REASON_MISSING_SIM_MEASUREMENT)
        summary = summarize_verdicts(verdicts)
        self.assertEqual(summary["not_comparable"], 2)
        self.assertEqual(DEFAULT_TOLERANCE_Z, 2.0)


class SampleCorrectionTests(unittest.TestCase):
    def test_corrections_append_in_order(self):
        original = "SAMPLE-A"
        corrections = [
            {"correction_id": 2, "field": "sample_id", "old_value": "SAMPLE-B", "new_value": "SAMPLE-C"},
            {"correction_id": 1, "field": "sample_id", "old_value": "SAMPLE-A", "new_value": "SAMPLE-B"},
            {"correction_id": 3, "field": "other", "old_value": "x", "new_value": "y"},
        ]
        self.assertEqual(effective_sample_id(original, corrections), "SAMPLE-C")


if __name__ == "__main__":
    unittest.main()
