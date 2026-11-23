"""命令行入口。

示例：

    python -m simulation_to_lab_result_lineage ingest --data-dir ./data --db ./work.duck.sqlite
    python -m simulation_to_lab_result_lineage build-facts --db ./work.duck.sqlite
    python -m simulation_to_lab_result_lineage query --verdict validated
    python -m simulation_to_lab_result_lineage refresh --data-dir ./data-update --measurements-only
    python -m simulation_to_lab_result_lineage correct-sample --experiment EXP-3002 --sample SAMPLE-B2 ...
    python -m simulation_to_lab_result_lineage report --as-of 2026-10-20T00:00:00Z
    python -m simulation_to_lab_result_lineage make-fixture --out ./demo/fixture-v1

所有命令默认输出中文表格；加 ``--json`` 输出机器可读结果。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .domain import DEFAULT_TOLERANCE_Z
from .ingest import MEASUREMENT_FILE, ingest_directory, ingest_file
from .store import (
    apply_sample_correction,
    build_report,
    fact_uid_for,
    flag_affected_for_rereview,
    links_for_measurements,
    list_batches,
    list_corrections,
    list_facts,
    list_ingest_issues,
    measurement_refs_for_batch,
    open_db,
    recompute_pending,
    rebuild_facts,
    signoff,
    get_fact_versions,
)
from .synthetic import write_fixture, write_update_fixture


def _emit(payload: Any, as_json: bool, render: Callable[[Any], str]) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(payload))


def _render_batch_reports(reports: list) -> str:
    lines = ["批次 | 文件 | 行数 | 写入 | 状态 | 问题计数"]
    for report in reports:
        lines.append(
            f"{report.batch_id} | {report.file_name} | {report.row_count} | "
            f"{report.inserted_count} | {report.status} | {dict(report.issue_codes) or '无'}"
        )
    return "\n".join(lines)


def _render_issues(issues: list[dict]) -> str:
    if not issues:
        return "无接入问题。"
    lines = ["问题ID | 批次 | 文件 | 原因码 | 原始引用 | 说明"]
    for item in issues:
        lines.append(
            f"{item['issue_id']} | {item['batch_id']} | {item['file_name']} | "
            f"{item['issue_code']} | {item['raw_ref']} | {item['detail']}"
        )
    return "\n".join(lines)


def _render_facts(facts: list[dict]) -> str:
    if not facts:
        return "无符合条件的比较事实。"
    lines = [
        "事实 | 版本 | 链接 | 指标 | 结论 | 原因码 | 状态 | 仿真值/单位 | 实测值/单位 | |差|/容差 | 样品 | 签收"
    ]
    for fact in facts:
        sim = "—" if fact["sim_value"] is None else f"{fact['sim_value']:g}/{fact['sim_unit']}"
        lab = "—" if fact["lab_value"] is None else f"{fact['lab_value']:g}/{fact['lab_unit']}"
        tol = (
            "—"
            if fact["abs_diff"] is None
            else f"{fact['abs_diff']:g}/{fact['tolerance']:g}"
        )
        lines.append(
            f"{fact['fact_uid']} | v{fact['version_no']} | {fact['link_id']} | {fact['metric']} | "
            f"{fact['verdict']} | {fact['reason_code']} | {fact['status']} | {sim} | {lab} | "
            f"{tol} | {fact['sample_id']} | {fact.get('signoff_summary') or '未签收'}"
        )
    return "\n".join(lines)


def _render_report(report: dict) -> str:
    lines = [
        f"验证报告（生成于 {report['generated_at']}"
        + (f"，历史重建时点 {report['as_of']}" if report["as_of"] else "，当前视图")
        + "）",
        f"事实总数：{report['fact_count']}；"
        f"验证 {report['verdict_counts']['validated']} / "
        f"否定 {report['verdict_counts']['contradicted']} / "
        f"不可比较 {report['verdict_counts']['not_comparable']}",
        f"待复核：{report['re_review_pending']}；双负责人均确认：{report['dual_confirmed_count']}",
        f"原因码分布：{report['reason_code_counts']}",
        "",
        _render_facts(report["facts"]),
    ]
    return "\n".join(lines)


def _render_versions(versions: list[dict]) -> str:
    lines: list[str] = []
    for version in versions:
        lines.append(
            f"{version['fact_uid']} v{version['version_no']} [{version['status']}] "
            f"{version['verdict']}/{version['reason_code']} 建立于 {version['created_at']}"
        )
        lines.append(f"  取值：仿真 {version['sim_value']} {version['sim_unit']} vs "
                     f"实测 {version['lab_value']} {version['lab_unit']}（{version['reason_detail']}）")
        for revision in version["revisions"]:
            lines.append(
                f"  修订：{revision['change_type']} by {revision['triggered_by']} "
                f"at {revision['created_at']} — {revision['detail']}"
            )
        for sign in version["signoffs"]:
            lines.append(
                f"  签收：{sign['role']} {sign['owner']} → {sign['decision']} "
                f"at {sign['decided_at']}（{sign['comment']}）"
            )
    return "\n".join(lines) or "无版本记录。"


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default="lineage.sqlite", help="应用数据库路径（默认 lineage.sqlite）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simulation_to_lab_result_lineage",
        description="仿真批次与实验实测结果的谱系连接、验证关系与差异证据应用",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="接入一个资料目录下的全部契约 CSV")
    _add_db_arg(p_ingest)
    p_ingest.add_argument("--data-dir", required=True)

    p_build = sub.add_parser("build-facts", help="依据已接入资料建立比较事实（幂等）")
    _add_db_arg(p_build)
    p_build.add_argument("--tolerance-z", type=float, default=DEFAULT_TOLERANCE_Z)
    p_build.add_argument("--now", help="覆盖事实建立时间戳（ISO 8601），便于确定性演示与测试")

    p_refresh = sub.add_parser(
        "refresh", help="增量接入资料；受影响的活动事实进入待复核，随后按新资料重算"
    )
    _add_db_arg(p_refresh)
    p_refresh.add_argument("--data-dir", required=True)
    p_refresh.add_argument(
        "--measurements-only", action="store_true",
        help="目录仅含 measurements.csv 时使用；按新测量反查受影响链接",
    )
    p_refresh.add_argument("--no-recompute", action="store_true", help="只置待复核，不立即重算")
    p_refresh.add_argument("--now", help="覆盖修订与重算时间戳（ISO 8601）")

    p_correct = sub.add_parser("correct-sample", help="追加样品关联更正（不修改原始实验行）")
    _add_db_arg(p_correct)
    p_correct.add_argument("--experiment", required=True)
    p_correct.add_argument("--sample", required=True, help="新的有效样品编号")
    p_correct.add_argument("--reason", required=True)
    p_correct.add_argument("--by", required=True, help="更正操作人/负责人")
    p_correct.add_argument("--now", help="覆盖更正时间戳（ISO 8601）")

    p_recompute = sub.add_parser("recompute-pending", help="对全部待复核事实按当前资料重算并追加版本")
    _add_db_arg(p_recompute)
    p_recompute.add_argument("--tolerance-z", type=float, default=DEFAULT_TOLERANCE_Z)
    p_recompute.add_argument("--now", help="覆盖重算时间戳（ISO 8601）")

    p_sign = sub.add_parser("signoff", help="模型或实验负责人对具体事实版本签收")
    _add_db_arg(p_sign)
    p_sign.add_argument("--link", required=True)
    p_sign.add_argument("--metric", required=True)
    p_sign.add_argument("--version", type=int, required=True)
    p_sign.add_argument("--role", required=True, choices=["model_owner", "experiment_owner"])
    p_sign.add_argument("--owner", required=True)
    p_sign.add_argument("--decision", required=True, choices=["confirmed", "rejected"])
    p_sign.add_argument("--comment", default="")

    p_query = sub.add_parser("query", help="查询比较事实与证据来源")
    _add_db_arg(p_query)
    p_query.add_argument("--verdict", choices=["validated", "contradicted", "not_comparable"])
    p_query.add_argument("--status", choices=["active", "re_review_pending", "superseded"])
    p_query.add_argument("--link")
    p_query.add_argument("--all-versions", action="store_true", help="包含历史版本（默认仅当前活动版本）")

    p_versions = sub.add_parser("versions", help="查看单个事实的全部版本、修订与签收")
    _add_db_arg(p_versions)
    p_versions.add_argument("--link", required=True)
    p_versions.add_argument("--metric", required=True)

    p_report = sub.add_parser("report", help="输出验证报告，支持按历史时点重建")
    _add_db_arg(p_report)
    p_report.add_argument("--as-of", help="ISO 时间戳，仅采用该时点之前的事实与签收")

    p_issues = sub.add_parser("issues", help="查看接入问题台账")
    _add_db_arg(p_issues)

    p_batches = sub.add_parser("batches", help="查看接入批次与去重情况")
    _add_db_arg(p_batches)

    p_corrections = sub.add_parser("corrections", help="查看样品关联更正台账")
    _add_db_arg(p_corrections)

    p_fixture = sub.add_parser("make-fixture", help="生成受控合成资料夹具")
    p_fixture.add_argument("--out", required=True)
    p_fixture.add_argument("--update", action="store_true", help="生成第二批增量测量资料")

    for item in (
        p_ingest, p_build, p_refresh, p_correct, p_recompute, p_sign, p_query,
        p_versions, p_report, p_issues, p_batches, p_corrections,
    ):
        item.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "make-fixture":
        target = Path(args.out)
        if args.update:
            write_update_fixture(target)
            print(f"已写入第二批测量资料：{target / MEASUREMENT_FILE}")
        else:
            write_fixture(target)
            print(f"已写入第一批合成资料：{target}")
        return 0

    connection = open_db(args.db)
    try:
        if args.command == "ingest":
            reports = ingest_directory(connection, Path(args.data_dir))
            payload = [report.__dict__ | {"issues": [i.__dict__ for i in report.issues]} for report in reports]
            _emit(payload, args.json, lambda _: _render_batch_reports(reports))
            issues = list_ingest_issues(connection)
            if issues and not args.json:
                print("\n接入问题台账：")
                print(_render_issues(issues))
        elif args.command == "build-facts":
            payload = rebuild_facts(connection, tolerance_z=args.tolerance_z, now=args.now)
            _emit(payload, args.json, lambda p: f"已建立比较事实：{p}")
        elif args.command == "refresh":
            directory = Path(args.data_dir)
            before_refs = {
                row[0] for row in connection.execute("SELECT DISTINCT result_ref FROM measurements")
            }
            if args.measurements_only:
                report = ingest_file(connection, directory / MEASUREMENT_FILE)
                reports = [report]
            else:
                reports = ingest_directory(connection, directory)
            # 受影响引用 = 本批次实际写入行的引用（含老引用上的新增重复）
            touched_refs = sorted({
                ref
                for item in reports
                if item.file_name == MEASUREMENT_FILE and item.status != "skipped_unchanged"
                for ref in measurement_refs_for_batch(connection, item.batch_id)
            })
            after_refs = {
                row[0] for row in connection.execute("SELECT DISTINCT result_ref FROM measurements")
            }
            new_refs = sorted(after_refs - before_refs)
            affected = links_for_measurements(connection, touched_refs)
            flagged = flag_affected_for_rereview(
                connection, affected,
                triggered_by="ingest_refresh",
                detail=f"增量批次涉及测量引用：{touched_refs or '无（重复接入）'}",
                now=args.now,
            )
            recomputed = None
            if not args.no_recompute and flagged:
                recomputed = recompute_pending(connection, now=args.now)
            payload = {
                "batches": [r.__dict__ for r in reports],
                "new_result_refs": new_refs,
                "touched_result_refs": touched_refs,
                "affected_links": affected,
                "flagged_facts": flagged,
                "recomputed": recomputed,
            }
            _emit(
                payload, args.json,
                lambda p: "\n".join(
                    [_render_batch_reports(reports), "",
                     f"新测量引用：{new_refs or '无'}；本批次涉及引用：{touched_refs or '无'}",
                     f"受影响链接：{affected or '无'}；置待复核事实 {flagged} 条",
                     f"重算结果：{recomputed or '未触发'}"]
                ),
            )
        elif args.command == "correct-sample":
            payload = apply_sample_correction(
                connection, args.experiment, args.sample,
                reason=args.reason, corrected_by=args.by, now=args.now,
            )
            _emit(payload, args.json, lambda p: f"已登记样品更正：{p}")
        elif args.command == "recompute-pending":
            payload = recompute_pending(connection, tolerance_z=args.tolerance_z, now=args.now)
            _emit(payload, args.json, lambda p: f"待复核重算完成：{p}")
        elif args.command == "signoff":
            fact_uid = fact_uid_for(args.link, args.metric)
            payload = signoff(
                connection, fact_uid, args.version, args.role,
                args.owner, args.decision, args.comment,
            )
            _emit(payload, args.json, lambda p: f"已记录签收：{p}")
        elif args.command == "query":
            facts = list_facts(
                connection, verdict=args.verdict, status=args.status,
                link_id=args.link, active_only=not args.all_versions and not args.status,
            )
            _emit(facts, args.json, _render_facts)
        elif args.command == "versions":
            fact_uid = fact_uid_for(args.link, args.metric)
            versions = get_fact_versions(connection, fact_uid)
            _emit(versions, args.json, _render_versions)
        elif args.command == "report":
            payload = build_report(connection, as_of=args.as_of)
            _emit(payload, args.json, _render_report)
        elif args.command == "issues":
            payload = list_ingest_issues(connection)
            _emit(payload, args.json, _render_issues)
        elif args.command == "batches":
            payload = list_batches(connection)
            _emit(payload, args.json, _render_batches)
        elif args.command == "corrections":
            payload = list_corrections(connection)
            _emit(payload, args.json, _render_corrections)
        else:  # pragma: no cover - argparse 已保证不可达
            parser.error("未知命令")
    finally:
        connection.close()
    return 0


def _render_batches(rows: list[dict]) -> str:
    lines = ["批次 | 文件 | 行数 | 写入 | 状态 | 接入时间"]
    for row in rows:
        lines.append(
            f"{row['batch_id']} | {row['file_name']} | {row['row_count']} | "
            f"{row['inserted_count']} | {row['status']} | {row['ingested_at']}"
        )
    return "\n".join(lines)


def _render_corrections(rows: list[dict]) -> str:
    if not rows:
        return "无更正记录。"
    lines = ["更正ID | 实验 | 字段 | 原值 | 新值 | 时间 | 操作人 | 原因"]
    for row in rows:
        lines.append(
            f"#{row['correction_id']} | {row['experiment_id']} | {row['field']} | "
            f"{row['old_value']} | {row['new_value']} | {row['corrected_at']} | "
            f"{row['corrected_by']} | {row['reason']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main())
    except BrokenPipeError:
        # 输出被 head 等下游命令关闭管道时安静退出，避免解释器噪声。
        sys.stderr.close()
        sys.exit(0)
