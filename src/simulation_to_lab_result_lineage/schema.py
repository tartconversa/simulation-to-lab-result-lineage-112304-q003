"""SQLite 表结构定义。

设计原则：

1. ``simulation_runs`` / ``lab_runs`` / ``measurements`` / ``validation_links``
   为原始资料表，只允许 INSERT，不允许 UPDATE/DELETE；来源行一旦写入即固化，
   后续修订通过更正表与修订台账表达。
2. ``comparison_facts`` 为比较事实表，同一事实以 ``(fact_uid, version_no)``
   保存多个版本；旧版本不被覆盖，仅迁移到 ``re_review_pending`` 或
   ``superseded`` 状态，任何时点的报告都可按版本重建。
3. ``signoffs`` 记录模型负责人与实验负责人的分别签收，二者互不替代。
"""

from __future__ import annotations

import sqlite3

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS simulation_runs (
        simulation_id TEXT PRIMARY KEY,
        model_id      TEXT NOT NULL,
        model_version TEXT NOT NULL,
        input_hash    TEXT NOT NULL,
        seed          INTEGER NOT NULL,
        environment   TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        result_ref    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lab_runs (
        experiment_id    TEXT PRIMARY KEY,
        sample_id        TEXT NOT NULL,
        protocol_version TEXT NOT NULL,
        instrument_id    TEXT NOT NULL,
        started_at       TEXT NOT NULL,
        run_state        TEXT NOT NULL CHECK (run_state IN ('complete', 'failed', 'aborted')),
        result_ref       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS measurements (
        result_ref   TEXT NOT NULL,
        metric       TEXT NOT NULL,
        value        REAL NOT NULL,
        unit         TEXT NOT NULL,
        uncertainty  REAL NOT NULL CHECK (uncertainty >= 0),
        replicate_no INTEGER NOT NULL,
        batch_id     TEXT NOT NULL,
        PRIMARY KEY (result_ref, metric, replicate_no)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS validation_links (
        link_id               TEXT PRIMARY KEY,
        simulation_id         TEXT NOT NULL,
        experiment_id         TEXT NOT NULL,
        condition_map_version TEXT NOT NULL,
        review_state          TEXT NOT NULL CHECK (review_state IN ('proposed', 'reviewed', 'rejected')),
        reviewer              TEXT NOT NULL,
        decision_at           TEXT NOT NULL
    )
    """,
    # 接入批次：按 文件+内容哈希 去重，保证重复接入为空操作。
    """
    CREATE TABLE IF NOT EXISTS ingest_batches (
        batch_id       TEXT PRIMARY KEY,
        source_dir     TEXT NOT NULL,
        file_name      TEXT NOT NULL,
        content_hash   TEXT NOT NULL,
        row_count      INTEGER NOT NULL,
        inserted_count INTEGER NOT NULL,
        status         TEXT NOT NULL CHECK (status IN ('accepted', 'accepted_with_issues', 'skipped_unchanged')),
        ingested_at    TEXT NOT NULL,
        UNIQUE (file_name, content_hash)
    )
    """,
    # 接入问题台账：孤立引用、重复主键、非法取值等都留痕，可追溯。
    """
    CREATE TABLE IF NOT EXISTS ingest_issues (
        issue_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id   TEXT NOT NULL,
        file_name  TEXT NOT NULL,
        issue_code TEXT NOT NULL,
        detail     TEXT NOT NULL,
        raw_ref    TEXT NOT NULL
    )
    """,
    # 实验记录更正台账：原始 lab_runs 行不变，更正以追加方式表达。
    """
    CREATE TABLE IF NOT EXISTS lab_run_corrections (
        correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        experiment_id TEXT NOT NULL,
        field         TEXT NOT NULL,
        old_value     TEXT NOT NULL,
        new_value     TEXT NOT NULL,
        reason        TEXT NOT NULL,
        corrected_by  TEXT NOT NULL,
        corrected_at  TEXT NOT NULL
    )
    """,
    # 比较事实：同一逻辑事实跨版本追加，旧版本保留全部取值。
    """
    CREATE TABLE IF NOT EXISTS comparison_facts (
        fact_uid            TEXT NOT NULL,
        version_no          INTEGER NOT NULL,
        link_id             TEXT NOT NULL,
        simulation_id       TEXT NOT NULL,
        experiment_id       TEXT NOT NULL,
        metric              TEXT NOT NULL,
        sim_result_ref      TEXT,
        sim_value           REAL,
        sim_unit            TEXT,
        sim_uncertainty     REAL,
        sim_replicate_count INTEGER,
        lab_result_ref      TEXT,
        lab_value           REAL,
        lab_unit            TEXT,
        lab_uncertainty     REAL,
        lab_replicate_count INTEGER,
        abs_diff            REAL,
        tolerance           REAL,
        tolerance_z         REAL,
        lab_run_state       TEXT,
        sample_id           TEXT,
        model_id            TEXT,
        model_version       TEXT,
        protocol_version    TEXT,
        condition_map_version TEXT,
        verdict             TEXT NOT NULL CHECK (verdict IN ('validated', 'contradicted', 'not_comparable')),
        reason_code         TEXT NOT NULL,
        reason_detail       TEXT NOT NULL,
        status              TEXT NOT NULL CHECK (status IN ('active', 're_review_pending', 'superseded')),
        created_at          TEXT NOT NULL,
        superseded_at       TEXT,
        PRIMARY KEY (fact_uid, version_no)
    ) WITHOUT ROWID
    """,
    # 事实修订台账：记录每次创建、待复核、再判定的触发来源。
    """
    CREATE TABLE IF NOT EXISTS fact_revisions (
        revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_uid    TEXT NOT NULL,
        version_no  INTEGER NOT NULL,
        link_id     TEXT NOT NULL,
        metric      TEXT NOT NULL,
        change_type TEXT NOT NULL CHECK (change_type IN ('created', 're_review_pending', 'recomputed', 'sample_corrected')),
        triggered_by TEXT NOT NULL,
        detail      TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    # 双负责人签收：绑定具体事实版本，版本升级后旧签收不自动沿用。
    """
    CREATE TABLE IF NOT EXISTS signoffs (
        signoff_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_uid    TEXT NOT NULL,
        version_no  INTEGER NOT NULL,
        role        TEXT NOT NULL CHECK (role IN ('model_owner', 'experiment_owner')),
        owner       TEXT NOT NULL,
        decision    TEXT NOT NULL CHECK (decision IN ('confirmed', 'rejected')),
        comment     TEXT NOT NULL,
        decided_at  TEXT NOT NULL,
        UNIQUE (fact_uid, version_no, role)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_facts_link ON comparison_facts(link_id)",
    "CREATE INDEX IF NOT EXISTS idx_facts_status ON comparison_facts(status)",
    "CREATE INDEX IF NOT EXISTS idx_revisions_fact ON fact_revisions(fact_uid, version_no)",
    "CREATE INDEX IF NOT EXISTS idx_measurements_ref ON measurements(result_ref)",
)

# 原始资料表的数据库级防改写保护：禁止 UPDATE/DELETE（INSERT 与查询不受影响）。
# 修订只能经更正表与事实版本表表达，形成纵深防御。
_IMMUTABLE_RAW_TABLES = (
    "simulation_runs",
    "lab_runs",
    "measurements",
    "validation_links",
)


def _trigger_statements() -> tuple[str, ...]:
    statements: list[str] = []
    for table in _IMMUTABLE_RAW_TABLES:
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_block_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '原始资料表 {table} 不可更新：请使用更正/版本机制');
            END
            """
        )
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_block_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '原始资料表 {table} 不可删除：原始记录必须保留');
            END
            """
        )
    return tuple(statements)


def initialize(connection: sqlite3.Connection) -> None:
    """创建全部表、索引与防改写触发器（幂等）。"""
    connection.execute("PRAGMA foreign_keys = ON")
    for statement in SCHEMA_STATEMENTS + _trigger_statements():
        connection.execute(statement)
    connection.commit()
