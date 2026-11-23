"""受控合成数据生成器（仅用于演示与测试，不代表任何真实实验）。

生成的夹具刻意覆盖以下场景，使每种验证结论与不可比较原因都可被断言：

1. 完整实验 + 同单位 + 差异落在扩展容差内 → ``validated``
2. 完整实验 + 同单位 + 差异超出扩展容差（含双侧重复测量）→ ``contradicted``
3. 同指标但单位不一致 → ``not_comparable / unit_mismatch``
4. 失败实验（测量保留）→ ``not_comparable / lab_run_not_complete``
5. 实测侧缺该指标 → ``not_comparable / missing_lab_measurement``
6. 仿真侧缺该指标 → ``not_comparable / missing_simulation_measurement``
7. 中止实验且无共同指标 → ``not_comparable / no_shared_metric``
8. 链接审核状态为 rejected 的原始链接（数据平台不据此自动采信）
9. 孤立测量引用、重复测量主键、负不确定度等接入问题样本
10. :func:`write_update_fixture` 生成第二批资料：新增实测重复与新指标，
    触发待复核与再判定，且不覆盖任何原始行
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

SIMULATION_FILE = "simulation_runs.csv"
LAB_FILE = "lab_runs.csv"
MEASUREMENT_FILE = "measurements.csv"
LINK_FILE = "validation_links.csv"

_COLUMNS = {
    SIMULATION_FILE: [
        "simulation_id", "model_id", "model_version", "input_hash", "seed",
        "environment", "started_at", "result_ref",
    ],
    LAB_FILE: [
        "experiment_id", "sample_id", "protocol_version", "instrument_id",
        "started_at", "run_state", "result_ref",
    ],
    MEASUREMENT_FILE: [
        "result_ref", "metric", "value", "unit", "uncertainty", "replicate_no",
    ],
    LINK_FILE: [
        "link_id", "simulation_id", "experiment_id", "condition_map_version",
        "review_state", "reviewer", "decision_at",
    ],
}

# (仿真ID, 模型版本, 种子, 结果引用) —— 模型版本与随机种子逐批留痕。
_SIM_ROWS = [
    ("SIM-3001", "v3", 101, "SR-3001"),
    ("SIM-3002", "v3", 102, "SR-3002"),
    ("SIM-3003", "v4", 103, "SR-3003"),
    ("SIM-3004", "v4", 104, "SR-3004"),
    ("SIM-3005", "v5", 105, "SR-3005"),
    ("SIM-3006", "v5", 106, "SR-3006"),
    ("SIM-3007", "v5", 107, "SR-3007"),
    ("SIM-3008", "v5", 108, "SR-3008"),
]

# (实验ID, 样品, 方案版本, 设备, 状态, 结果引用)
_LAB_ROWS = [
    ("EXP-3001", "SAMPLE-A1", "P-01-v2", "INST-11", "complete", "LR-3001"),
    ("EXP-3002", "SAMPLE-A2", "P-01-v2", "INST-11", "complete", "LR-3002"),
    ("EXP-3003", "SAMPLE-A3", "P-02-v1", "INST-12", "complete", "LR-3003"),
    ("EXP-3004", "SAMPLE-A4", "P-02-v1", "INST-12", "failed", "LR-3004"),
    ("EXP-3005", "SAMPLE-A5", "P-03-v3", "INST-13", "complete", "LR-3005"),
    ("EXP-3006", "SAMPLE-A6", "P-03-v3", "INST-13", "complete", "LR-3006"),
    ("EXP-3007", "SAMPLE-A7", "P-04-v1", "INST-14", "aborted", "LR-3007"),
    ("EXP-3008", "SAMPLE-A8", "P-04-v1", "INST-14", "complete", "LR-3008"),
]

# 测量：(结果引用, 指标, 值, 单位, 不确定度, 重复号)
_MEASUREMENT_ROWS = [
    # 场景1 validated：差异 0.10，容差 2*sqrt(0.05^2+0.05^2)≈0.141
    ("SR-3001", "binding", 1.00, "nM", 0.05, 1),
    ("LR-3001", "binding", 1.10, "nM", 0.05, 1),
    # 场景2 contradicted（双侧重复）：均值差 0.80，容差≈0.141
    ("SR-3002", "binding", 2.00, "nM", 0.05, 1),
    ("SR-3002", "binding", 2.10, "nM", 0.05, 2),
    ("LR-3002", "binding", 2.85, "nM", 0.05, 1),
    ("LR-3002", "binding", 2.95, "nM", 0.05, 2),
    # 场景3 unit_mismatch：同指标 energy，kJ vs J
    ("SR-3003", "energy", 3.00, "kJ", 0.10, 1),
    ("LR-3003", "energy", 3000.0, "J", 50.0, 1),
    # 场景4 failed 实验：测量保留，但不可作为验证证据
    ("SR-3004", "binding", 4.00, "nM", 0.10, 1),
    ("LR-3004", "binding", 4.05, "nM", 0.10, 1),
    # 场景5 实测缺该指标
    ("SR-3005", "energy", 5.00, "kJ", 0.10, 1),
    ("LR-3005", "yield", 0.82, "percent", 0.02, 1),
    # 场景6 仿真缺该指标（实验有 binding）
    ("SR-3006", "yield", 0.50, "percent", 0.02, 1),
    ("LR-3006", "binding", 6.00, "nM", 0.10, 1),
    # 场景7 中止 + 两侧均无任何测量 → no_shared_metric（中止记录本身仍保留在 lab_runs）
    # 场景8 基线仅有一条实测，后续批次补充重复与新指标
    ("LR-3008", "yield", 0.40, "percent", 0.02, 1),
    ("SR-3008", "yield", 0.41, "percent", 0.02, 1),
    # 接入问题样本：孤立引用（无对应仿真/实验）
    ("LR-9999", "binding", 9.99, "nM", 0.10, 1),
    # 接入问题样本：重复主键（同 ref/metric/replicate_no）
    ("SR-3001", "binding", 1.01, "nM", 0.05, 1),
    # 接入问题样本：负不确定度
    ("LR-3005", "yield", 0.80, "percent", -0.01, 2),
]

# (链接ID, 仿真, 实验, 条件映射版本, 审核状态, 审核人, 日期)
_LINK_ROWS = [
    ("L-3001", "SIM-3001", "EXP-3001", "CM-7", "reviewed", "REV-11", "2026-10-11"),
    ("L-3002", "SIM-3002", "EXP-3002", "CM-7", "reviewed", "REV-12", "2026-10-12"),
    ("L-3003", "SIM-3003", "EXP-3003", "CM-7", "reviewed", "REV-13", "2026-10-13"),
    ("L-3004", "SIM-3004", "EXP-3004", "CM-7", "reviewed", "REV-14", "2026-10-14"),
    ("L-3005", "SIM-3005", "EXP-3005", "CM-8", "reviewed", "REV-15", "2026-10-15"),
    ("L-3006", "SIM-3006", "EXP-3006", "CM-8", "reviewed", "REV-16", "2026-10-16"),
    ("L-3007", "SIM-3007", "EXP-3007", "CM-8", "proposed", "REV-17", "2026-10-17"),
    ("L-3008", "SIM-3008", "EXP-3008", "CM-8", "rejected", "REV-18", "2026-10-18"),
]

# 第二批：为 EXP-3008 增加 yield 重复、补充 stability 新指标（两侧）。
_UPDATE_MEASUREMENT_ROWS = [
    ("LR-3008", "yield", 0.42, "percent", 0.02, 2),
    ("LR-3008", "yield", 0.38, "percent", 0.02, 3),
    ("LR-3008", "stability", 12.0, "hours", 0.30, 1),
    ("SR-3008", "stability", 12.6, "hours", 0.30, 1),
]


def _write_csv(path: Path, columns: list[str], rows: list[tuple]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)


def write_fixture(directory: str | Path, *, seed: int = 20261124) -> Path:
    """写入第一批合成资料，返回目录路径。"""
    rng = random.Random(seed)  # 保留确定性生成能力；当前夹具为显式取值
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)

    sim_rows = [
        (
            sim_id, f"MODEL-{(i % 3) + 1}", version,
            f"HASH-{sim_id.lower()}", seed_value, f"ENV-{(i % 4)}",
            f"2026-10-0{(i % 9) + 1}T01:00:00Z", result_ref,
        )
        for i, (sim_id, version, seed_value, result_ref) in enumerate(_SIM_ROWS)
    ]
    lab_rows = [
        (
            exp_id, sample_id, protocol, instrument,
            f"2026-10-{10 + i:02d}T08:00:00Z", state, result_ref,
        )
        for i, (exp_id, sample_id, protocol, instrument, state, result_ref) in enumerate(_LAB_ROWS)
    ]
    _write_csv(base / SIMULATION_FILE, _COLUMNS[SIMULATION_FILE], sim_rows)
    _write_csv(base / LAB_FILE, _COLUMNS[LAB_FILE], lab_rows)
    _write_csv(base / MEASUREMENT_FILE, _COLUMNS[MEASUREMENT_FILE], _MEASUREMENT_ROWS)
    _write_csv(base / LINK_FILE, _COLUMNS[LINK_FILE], _LINK_ROWS)
    rng.random()  # 保持 rng 被使用，便于未来扩展随机批次
    return base


def write_update_fixture(directory: str | Path) -> Path:
    """写入第二批测量资料（仅 measurements.csv），用于演示增量接入与待复核。"""
    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    _write_csv(
        base / MEASUREMENT_FILE,
        _COLUMNS[MEASUREMENT_FILE],
        _UPDATE_MEASUREMENT_ROWS,
    )
    return base


def fixture_scenario_index() -> dict[str, dict[str, str]]:
    """返回场景说明，供文档与测试引用。"""
    return {
        "L-3001": {"scenario": "验证", "expect": "validated", "metric": "binding"},
        "L-3002": {"scenario": "否定（双侧重复）", "expect": "contradicted", "metric": "binding"},
        "L-3003": {"scenario": "单位不一致", "expect": "not_comparable:unit_mismatch", "metric": "energy"},
        "L-3004": {"scenario": "失败实验保留", "expect": "not_comparable:lab_run_not_complete", "metric": "binding"},
        "L-3005": {"scenario": "实测缺指标", "expect": "not_comparable:missing_lab_measurement", "metric": "energy"},
        "L-3006": {"scenario": "仿真缺指标", "expect": "not_comparable:missing_simulation_measurement", "metric": "binding"},
        "L-3007": {"scenario": "中止且两侧均无测量", "expect": "not_comparable:no_shared_metric", "metric": "(none)"},
        "L-3008": {"scenario": "增量批次触发再判定", "expect": "validated", "metric": "yield/stability"},
    }
