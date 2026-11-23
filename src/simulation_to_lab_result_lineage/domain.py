"""验证结论判定的纯函数规则。

核心原则：仿真输出与实测记录不是同一种观测。二者仅在

1. 存在同名指标且单位完全一致（本应用不做隐式单位换算）；
2. 实验批次为 ``complete``（失败、中止记录保留，但不能自动作为验证证据）；
3. 指标在两侧均有测量值；

三个条件同时满足时才可比较，并按扩展不确定度区间判定：

- |仿真均值 − 实测均值| ≤ z·√(u_sim² + u_lab²) → ``validated``（验证）
- 否则 → ``contradicted``（否定）

其余情况一律为 ``not_comparable``（尚不可比较），并给出结构化原因码，
禁止把不可比的数值强行折算后混入结论。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

DEFAULT_TOLERANCE_Z = 2.0

# 原因码（稳定字符串，便于程序检索与报告引用）
REASON_COMPARABLE = "comparable"
REASON_LAB_NOT_COMPLETE = "lab_run_not_complete"
REASON_UNIT_MISMATCH = "unit_mismatch"
REASON_NO_SHARED_METRIC = "no_shared_metric"
REASON_MISSING_SIM_MEASUREMENT = "missing_simulation_measurement"
REASON_MISSING_LAB_MEASUREMENT = "missing_lab_measurement"
REASON_UNCERT_MISSING = "uncertainty_missing"

VALID_RUN_STATES = frozenset({"complete", "failed", "aborted"})


@dataclass(frozen=True)
class MeasurementPoint:
    """单侧（仿真或实验）同一指标的一条或多条重复测量汇总。"""

    result_ref: str
    metric: str
    unit: str
    values: tuple[float, ...]
    uncertainties: tuple[float, ...]
    replicate_numbers: tuple[int, ...]

    @property
    def replicate_count(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float:
        return sum(self.values) / len(self.values)

    @property
    def mean_uncertainty(self) -> float:
        """重复测量的平均报告不确定度；不确定度为 0 时如实返回 0。"""
        return sum(self.uncertainties) / len(self.uncertainties)


@dataclass(frozen=True)
class ComparisonVerdict:
    """一次比较的结构化结论。"""

    metric: str
    verdict: str
    reason_code: str
    reason_detail: str
    sim: MeasurementPoint | None = None
    lab: MeasurementPoint | None = None
    abs_diff: float | None = None
    tolerance: float | None = None
    tolerance_z: float | None = None

    @property
    def is_comparable(self) -> bool:
        return self.verdict in {"validated", "contradicted"}


def aggregate_measurements(rows: Iterable[dict]) -> dict[tuple[str, str], MeasurementPoint]:
    """把测量行按 ``(result_ref, metric)`` 聚合成重复测量汇总。"""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["result_ref"], row["metric"]), []).append(row)
    points: dict[tuple[str, str], MeasurementPoint] = {}
    for key, items in grouped.items():
        items.sort(key=lambda item: int(item["replicate_no"]))
        points[key] = MeasurementPoint(
            result_ref=key[0],
            metric=key[1],
            unit=items[0]["unit"],
            values=tuple(float(item["value"]) for item in items),
            uncertainties=tuple(float(item["uncertainty"]) for item in items),
            replicate_numbers=tuple(int(item["replicate_no"]) for item in items),
        )
    return points


def combined_tolerance(
    sim: MeasurementPoint, lab: MeasurementPoint, z: float = DEFAULT_TOLERANCE_Z
) -> tuple[float, float]:
    """返回 ``(绝对差, 扩展容差)``。

    容差 = z × 仿真与实测平均报告不确定度的平方和开根。
    两侧不确定度均为 0 时容差为 0，任何非零差异即构成否定，避免零容差
    被误当作"必然验证"。
    """
    abs_diff = abs(sim.mean - lab.mean)
    tolerance = z * math.sqrt(sim.mean_uncertainty**2 + lab.mean_uncertainty**2)
    return abs_diff, tolerance


def compare_metric(
    metric: str,
    sim: MeasurementPoint | None,
    lab: MeasurementPoint | None,
    lab_run_state: str,
    tolerance_z: float = DEFAULT_TOLERANCE_Z,
) -> ComparisonVerdict:
    """按领域规则判定单个指标的验证结论。"""
    if lab_run_state not in VALID_RUN_STATES:
        raise ValueError(f"未知实验状态: {lab_run_state!r}")

    if sim is None:
        return ComparisonVerdict(
            metric=metric,
            verdict="not_comparable",
            reason_code=REASON_MISSING_SIM_MEASUREMENT,
            reason_detail=f"指标 {metric} 缺少仿真侧测量，无法比较",
            lab=lab,
        )
    if lab is None:
        return ComparisonVerdict(
            metric=metric,
            verdict="not_comparable",
            reason_code=REASON_MISSING_LAB_MEASUREMENT,
            reason_detail=f"指标 {metric} 缺少实测侧测量，无法比较",
            sim=sim,
        )
    if lab_run_state != "complete":
        return ComparisonVerdict(
            metric=metric,
            verdict="not_comparable",
            reason_code=REASON_LAB_NOT_COMPLETE,
            reason_detail=(
                f"实验批次状态为 {lab_run_state}，记录予以保留但不能作为验证证据；"
                "待补做完整实验后再比较"
            ),
            sim=sim,
            lab=lab,
        )
    if sim.unit != lab.unit:
        return ComparisonVerdict(
            metric=metric,
            verdict="not_comparable",
            reason_code=REASON_UNIT_MISMATCH,
            reason_detail=(
                f"单位不一致：仿真为 {sim.unit}，实测为 {lab.unit}；"
                "应用不做隐式单位换算，须先明确换算依据"
            ),
            sim=sim,
            lab=lab,
        )

    abs_diff, tolerance = combined_tolerance(sim, lab, tolerance_z)
    verdict = "validated" if abs_diff <= tolerance else "contradicted"
    detail = (
        f"|仿真均值 {sim.mean:.6g} − 实测均值 {lab.mean:.6g}| = {abs_diff:.6g}，"
        f"扩展容差（z={tolerance_z:g}）= {tolerance:.6g}"
    )
    return ComparisonVerdict(
        metric=metric,
        verdict=verdict,
        reason_code=REASON_COMPARABLE,
        reason_detail=detail,
        sim=sim,
        lab=lab,
        abs_diff=abs_diff,
        tolerance=tolerance,
        tolerance_z=tolerance_z,
    )


def compare_link(
    sim_ref: str | None,
    lab_ref: str | None,
    lab_run_state: str,
    measurements_by_ref_metric: dict[tuple[str, str], MeasurementPoint],
    candidate_metrics: Sequence[str] | None = None,
    tolerance_z: float = DEFAULT_TOLERANCE_Z,
) -> list[ComparisonVerdict]:
    """判定一条验证链接上的全部候选指标。

    候选指标取两侧测量指标的并集；若两侧都没有任何测量，则产出一条
    ``no_shared_metric`` 的不可比较事实，保证失败/空记录仍在谱系中留痕。
    """
    sim_metrics = sorted(m for (ref, m) in measurements_by_ref_metric if ref == sim_ref)
    lab_metrics = sorted(m for (ref, m) in measurements_by_ref_metric if ref == lab_ref)
    metrics = list(candidate_metrics) if candidate_metrics is not None else sorted(
        set(sim_metrics) | set(lab_metrics)
    )
    if not metrics:
        return [
            ComparisonVerdict(
                metric="(none)",
                verdict="not_comparable",
                reason_code=REASON_NO_SHARED_METRIC,
                reason_detail="仿真与实验两侧均无测量记录，无任何可比较指标",
            )
        ]

    verdicts: list[ComparisonVerdict] = []
    for metric in metrics:
        sim = measurements_by_ref_metric.get((sim_ref, metric)) if sim_ref else None
        lab = measurements_by_ref_metric.get((lab_ref, metric)) if lab_ref else None
        verdicts.append(compare_metric(metric, sim, lab, lab_run_state, tolerance_z))
    return verdicts


def summarize_verdicts(verdicts: Sequence[ComparisonVerdict]) -> dict[str, int]:
    """按结论计数，供报告汇总使用。"""
    summary = {"validated": 0, "contradicted": 0, "not_comparable": 0}
    for verdict in verdicts:
        summary[verdict.verdict] += 1
    return summary


def effective_sample_id(original_sample_id: str, corrections: Sequence[dict]) -> str:
    """依据仅追加的更正台账计算实验当前有效的样品编号。

    原始 ``lab_runs.sample_id`` 永不修改；更正按先后顺序应用。
    """
    sample_id = original_sample_id
    for correction in sorted(corrections, key=lambda item: item["correction_id"]):
        if correction["field"] == "sample_id" and correction["old_value"] == sample_id:
            sample_id = correction["new_value"]
    return sample_id
