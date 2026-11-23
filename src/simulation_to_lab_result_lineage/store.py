"""面向用例的应用服务层。

职责边界：

- :func:`rebuild_facts` 依据当前原始资料为全部验证链接建立当前有效版本的
  比较事实；对已经存在的事实不重复建版本。
- :func:`flag_affected_for_rereview` 在新增实测或样品关联更正后，把受影响
  的活动事实置为 ``re_review_pending``（旧值原样保留），
  :func:`recompute_pending` 再依据新资料追加新版本。
- :func:`signoff` 记录模型/实验负责人对**具体事实版本**的签收；版本升级后
  旧签收不自动沿用。
- :func:`build_report` 支持 ``as_of`` 时间点重建：按当时已存在的事实版本
  与签收记录还原历史报告，不被后续修订污染。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .domain import (
    DEFAULT_TOLERANCE_Z,
    MeasurementPoint,
    aggregate_measurements,
    compare_link,
    effective_sample_id,
)
from .schema import initialize


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）应用数据库并初始化表结构。"""
    path = Path(path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    initialize(connection)
    return connection


def fact_uid_for(link_id: str, metric: str) -> str:
    """由链接与指标生成稳定事实标识，跨重建保持同一谱系。"""
    digest = hashlib.sha256(f"{link_id}|{metric}".encode("utf-8")).hexdigest()[:12]
    safe_metric = metric if metric != "(none)" else "none"
    return f"F-{digest}-{safe_metric}"


def _load_measurement_points(connection: sqlite3.Connection) -> dict[tuple[str, str], MeasurementPoint]:
    rows = connection.execute(
        """
        SELECT result_ref, metric, value, unit, uncertainty, replicate_no
        FROM measurements
        """
    ).fetchall()
    return aggregate_measurements([dict(row) for row in rows])


def _effective_sample_map(connection: sqlite3.Connection) -> dict[str, str]:
    corrections = connection.execute(
        "SELECT correction_id, experiment_id, field, old_value, new_value FROM lab_run_corrections"
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in corrections:
        grouped.setdefault(row["experiment_id"], []).append(dict(row))
    mapping: dict[str, str] = {}
    for row in connection.execute(
        "SELECT experiment_id, sample_id FROM lab_runs"
    ).fetchall():
        mapping[row["experiment_id"]] = effective_sample_id(
            row["sample_id"], grouped.get(row["experiment_id"], [])
        )
    return mapping


def _next_version_no(connection: sqlite3.Connection, fact_uid: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS n FROM comparison_facts WHERE fact_uid = ?",
        (fact_uid,),
    ).fetchone()
    return int(row["n"]) + 1


def _add_revision(
    connection: sqlite3.Connection,
    fact_uid: str,
    version_no: int,
    link_id: str,
    metric: str,
    change_type: str,
    triggered_by: str,
    detail: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO fact_revisions (
            fact_uid, version_no, link_id, metric, change_type,
            triggered_by, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (fact_uid, version_no, link_id, metric, change_type, triggered_by, detail, now),
    )


def _persist_verdict(
    connection: sqlite3.Connection,
    link: sqlite3.Row,
    sim_row: sqlite3.Row,
    lab_row: sqlite3.Row,
    sample_id: str,
    verdict,
    now: str,
    *,
    change_type: str = "created",
    triggered_by: str = "rebuild_facts",
    detail: str | None = None,
) -> str:
    """为一个结论追加事实版本，返回事实标识。"""
    fact_uid = fact_uid_for(link["link_id"], verdict.metric)
    version_no = _next_version_no(connection, fact_uid)
    connection.execute(
        """
        INSERT INTO comparison_facts (
            fact_uid, version_no, link_id, simulation_id, experiment_id, metric,
            sim_result_ref, sim_value, sim_unit, sim_uncertainty, sim_replicate_count,
            lab_result_ref, lab_value, lab_unit, lab_uncertainty, lab_replicate_count,
            abs_diff, tolerance, tolerance_z, lab_run_state, sample_id,
            model_id, model_version, protocol_version, condition_map_version,
            verdict, reason_code, reason_detail, status, created_at, superseded_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, 'active', ?, NULL
        )
        """,
        (
            fact_uid, version_no, link["link_id"], link["simulation_id"],
            link["experiment_id"], verdict.metric,
            verdict.sim.result_ref if verdict.sim else sim_row["result_ref"] if sim_row else None,
            verdict.sim.mean if verdict.sim else None,
            verdict.sim.unit if verdict.sim else None,
            verdict.sim.mean_uncertainty if verdict.sim else None,
            verdict.sim.replicate_count if verdict.sim else None,
            verdict.lab.result_ref if verdict.lab else lab_row["result_ref"] if lab_row else None,
            verdict.lab.mean if verdict.lab else None,
            verdict.lab.unit if verdict.lab else None,
            verdict.lab.mean_uncertainty if verdict.lab else None,
            verdict.lab.replicate_count if verdict.lab else None,
            verdict.abs_diff, verdict.tolerance, verdict.tolerance_z,
            lab_row["run_state"] if lab_row else None,
            sample_id,
            sim_row["model_id"] if sim_row else None,
            sim_row["model_version"] if sim_row else None,
            lab_row["protocol_version"] if lab_row else None,
            link["condition_map_version"],
            verdict.verdict, verdict.reason_code, verdict.reason_detail, now,
        ),
    )
    _add_revision(
        connection, fact_uid, version_no, link["link_id"], verdict.metric,
        change_type, triggered_by, detail or verdict.reason_detail, now,
    )
    return fact_uid


def rebuild_facts(
    connection: sqlite3.Connection,
    *,
    tolerance_z: float = DEFAULT_TOLERANCE_Z,
    now: str | None = None,
) -> dict[str, int]:
    """为尚无事实的验证链接建立初始比较事实（幂等，不覆盖既有版本）。"""
    timestamp = now or utc_now()
    points = _load_measurement_points(connection)
    sample_map = _effective_sample_map(connection)
    created = 0
    skipped = 0
    links = connection.execute("SELECT * FROM validation_links ORDER BY link_id").fetchall()
    for link in links:
        existing = connection.execute(
            "SELECT 1 FROM comparison_facts WHERE link_id = ? LIMIT 1", (link["link_id"],)
        ).fetchone()
        if existing:
            skipped += 1
            continue
        sim_row = connection.execute(
            "SELECT * FROM simulation_runs WHERE simulation_id = ?", (link["simulation_id"],)
        ).fetchone()
        lab_row = connection.execute(
            "SELECT * FROM lab_runs WHERE experiment_id = ?", (link["experiment_id"],)
        ).fetchone()
        verdicts = compare_link(
            sim_row["result_ref"] if sim_row else None,
            lab_row["result_ref"] if lab_row else None,
            lab_row["run_state"] if lab_row else "aborted",
            points,
            tolerance_z=tolerance_z,
        )
        for verdict in verdicts:
            _persist_verdict(
                connection, link, sim_row, lab_row,
                sample_map.get(link["experiment_id"], ""), verdict, timestamp,
            )
            created += 1
    connection.commit()
    return {"created": created, "skipped_links": skipped, "links_total": len(links)}


def _active_facts_for_links(
    connection: sqlite3.Connection, link_ids: Iterable[str]
) -> list[sqlite3.Row]:
    ids = tuple(link_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return connection.execute(
        f"""
        SELECT * FROM comparison_facts
        WHERE status = 'active' AND link_id IN ({placeholders})
        """,
        ids,
    ).fetchall()


def flag_affected_for_rereview(
    connection: sqlite3.Connection,
    link_ids: Sequence[str],
    *,
    triggered_by: str,
    detail: str,
    now: str | None = None,
) -> int:
    """把指定链接的活动事实置为待复核；原始取值与结论原样保留。"""
    timestamp = now or utc_now()
    flagged = 0
    for fact in _active_facts_for_links(connection, link_ids):
        connection.execute(
            "UPDATE comparison_facts SET status = 're_review_pending' WHERE fact_uid = ? AND version_no = ?",
            (fact["fact_uid"], fact["version_no"]),
        )
        _add_revision(
            connection, fact["fact_uid"], fact["version_no"], fact["link_id"],
            fact["metric"], "re_review_pending", triggered_by, detail, timestamp,
        )
        flagged += 1
    connection.commit()
    return flagged


def links_for_experiments(connection: sqlite3.Connection, experiment_ids: Iterable[str]) -> list[str]:
    ids = tuple(experiment_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return [
        row[0]
        for row in connection.execute(
            f"SELECT DISTINCT link_id FROM validation_links WHERE experiment_id IN ({placeholders}) ORDER BY link_id",
            ids,
        )
    ]


def links_for_measurements(connection: sqlite3.Connection, result_refs: Iterable[str]) -> list[str]:
    """依据测量引用反查受影响的验证链接。"""
    refs = tuple(result_refs)
    if not refs:
        return []
    placeholders = ",".join("?" for _ in refs)
    rows = connection.execute(
        f"""
        SELECT DISTINCT vl.link_id
        FROM validation_links vl
        JOIN simulation_runs sr ON sr.simulation_id = vl.simulation_id
        JOIN lab_runs lr ON lr.experiment_id = vl.experiment_id
        WHERE sr.result_ref IN ({placeholders}) OR lr.result_ref IN ({placeholders})
        ORDER BY vl.link_id
        """,
        refs + refs,
    ).fetchall()
    return [row[0] for row in rows]


def measurement_refs_for_batch(
    connection: sqlite3.Connection, batch_id: str
) -> list[str]:
    """返回某接入批次实际写入测量行涉及的全部结果引用。

    既覆盖首次出现的引用，也覆盖老引用上新增的重复测量，避免增量批次
    只按"新引用"反查而漏掉同一引用上的新增重复。
    """
    return [
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT result_ref FROM measurements
            WHERE batch_id = ? ORDER BY result_ref
            """,
            (batch_id,),
        ).fetchall()
    ]


def apply_sample_correction(
    connection: sqlite3.Connection,
    experiment_id: str,
    new_sample_id: str,
    *,
    reason: str,
    corrected_by: str,
    now: str | None = None,
) -> dict[str, object]:
    """追加样品关联更正，并把相关验证结论置为待复核。

    原始 ``lab_runs`` 行不修改；更正只写入 ``lab_run_corrections``。
    """
    timestamp = now or utc_now()
    lab_row = connection.execute(
        "SELECT * FROM lab_runs WHERE experiment_id = ?", (experiment_id,)
    ).fetchone()
    if lab_row is None:
        raise KeyError(f"实验不存在: {experiment_id}")
    current_map = _effective_sample_map(connection)
    old_sample = current_map.get(experiment_id, lab_row["sample_id"])
    if new_sample_id == old_sample:
        raise ValueError("新样品编号与当前有效编号相同，无需更正")
    cur = connection.execute(
        """
        INSERT INTO lab_run_corrections (
            experiment_id, field, old_value, new_value, reason, corrected_by, corrected_at
        ) VALUES (?, 'sample_id', ?, ?, ?, ?, ?)
        """,
        (experiment_id, old_sample, new_sample_id, reason, corrected_by, timestamp),
    )
    correction_id = cur.lastrowid
    affected = links_for_experiments(connection, [experiment_id])
    flagged = flag_affected_for_rereview(
        connection, affected,
        triggered_by=f"sample_correction#{correction_id}",
        detail=f"实验 {experiment_id} 样品关联 {old_sample} → {new_sample_id}：{reason}",
        now=timestamp,
    )
    connection.commit()
    return {
        "correction_id": correction_id,
        "experiment_id": experiment_id,
        "old_sample_id": old_sample,
        "new_sample_id": new_sample_id,
        "affected_links": affected,
        "flagged_facts": flagged,
    }


def recompute_pending(
    connection: sqlite3.Connection,
    *,
    tolerance_z: float = DEFAULT_TOLERANCE_Z,
    now: str | None = None,
) -> dict[str, int]:
    """对全部待复核事实按当前资料重新判定，并追加新版本。

    每个受影响链接处理一次：该链接上处于待复核的旧版本全部置为
    ``superseded``，按当前资料生成的事实作为新版本激活。
    """
    timestamp = now or utc_now()
    points = _load_measurement_points(connection)
    sample_map = _effective_sample_map(connection)
    pending_rows = connection.execute(
        """
        SELECT DISTINCT link_id FROM comparison_facts
        WHERE status = 're_review_pending'
        ORDER BY link_id
        """
    ).fetchall()
    recomputed = 0
    for item in pending_rows:
        link = connection.execute(
            "SELECT * FROM validation_links WHERE link_id = ?", (item["link_id"],)
        ).fetchone()
        if link is None:
            continue
        sim_row = connection.execute(
            "SELECT * FROM simulation_runs WHERE simulation_id = ?", (link["simulation_id"],)
        ).fetchone()
        lab_row = connection.execute(
            "SELECT * FROM lab_runs WHERE experiment_id = ?", (link["experiment_id"],)
        ).fetchone()
        verdicts = compare_link(
            sim_row["result_ref"] if sim_row else None,
            lab_row["result_ref"] if lab_row else None,
            lab_row["run_state"] if lab_row else "aborted",
            points,
            tolerance_z=tolerance_z,
        )
        for verdict in verdicts:
            fact_uid = fact_uid_for(link["link_id"], verdict.metric)
            old_versions = connection.execute(
                """
                SELECT version_no FROM comparison_facts
                WHERE fact_uid = ? AND status = 're_review_pending'
                ORDER BY version_no
                """,
                (fact_uid,),
            ).fetchall()
            _persist_verdict(
                connection, link, sim_row, lab_row,
                sample_map.get(link["experiment_id"], ""), verdict, timestamp,
                change_type="recomputed",
                triggered_by="recompute_pending",
                detail="依据最新实测/样品资料重新判定并追加版本",
            )
            for old in old_versions:
                connection.execute(
                    """
                    UPDATE comparison_facts
                    SET status = 'superseded', superseded_at = ?
                    WHERE fact_uid = ? AND version_no = ?
                    """,
                    (timestamp, fact_uid, old["version_no"]),
                )
            recomputed += 1
    connection.commit()
    return {"recomputed_facts": recomputed, "pending_links": len(pending_rows)}


def signoff(
    connection: sqlite3.Connection,
    fact_uid: str,
    version_no: int,
    role: str,
    owner: str,
    decision: str,
    comment: str,
    *,
    now: str | None = None,
) -> dict[str, object]:
    """记录负责人签收。签收绑定事实版本，结论为不可比较时允许备注但会提示。"""
    timestamp = now or utc_now()
    if role not in {"model_owner", "experiment_owner"}:
        raise ValueError("role 必须是 model_owner 或 experiment_owner")
    if decision not in {"confirmed", "rejected"}:
        raise ValueError("decision 必须是 confirmed 或 rejected")
    fact = connection.execute(
        "SELECT * FROM comparison_facts WHERE fact_uid = ? AND version_no = ?",
        (fact_uid, version_no),
    ).fetchone()
    if fact is None:
        raise KeyError(f"事实版本不存在: {fact_uid} v{version_no}")
    connection.execute(
        """
        INSERT INTO signoffs (fact_uid, version_no, role, owner, decision, comment, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (fact_uid, version_no, role) DO UPDATE SET
            owner = excluded.owner,
            decision = excluded.decision,
            comment = excluded.comment,
            decided_at = excluded.decided_at
        """,
        (fact_uid, version_no, role, owner, decision, comment, timestamp),
    )
    connection.commit()
    return {
        "fact_uid": fact_uid,
        "version_no": version_no,
        "role": role,
        "owner": owner,
        "decision": decision,
        "fact_status": fact["status"],
        "verdict": fact["verdict"],
    }


def list_facts(
    connection: sqlite3.Connection,
    *,
    verdict: str | None = None,
    status: str | None = None,
    link_id: str | None = None,
    active_only: bool = True,
) -> list[dict]:
    """查询比较事实及其签收状态。"""
    clauses: list[str] = []
    params: list[object] = []
    if active_only:
        clauses.append("f.status = 'active'")
    elif status:
        clauses.append("f.status = ?")
        params.append(status)
    if verdict:
        clauses.append("f.verdict = ?")
        params.append(verdict)
    if link_id:
        clauses.append("f.link_id = ?")
        params.append(link_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = connection.execute(
        f"""
        SELECT f.*,
               (SELECT GROUP_CONCAT(role || ':' || owner || '=' || decision, '; ')
                  FROM signoffs s WHERE s.fact_uid = f.fact_uid AND s.version_no = f.version_no
               ) AS signoff_summary
        FROM comparison_facts f
        {where}
        ORDER BY f.link_id, f.metric, f.version_no
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def get_fact_versions(connection: sqlite3.Connection, fact_uid: str) -> list[dict]:
    """返回一个事实的全部版本及修订记录。"""
    versions = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM comparison_facts WHERE fact_uid = ? ORDER BY version_no",
            (fact_uid,),
        ).fetchall()
    ]
    for version in versions:
        version["revisions"] = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM fact_revisions WHERE fact_uid = ? AND version_no = ? ORDER BY revision_id",
                (fact_uid, version["version_no"]),
            ).fetchall()
        ]
        version["signoffs"] = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM signoffs WHERE fact_uid = ? AND version_no = ? ORDER BY role",
                (fact_uid, version["version_no"]),
            ).fetchall()
        ]
    return versions


def build_report(
    connection: sqlite3.Connection,
    *,
    as_of: str | None = None,
    tolerance_z: float | None = None,
) -> dict[str, object]:
    """生成验证报告；``as_of`` 给出 ISO 时间戳时按该时点重建历史报告。

    历史重建只采用 ``created_at <= as_of`` 的事实版本与
    ``decided_at <= as_of`` 的签收，后续修订不会污染旧报告。
    """
    fact_clause = ""
    signoff_clause = ""
    params: list[object] = []
    if as_of:
        fact_clause = "WHERE created_at <= ?"
        signoff_clause = "AND decided_at <= ?"
        params.append(as_of)

    rows = connection.execute(
        f"SELECT * FROM comparison_facts {fact_clause} ORDER BY link_id, metric, version_no",
        params,
    ).fetchall()
    # 每个事实仅取 as_of 时点上的最新版本
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest[row["fact_uid"]] = row

    items: list[dict] = []
    counts = {"validated": 0, "contradicted": 0, "not_comparable": 0}
    pending = 0
    for fact in latest.values():
        signoff_params = [fact["fact_uid"], fact["version_no"]]
        if as_of:
            signoff_params.append(as_of)
        signoffs = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT * FROM signoffs
                WHERE fact_uid = ? AND version_no = ? {signoff_clause}
                ORDER BY role
                """,
                signoff_params,
            ).fetchall()
        ]
        item = dict(fact)
        item["signoffs"] = signoffs
        item["dual_confirmed"] = (
            {s["role"] for s in signoffs if s["decision"] == "confirmed"}
            == {"model_owner", "experiment_owner"}
        )
        items.append(item)
        counts[fact["verdict"]] += 1
        if fact["status"] == "re_review_pending":
            pending += 1

    reason_counts: dict[str, int] = {}
    for item in items:
        reason_counts[item["reason_code"]] = reason_counts.get(item["reason_code"], 0) + 1

    return {
        "generated_at": utc_now(),
        "as_of": as_of,
        "tolerance_z": tolerance_z,
        "fact_count": len(items),
        "verdict_counts": counts,
        "re_review_pending": pending,
        "dual_confirmed_count": sum(1 for item in items if item["dual_confirmed"]),
        "reason_code_counts": reason_counts,
        "facts": items,
    }


def list_ingest_issues(connection: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM ingest_issues ORDER BY issue_id"
        ).fetchall()
    ]


def list_batches(connection: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM ingest_batches ORDER BY ingested_at, file_name"
        ).fetchall()
    ]


def list_corrections(connection: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM lab_run_corrections ORDER BY correction_id"
        ).fetchall()
    ]
