#!/usr/bin/env python3
"""Render bounded release-summary assets from a measured benchmark artifact."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
from pathlib import Path
from typing import Any

EXPECTED_SCHEMA = "recall-origin-benchmark/v1"
SUMMARY_SCHEMA = "recall-origin-benchmark-summary/v1"


def _load_artifact(path: Path) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    artifact = json.loads(payload)
    if not isinstance(artifact, dict) or artifact.get("schema_version") != EXPECTED_SCHEMA:
        msg = f"{path} is not a {EXPECTED_SCHEMA} artifact"
        raise ValueError(msg)
    return artifact, hashlib.sha256(payload).hexdigest()


def build_summary(
    artifact: dict[str, Any],
    *,
    artifact_name: str,
    artifact_sha256: str,
) -> dict[str, Any]:
    """Select measured values used by release copy and charts."""
    configuration = artifact["configuration"]
    engine = configuration["engine"]
    retrieval = artifact["retrieval"]
    performance = artifact["performance"]
    quality = artifact["quality"]
    return {
        "schema_version": SUMMARY_SCHEMA,
        "source_artifact": artifact_name,
        "source_artifact_sha256": artifact_sha256,
        "generated_at_utc": artifact["generated_at_utc"],
        "scope": {
            "corpus_kind": "deterministic synthetic",
            "document_count": artifact["corpus"]["document_count"],
            "query_count": retrieval["query_count"],
            "seed": configuration["seed"],
            "k": engine["search_limit_k"],
            "durable": engine["durable"],
            "single_measured_run": True,
            "cross_system_comparison": False,
            "vector_baseline": artifact["comparisons"]["vector_baseline"],
        },
        "quality": {
            "passed": quality["passed"],
            "passed_count": quality["passed_count"],
            "case_count": quality["case_count"],
        },
        "retrieval": {
            "recall_at_k": retrieval["recall_at_k"],
            "mrr": retrieval["mrr"],
            "latency_ms": retrieval["latency_ms"],
            "by_query_type": retrieval["by_query_type"],
        },
        "ingest": performance["ingest"],
        "storage": {
            "db_size_bytes": performance["db_size_bytes"],
            "database_file_bytes": performance["database_file_bytes"],
        },
        "runtime": {
            "package_version": engine["package_version"],
            "python_version": artifact["provenance"]["python_version"],
            "sqlite_version": artifact["provenance"]["sqlite_version"],
            "platform": artifact["provenance"]["platform"],
            "machine": artifact["provenance"]["machine"],
            "logical_cpu_count": artifact["provenance"]["logical_cpu_count"],
            "source_snapshot_sha256": artifact["provenance"]["source_snapshot"]["sha256"],
        },
    }


def _metric_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    scope = summary["scope"]
    retrieval = summary["retrieval"]
    rows: list[dict[str, Any]] = [
        {
            "category": "retrieval",
            "metric": f"Recall@{scope['k']}",
            "segment": "all",
            "value": retrieval["recall_at_k"],
            "unit": "ratio",
            "sample_size": scope["query_count"],
        },
        {
            "category": "retrieval",
            "metric": "MRR",
            "segment": "all",
            "value": retrieval["mrr"],
            "unit": "ratio",
            "sample_size": scope["query_count"],
        },
    ]
    for segment, metrics in [("all", retrieval), *retrieval["by_query_type"].items()]:
        for percentile_name in ("p50", "p95", "p99"):
            rows.append(
                {
                    "category": "latency",
                    "metric": percentile_name,
                    "segment": segment,
                    "value": metrics["latency_ms"][percentile_name],
                    "unit": "ms",
                    "sample_size": metrics.get("query_count", scope["query_count"]),
                }
            )
    rows.extend(
        [
            {
                "category": "ingest",
                "metric": "throughput",
                "segment": "all",
                "value": summary["ingest"]["writes_per_second"],
                "unit": "writes/s",
                "sample_size": summary["ingest"]["writes"],
            },
            {
                "category": "storage",
                "metric": "database_size",
                "segment": "all",
                "value": summary["storage"]["db_size_bytes"],
                "unit": "bytes",
                "sample_size": scope["document_count"],
            },
        ]
    )
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["category", "metric", "segment", "value", "unit", "sample_size"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_checksums(directory: Path, paths: list[Path]) -> Path:
    checksum_path = directory / "SHA256SUMS"
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(paths, key=lambda item: item.name)
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksum_path


def _bar_width(value: float, maximum: float) -> float:
    return 0.0 if maximum <= 0 else value / maximum * 610


def render_svg(summary: dict[str, Any]) -> str:
    """Render a standalone SVG whose labels come only from measured values."""
    retrieval = summary["retrieval"]
    ingest = summary["ingest"]
    scope = summary["scope"]
    quality = summary["quality"]
    runtime = summary["runtime"]
    latency_groups = [("All", retrieval), *retrieval["by_query_type"].items()]
    max_latency = max(
        float(metrics["latency_ms"][percentile_name])
        for _, metrics in latency_groups
        for percentile_name in ("p50", "p95", "p99")
    )
    chart_max = max_latency * 1.08
    colors = {"p50": "#35C6A1", "p95": "#5B8DEF", "p99": "#9B7BF6"}
    document_count = int(scope["document_count"])
    scale_label = "10k" if document_count == 10_000 else f"{document_count:,}-document"
    durable = bool(scope["durable"])
    durability_label = "durable" if durable else "relaxed-durability"
    title = f"{scale_label} {durability_label} local benchmark"
    ingest_label = "Durable ingest" if durable else "Relaxed-durability ingest"
    cards = [
        (f"Recall@{scope['k']}", f"{retrieval['recall_at_k']:.3f}"),
        ("MRR", f"{retrieval['mrr']:.3f}"),
        ("Search p95", f"{retrieval['latency_ms']['p95']:.1f} ms"),
        (ingest_label, f"{ingest['writes_per_second']:.2f} writes/s"),
    ]
    fragments = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="760" '
        'viewBox="0 0 1200 760" role="img" aria-labelledby="title desc">',
        f'<title id="title">RecallOrigin {html.escape(title)}</title>',
        (
            f'<desc id="desc">Measured Recall at {scope["k"]}, mean reciprocal rank, '
            f"search latency, and {html.escape(durability_label)} ingest throughput on a "
            "deterministic synthetic corpus.</desc>"
        ),
        "<style>",
        ".title{font:700 34px ui-sans-serif,system-ui,sans-serif;fill:#EAF1FF}",
        ".subtitle{font:400 16px ui-sans-serif,system-ui,sans-serif;fill:#9DB0CE}",
        ".label{font:600 14px ui-sans-serif,system-ui,sans-serif;fill:#AFC0D9}",
        ".metric{font:700 27px ui-monospace,SFMono-Regular,monospace;fill:#FFFFFF}",
        ".axis{font:400 13px ui-monospace,SFMono-Regular,monospace;fill:#8EA2C2}",
        ".note{font:400 14px ui-sans-serif,system-ui,sans-serif;fill:#AFC0D9}",
        "</style>",
        '<rect width="1200" height="760" rx="24" fill="#0B1220"/>',
        '<rect x="28" y="28" width="1144" height="704" rx="20" fill="#111C30" stroke="#243653"/>',
        f'<text class="title" x="64" y="82">{html.escape(title)}</text>',
        (
            f'<text class="subtitle" x="64" y="112">'
            f"{scope['document_count']:,} synthetic documents · {scope['query_count']} queries "
            f"· single measured run · {html.escape(runtime['platform'])}</text>"
        ),
    ]
    for index, (label, card_value) in enumerate(cards):
        card_x = 64 + index * 273
        fragments.extend(
            [
                f'<rect x="{card_x}" y="142" width="245" height="112" rx="14" fill="#17263E"/>',
                f'<text class="label" x="{card_x + 20}" y="178">{html.escape(label)}</text>',
                f'<text class="metric" x="{card_x + 20}" y="222">{html.escape(card_value)}</text>',
            ]
        )

    fragments.append(
        '<text class="label" x="64" y="304">Search latency by query type (milliseconds)</text>'
    )
    chart_x = 290
    chart_y = 346
    row_gap = 92
    for tick in range(5):
        tick_value = chart_max * tick / 4
        tick_x = chart_x + 610 * tick / 4
        fragments.extend(
            [
                f'<line x1="{tick_x:.2f}" y1="326" x2="{tick_x:.2f}" y2="616" '
                'stroke="#273955" stroke-width="1"/>',
                f'<text class="axis" x="{tick_x:.2f}" y="638" '
                f'text-anchor="middle">{tick_value:.0f}</text>',
            ]
        )
    for group_index, (group_name, metrics) in enumerate(latency_groups):
        base_y = chart_y + group_index * row_gap
        query_count = metrics.get("query_count", scope["query_count"])
        fragments.append(
            f'<text class="label" x="64" y="{base_y + 25}">'
            f"{html.escape(str(group_name).title())} · n={query_count}</text>"
        )
        for offset, percentile_name in enumerate(("p50", "p95", "p99")):
            latency_value = float(metrics["latency_ms"][percentile_name])
            y = base_y + offset * 24
            width = _bar_width(latency_value, chart_max)
            fragments.extend(
                [
                    f'<text class="axis" x="250" y="{y + 12}" text-anchor="end">'
                    f"{percentile_name}</text>",
                    f'<rect x="{chart_x}" y="{y}" width="{width:.2f}" height="16" rx="8" '
                    f'fill="{colors[percentile_name]}"/>',
                    f'<text class="axis" x="{chart_x + width + 10:.2f}" y="{y + 12}">'
                    f"{latency_value:.1f}</text>",
                ]
            )
    fragments.extend(
        [
            '<line x1="64" y1="665" x2="1136" y2="665" stroke="#243653"/>',
            (
                f'<text class="note" x="64" y="698">Quality cases: '
                f"{quality['passed_count']}/{quality['case_count']} passed · "
                "Vector retriever: not configured · No candidate comparison measured</text>"
            ),
            (
                f'<text class="axis" x="1136" y="698" text-anchor="end">artifact '
                f"{summary['source_artifact_sha256'][:12]}…</text>"
            ),
            "</svg>",
        ]
    )
    return "\n".join(fragments) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render release-summary JSON, CSV, and SVG from a measured artifact."
    )
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    artifact, artifact_sha256 = _load_artifact(args.artifact)
    output_dir = args.output_dir or args.artifact.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = args.artifact.stem
    summary = build_summary(
        artifact,
        artifact_name=args.artifact.name,
        artifact_sha256=artifact_sha256,
    )
    summary_path = output_dir / f"{base_name}-summary.json"
    csv_path = output_dir / f"{base_name}-metrics.csv"
    svg_path = output_dir / f"{base_name}-summary.svg"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(csv_path, _metric_rows(summary))
    svg_path.write_text(render_svg(summary), encoding="utf-8")
    checksum_inputs = [args.artifact, summary_path, csv_path, svg_path]
    inspector_path = output_dir / f"{base_name}-inspector.json"
    if inspector_path.is_file():
        checksum_inputs.append(inspector_path)
    run_metadata_path = output_dir / f"{base_name}-run.json"
    if run_metadata_path.is_file():
        checksum_inputs.append(run_metadata_path)
    checksums_path = _write_checksums(output_dir, checksum_inputs)
    print(
        json.dumps(
            {
                "artifact_sha256": artifact_sha256,
                "checksums": str(checksums_path),
                "csv": str(csv_path),
                "summary": str(summary_path),
                "svg": str(svg_path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
