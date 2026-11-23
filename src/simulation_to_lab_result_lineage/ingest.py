"""CSV 资料接入与校验。

接入策略：

- 以 ``(文件名, 内容 SHA-256)`` 去重，同一文件重复接入为空操作；
- 原始表仅 INSERT：主键冲突的重复行不覆盖旧值，只记入问题台账；
- 非法取值行被隔离（不写入原始表）并留下可追溯的问题记录；
- 悬空测量引用（不属于任何仿真/实验结果）仍写入原始测量表以保留原始资料，
  但记录 ``orphan_result_ref`` 问题，且因无法关联而不会进入任何比较事实；
- 指向不存在仿真或实验的验证链接不写入，并记录 ``dangling_link`` 问题。
"""

from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SIMULATION_FILE = "simulation_runs.csv"
LAB_FILE = "lab_runs.csv"
MEASUREMENT_FILE = "measurements.csv"
LINK_FILE = "validation_links.csv"

CSV_FILES: tuple[str, ...] = (SIMULATION_FILE, LAB_FILE, MEASUREMENT_FILE, LINK_FILE)

_EXPECTED_COLUMNS: dict[str, list[str]] = {
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

_VALID_RUN_STATES = {"complete", "failed", "aborted"}
_VALID_REVIEW_STATES = {"proposed", "reviewed", "rejected"}


@dataclass(frozen=True)
class IngestIssue:
    issue_code: str
    detail: str
    raw_ref: str


@dataclass(frozen=True)
class BatchReport:
    """单文件接入结果。"""

    file_name: str
    batch_id: str
    row_count: int
    inserted_count: int
    issues: tuple[IngestIssue, ...]
    status: str

    @property
    def issue_codes(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.issue_code] = counts.get(issue.issue_code, 0) + 1
        return counts


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_csv_rows(path: Path) -> tuple[list[dict[str, str]], str]:
    """读取 CSV，返回 ``(行列表, 内容哈希)``；表头不符时抛出 ``ValueError``。"""
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != _EXPECTED_COLUMNS[path.name]:
            raise ValueError(
                f"{path.name} 表头不符：期望 {_EXPECTED_COLUMNS[path.name]}，"
                f"实际 {reader.fieldnames}"
            )
        rows = list(reader)
    return rows, content_hash


def _record_issue(
    connection: sqlite3.Connection,
    batch_id: str,
    file_name: str,
    issue: IngestIssue,
) -> None:
    connection.execute(
        """
        INSERT INTO ingest_issues (batch_id, file_name, issue_code, detail, raw_ref)
        VALUES (?, ?, ?, ?, ?)
        """,
        (batch_id, file_name, issue.issue_code, issue.detail, issue.raw_ref),
    )


def _required_missing(row: dict[str, str]) -> str | None:
    for key, value in row.items():
        if value is None or str(value).strip() == "":
            return key
    return None


def _insert_simulation(
    connection: sqlite3.Connection, row: dict[str, str], batch_id: str
) -> IngestIssue | None:
    del batch_id  # 仿真表不记录来源批次
    missing = _required_missing(row)
    if missing:
        return IngestIssue("missing_field", f"字段 {missing} 为空", row.get("simulation_id", ""))
    try:
        seed = int(row["seed"])
    except ValueError:
        return IngestIssue("invalid_numeric", f"seed 非整数: {row['seed']!r}", row["simulation_id"])
    try:
        connection.execute(
            """
            INSERT INTO simulation_runs (
                simulation_id, model_id, model_version, input_hash, seed,
                environment, started_at, result_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["simulation_id"], row["model_id"], row["model_version"],
                row["input_hash"], seed, row["environment"], row["started_at"],
                row["result_ref"],
            ),
        )
    except sqlite3.IntegrityError:
        return IngestIssue(
            "duplicate_pk", f"仿真 {row['simulation_id']} 已存在，保留首次接入版本", row["simulation_id"]
        )
    return None


def _insert_lab_run(
    connection: sqlite3.Connection, row: dict[str, str], batch_id: str
) -> IngestIssue | None:
    del batch_id  # 实验表不记录来源批次
    missing = _required_missing(row)
    if missing:
        return IngestIssue("missing_field", f"字段 {missing} 为空", row.get("experiment_id", ""))
    if row["run_state"] not in _VALID_RUN_STATES:
        return IngestIssue(
            "invalid_enum", f"run_state 非法: {row['run_state']!r}", row["experiment_id"]
        )
    try:
        connection.execute(
            """
            INSERT INTO lab_runs (
                experiment_id, sample_id, protocol_version, instrument_id,
                started_at, run_state, result_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["experiment_id"], row["sample_id"], row["protocol_version"],
                row["instrument_id"], row["started_at"], row["run_state"],
                row["result_ref"],
            ),
        )
    except sqlite3.IntegrityError:
        return IngestIssue(
            "duplicate_pk", f"实验 {row['experiment_id']} 已存在，保留首次接入版本", row["experiment_id"]
        )
    return None


def _insert_measurement(
    connection: sqlite3.Connection, row: dict[str, str], batch_id: str
) -> IngestIssue | None:
    missing = _required_missing(row)
    if missing:
        return IngestIssue("missing_field", f"字段 {missing} 为空", row.get("result_ref", ""))
    try:
        value = float(row["value"])
        uncertainty = float(row["uncertainty"])
        replicate_no = int(row["replicate_no"])
    except ValueError:
        return IngestIssue(
            "invalid_numeric",
            f"数值字段无法解析: value={row['value']!r}, uncertainty={row['uncertainty']!r}, "
            f"replicate_no={row['replicate_no']!r}",
            row["result_ref"],
        )
    if uncertainty < 0:
        return IngestIssue(
            "invalid_numeric", f"不确定度为负: {uncertainty}", row["result_ref"]
        )
    try:
        connection.execute(
            """
            INSERT INTO measurements (
                result_ref, metric, value, unit, uncertainty, replicate_no, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (row["result_ref"], row["metric"], value, row["unit"], uncertainty,
             replicate_no, batch_id),
        )
    except sqlite3.IntegrityError:
        return IngestIssue(
            "duplicate_pk",
            f"测量重复主键 ({row['result_ref']}, {row['metric']}, {row['replicate_no']})",
            row["result_ref"],
        )
    return None


def _insert_link(
    connection: sqlite3.Connection, row: dict[str, str], batch_id: str
) -> IngestIssue | None:
    del batch_id  # 链接表不记录来源批次
    missing = _required_missing(row)
    if missing:
        return IngestIssue("missing_field", f"字段 {missing} 为空", row.get("link_id", ""))
    if row["review_state"] not in _VALID_REVIEW_STATES:
        return IngestIssue(
            "invalid_enum", f"review_state 非法: {row['review_state']!r}", row["link_id"]
        )
    sim_exists = connection.execute(
        "SELECT 1 FROM simulation_runs WHERE simulation_id = ?", (row["simulation_id"],)
    ).fetchone()
    lab_exists = connection.execute(
        "SELECT 1 FROM lab_runs WHERE experiment_id = ?", (row["experiment_id"],)
    ).fetchone()
    if not sim_exists or not lab_exists:
        return IngestIssue(
            "dangling_link",
            f"链接引用不存在的记录：simulation_exists={bool(sim_exists)}, "
            f"experiment_exists={bool(lab_exists)}",
            row["link_id"],
        )
    try:
        connection.execute(
            """
            INSERT INTO validation_links (
                link_id, simulation_id, experiment_id, condition_map_version,
                review_state, reviewer, decision_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["link_id"], row["simulation_id"], row["experiment_id"],
                row["condition_map_version"], row["review_state"], row["reviewer"],
                row["decision_at"],
            ),
        )
    except sqlite3.IntegrityError:
        return IngestIssue(
            "duplicate_pk", f"链接 {row['link_id']} 已存在，保留首次接入版本", row["link_id"]
        )
    return None


_INSERTERS = {
    SIMULATION_FILE: _insert_simulation,
    LAB_FILE: _insert_lab_run,
    MEASUREMENT_FILE: _insert_measurement,
    LINK_FILE: _insert_link,
}


def _flag_orphan_measurements(
    connection: sqlite3.Connection, batch_id: str
) -> list[IngestIssue]:
    """找出不属于任何仿真/实验结果的测量引用并登记问题（测量行本身保留）。"""
    known_refs = {
        row[0]
        for row in connection.execute("SELECT result_ref FROM simulation_runs")
    } | {
        row[0]
        for row in connection.execute("SELECT result_ref FROM lab_runs")
    }
    issues: list[IngestIssue] = []
    orphan_refs = connection.execute(
        "SELECT DISTINCT result_ref FROM measurements ORDER BY result_ref"
    ).fetchall()
    for (result_ref,) in orphan_refs:
        if result_ref not in known_refs:
            issue = IngestIssue(
                "orphan_result_ref",
                f"测量引用 {result_ref} 不属于任何仿真或实验结果，保留原始行但不参与比较",
                result_ref,
            )
            already = connection.execute(
                "SELECT 1 FROM ingest_issues WHERE issue_code = 'orphan_result_ref' AND raw_ref = ?",
                (result_ref,),
            ).fetchone()
            if not already:
                _record_issue(connection, batch_id, MEASUREMENT_FILE, issue)
                issues.append(issue)
    return issues


def ingest_file(
    connection: sqlite3.Connection,
    path: Path,
    *,
    now: str | None = None,
) -> BatchReport:
    """接入单个 CSV 文件；重复内容自动跳过。"""
    if path.name not in CSV_FILES:
        raise ValueError(f"未知资料文件: {path.name}")
    rows, content_hash = read_csv_rows(path)
    timestamp = now or _utc_now()

    existing = connection.execute(
        "SELECT batch_id, status FROM ingest_batches WHERE file_name = ? AND content_hash = ?",
        (path.name, content_hash),
    ).fetchone()
    if existing:
        return BatchReport(
            file_name=path.name,
            batch_id=existing[0],
            row_count=len(rows),
            inserted_count=0,
            issues=(),
            status="skipped_unchanged",
        )

    batch_id = f"B-{content_hash[:10]}-{path.name.removesuffix('.csv')}"
    inserted = 0
    issues: list[IngestIssue] = []
    inserter = _INSERTERS[path.name]
    for row in rows:
        issue = inserter(connection, row, batch_id)
        if issue is None:
            inserted += 1
        else:
            issues.append(issue)
            _record_issue(connection, batch_id, path.name, issue)

    if path.name == MEASUREMENT_FILE:
        issues.extend(_flag_orphan_measurements(connection, batch_id))

    status = "accepted" if not issues else "accepted_with_issues"
    connection.execute(
        """
        INSERT INTO ingest_batches (
            batch_id, source_dir, file_name, content_hash, row_count,
            inserted_count, status, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id, str(path.parent), path.name, content_hash, len(rows),
            inserted, status, timestamp,
        ),
    )
    connection.commit()
    return BatchReport(
        file_name=path.name,
        batch_id=batch_id,
        row_count=len(rows),
        inserted_count=inserted,
        issues=tuple(issues),
        status=status,
    )


def ingest_directory(
    connection: sqlite3.Connection,
    directory: Path,
    *,
    now: str | None = None,
) -> list[BatchReport]:
    """按依赖顺序接入目录下的全部契约 CSV。"""
    reports: list[BatchReport] = []
    for file_name in CSV_FILES:
        path = directory / file_name
        if path.exists():
            reports.append(ingest_file(connection, path, now=now))
    return reports
