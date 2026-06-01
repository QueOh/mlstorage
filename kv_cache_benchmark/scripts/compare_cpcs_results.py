#!/usr/bin/env python3
"""Compare baseline vs CPCS benchmark result JSON files."""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


COLUMNS: List[str] = [
    "run_name",
    "baseline_json",
    "cpcs_json",
    "baseline_system_metrics_dir",
    "cpcs_system_metrics_dir",
    "throughput_delta_percent",
    "storage_throughput_delta_percent",
    "storage_p95_delta_percent",
    "storage_p99_delta_percent",
    "host_cpu_delta_percent",
    "fabric_rx_bytes_delta_percent",
    "fabric_tx_bytes_delta_percent",
    "media_read_bytes_delta_percent",
    "media_write_bytes_delta_percent",
    "cpcs_command_p95_ms",
    "cpcs_compute_p95_us",
    "pack_ratio_or_selectivity",
    "cpcs_bootstrap_ok",
    "cpcs_program_rsid",
    "cpcs_program_pind_default",
    "cpcs_slm_read_mode",
    "cpcs_slm_write_mode",
    "cpcs_slm_rw_lba_bytes",
    "demo_claim_met",
    "demo_claim_reason",
    "correctness_status",
    "notes",
]


def _get_path(data: Dict[str, Any], path: Iterable[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    return None


def _pick_float(data: Dict[str, Any], paths: List[List[str]]) -> Optional[float]:
    for path in paths:
        value = _as_float(_get_path(data, path))
        if value is not None:
            return value
    return None


def _delta_percent(baseline: Optional[float], candidate: Optional[float]) -> Optional[float]:
    if baseline is None or candidate is None:
        return None
    if baseline == 0:
        return None
    return ((candidate - baseline) / baseline) * 100.0


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _extract_common_metrics(doc: Dict[str, Any]) -> Dict[str, Optional[float]]:
    return {
        "throughput": _pick_float(doc, [["summary", "avg_throughput_tokens_per_sec"]]),
        "storage_throughput": _pick_float(doc, [["summary", "storage_throughput_tokens_per_sec"]]),
        "storage_p95": _pick_float(doc, [["summary", "storage_io_latency_ms", "p95"]]),
        "storage_p99": _pick_float(doc, [["summary", "storage_io_latency_ms", "p99"]]),
        "host_cpu": _pick_float(
            doc,
            [
                ["summary", "host_cpu_percent_avg"],
                ["summary", "system_metrics", "host_cpu_percent_avg"],
                ["summary", "cache_stats", "host_cpu_percent_avg"],
            ],
        ),
        "fabric_rx_bytes": _pick_float(
            doc,
            [
                ["summary", "fabric_rx_bytes"],
                ["summary", "system_metrics", "fabric_rx_bytes"],
            ],
        ),
        "fabric_tx_bytes": _pick_float(
            doc,
            [
                ["summary", "fabric_tx_bytes"],
                ["summary", "system_metrics", "fabric_tx_bytes"],
            ],
        ),
        "media_read_bytes": _pick_float(
            doc,
            [
                ["summary", "media_read_bytes"],
                ["summary", "cache_stats", "media_read_bytes"],
                ["summary", "cache_stats", "cpcs_metrics", "cpcs_media_bytes_read_est"],
            ],
        ),
        "media_write_bytes": _pick_float(
            doc,
            [
                ["summary", "media_write_bytes"],
                ["summary", "cache_stats", "media_write_bytes"],
                ["summary", "cache_stats", "cpcs_metrics", "cpcs_media_bytes_written_est"],
            ],
        ),
    }


def _extract_cpcs_metrics(doc: Dict[str, Any]) -> Dict[str, Any]:
    cpcs = _get_path(doc, ["summary", "cache_stats", "cpcs_metrics"])
    if not isinstance(cpcs, dict):
        cpcs = {}
    bootstrap = cpcs.get("cpcs_bootstrap")
    bootstrap_ok: Optional[bool] = None
    if isinstance(bootstrap, dict):
        bootstrap_ok = _as_bool(bootstrap.get("ok"))
    bytes_in = _as_float(cpcs.get("cpcs_bytes_in_total"))
    bytes_out = _as_float(cpcs.get("cpcs_bytes_out_total"))
    ratio = _as_float(cpcs.get("cpcs_pack_ratio"))
    if ratio is None:
        ratio = _as_float(cpcs.get("cpcs_compression_ratio"))
    if ratio is None:
        ratio = _as_float(cpcs.get("cpcs_selected_blocks_ratio"))
    if ratio is None and bytes_in is not None and bytes_out and bytes_out > 0.0:
        ratio = bytes_in / bytes_out
    return {
        "command_p95_ms": _as_float(cpcs.get("cpcs_command_latency_ms_p95")),
        "compute_p95_us": _as_float(cpcs.get("cpcs_device_compute_us_p95")),
        "pack_ratio_or_selectivity": ratio,
        "commands_failed": _as_float(cpcs.get("cpcs_commands_failed")),
        "bootstrap_ok": bootstrap_ok,
        "program_rsid": _as_float(cpcs.get("cpcs_program_rsid")),
        "program_pind_default": _as_float(cpcs.get("cpcs_program_pind_default")),
        "slm_read_mode": str(cpcs.get("cpcs_slm_read_address_mode") or ""),
        "slm_write_mode": str(cpcs.get("cpcs_slm_write_address_mode") or ""),
        "slm_rw_lba_bytes": _as_float(cpcs.get("cpcs_slm_rw_lba_bytes")),
    }


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.6f}"


def _fmt_bool(value: Optional[bool]) -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def _evaluate_demo_claim(
    *,
    throughput_delta: Optional[float],
    storage_throughput_delta: Optional[float],
    storage_p95_delta: Optional[float],
    host_cpu_delta: Optional[float],
    fabric_rx_delta: Optional[float],
    fabric_tx_delta: Optional[float],
    media_read_delta: Optional[float],
    media_write_delta: Optional[float],
) -> tuple[bool, str]:
    hits: List[str] = []
    if throughput_delta is not None and throughput_delta >= 10.0:
        hits.append("throughput>=+10%")
    if storage_throughput_delta is not None and storage_throughput_delta >= 10.0:
        hits.append("storage_throughput>=+10%")
    if storage_p95_delta is not None and storage_p95_delta <= -10.0:
        hits.append("storage_p95<=-10%")
    if host_cpu_delta is not None and host_cpu_delta <= -10.0:
        hits.append("host_cpu<=-10%")
    if fabric_rx_delta is not None and fabric_rx_delta <= -10.0:
        hits.append("fabric_rx<=-10%")
    if fabric_tx_delta is not None and fabric_tx_delta <= -10.0:
        hits.append("fabric_tx<=-10%")
    if media_read_delta is not None and media_read_delta <= -10.0:
        hits.append("media_read<=-10%")
    if media_write_delta is not None and media_write_delta <= -10.0:
        hits.append("media_write<=-10%")
    if hits:
        return True, ",".join(hits)
    return False, ""


def _read_manifest(path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            run_name = str(row.get("run_name", "")).strip()
            baseline_json = str(row.get("baseline_json", "")).strip()
            cpcs_json = str(row.get("cpcs_json", "")).strip()
            baseline_system_metrics_dir = str(row.get("baseline_system_metrics_dir", "")).strip()
            cpcs_system_metrics_dir = str(row.get("cpcs_system_metrics_dir", "")).strip()
            if run_name and baseline_json and cpcs_json:
                rows.append({
                    "run_name": run_name,
                    "baseline_json": baseline_json,
                    "cpcs_json": cpcs_json,
                    "baseline_system_metrics_dir": baseline_system_metrics_dir,
                    "cpcs_system_metrics_dir": cpcs_system_metrics_dir,
                })
    return rows


def _parse_pairs(pairs: List[str]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for raw in pairs:
        if "=" not in raw:
            raise ValueError(f"Invalid --pair format: {raw}")
        run_name, rhs = raw.split("=", 1)
        if "," not in rhs:
            raise ValueError(f"Invalid --pair format: {raw}")
        baseline_json, cpcs_json = rhs.split(",", 1)
        rows.append({
            "run_name": run_name.strip(),
            "baseline_json": baseline_json.strip(),
            "cpcs_json": cpcs_json.strip(),
            "baseline_system_metrics_dir": "",
            "cpcs_system_metrics_dir": "",
        })
    return rows


def _write_csv(path: Path, rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in COLUMNS})


def _write_markdown(path: Path, rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        claim_met_rows = sum(1 for row in rows if str(row.get("demo_claim_met", "")).strip().lower() == "true")
        fh.write("# CPCS Comparison Summary\n\n")
        fh.write(f"Rows: {len(rows)}\n\n")
        fh.write(f"Demo claim met rows: {claim_met_rows}/{len(rows)}\n\n")
        fh.write("| " + " | ".join(COLUMNS) + " |\n")
        fh.write("|" + "|".join(["---"] * len(COLUMNS)) + "|\n")
        for row in rows:
            values = [row.get(col, "").replace("\n", " ") for col in COLUMNS]
            fh.write("| " + " | ".join(values) + " |\n")

        medians = _build_group_medians(rows)
        if medians:
            fh.write("\n## Group Medians\n\n")
            median_cols = [
                "throughput_delta_percent",
                "storage_throughput_delta_percent",
                "storage_p95_delta_percent",
                "storage_p99_delta_percent",
                "host_cpu_delta_percent",
                "cpcs_command_p95_ms",
                "cpcs_compute_p95_us",
                "pack_ratio_or_selectivity",
            ]
            fh.write("| group | " + " | ".join(median_cols) + " |\n")
            fh.write("|" + "|".join(["---"] * (len(median_cols) + 1)) + "|\n")
            for group, metrics in medians.items():
                vals = [metrics.get(col, "") for col in median_cols]
                fh.write("| " + str(group) + " | " + " | ".join(vals) + " |\n")


def _group_name(run_name: str) -> str:
    # storage_only_t01 -> storage_only
    m = re.match(r"^(.*)_t\d+$", run_name)
    if m:
        return m.group(1)
    return run_name


def _to_float(text: str) -> Optional[float]:
    try:
        return float(text)
    except Exception:
        return None


def _build_group_medians(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    numeric_cols = [
        "throughput_delta_percent",
        "storage_throughput_delta_percent",
        "storage_p95_delta_percent",
        "storage_p99_delta_percent",
        "host_cpu_delta_percent",
        "cpcs_command_p95_ms",
        "cpcs_compute_p95_us",
        "pack_ratio_or_selectivity",
    ]
    grouped: Dict[str, Dict[str, List[float]]] = {}
    for row in rows:
        group = _group_name(str(row.get("run_name", "")))
        g = grouped.setdefault(group, {col: [] for col in numeric_cols})
        for col in numeric_cols:
            val = _to_float(str(row.get(col, "")).strip())
            if val is not None:
                g[col].append(val)

    medians: Dict[str, Dict[str, str]] = {}
    for group, cols in grouped.items():
        med: Dict[str, str] = {}
        for col, vals in cols.items():
            med[col] = f"{statistics.median(vals):.6f}" if vals else ""
        medians[group] = med
    return medians


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare baseline and CPCS benchmark result files.")
    parser.add_argument(
        "--manifest",
        type=str,
        default="results/cpcs/run_manifest.csv",
        help="CSV file containing run_name,baseline_json,cpcs_json columns.",
    )
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        help="Direct pair input: run_name=baseline.json,cpcs.json (repeatable).",
    )
    parser.add_argument(
        "--output-csv",
        type=str,
        default="results/cpcs_comparison.csv",
        help="Output CSV path.",
    )
    parser.add_argument(
        "--output-md",
        type=str,
        default="results/cpcs_comparison.md",
        help="Output markdown path.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail if any input file is missing or invalid.",
    )
    args = parser.parse_args()

    inputs: List[Dict[str, str]] = []
    if args.pair:
        inputs.extend(_parse_pairs(args.pair))
    manifest_path = Path(args.manifest)
    if manifest_path.exists():
        inputs.extend(_read_manifest(manifest_path))
    if not inputs:
        raise SystemExit("No input pairs found. Provide --pair or a valid --manifest file.")

    rows: List[Dict[str, str]] = []
    for item in inputs:
        run_name = item["run_name"]
        baseline_path = Path(item["baseline_json"])
        cpcs_path = Path(item["cpcs_json"])
        row: Dict[str, str] = {
            "run_name": run_name,
            "baseline_json": str(baseline_path),
            "cpcs_json": str(cpcs_path),
            "baseline_system_metrics_dir": str(item.get("baseline_system_metrics_dir", "") or ""),
            "cpcs_system_metrics_dir": str(item.get("cpcs_system_metrics_dir", "") or ""),
            "correctness_status": "",
            "notes": "",
        }

        if not baseline_path.exists() or not cpcs_path.exists():
            row["correctness_status"] = "missing_input"
            row["notes"] = f"missing file(s): baseline_exists={baseline_path.exists()}, cpcs_exists={cpcs_path.exists()}"
            rows.append(row)
            if args.strict:
                raise FileNotFoundError(row["notes"])
            continue

        try:
            baseline_doc = _load_json(baseline_path)
            cpcs_doc = _load_json(cpcs_path)
        except Exception as exc:
            row["correctness_status"] = "invalid_json"
            row["notes"] = f"json parse error: {exc}"
            rows.append(row)
            if args.strict:
                raise
            continue

        b = _extract_common_metrics(baseline_doc)
        c = _extract_common_metrics(cpcs_doc)
        cpcs = _extract_cpcs_metrics(cpcs_doc)

        throughput_delta = _delta_percent(b["throughput"], c["throughput"])
        storage_throughput_delta = _delta_percent(b["storage_throughput"], c["storage_throughput"])
        storage_p95_delta = _delta_percent(b["storage_p95"], c["storage_p95"])
        storage_p99_delta = _delta_percent(b["storage_p99"], c["storage_p99"])
        host_cpu_delta = _delta_percent(b["host_cpu"], c["host_cpu"])
        fabric_rx_delta = _delta_percent(b["fabric_rx_bytes"], c["fabric_rx_bytes"])
        fabric_tx_delta = _delta_percent(b["fabric_tx_bytes"], c["fabric_tx_bytes"])
        media_read_delta = _delta_percent(b["media_read_bytes"], c["media_read_bytes"])
        media_write_delta = _delta_percent(b["media_write_bytes"], c["media_write_bytes"])

        row["throughput_delta_percent"] = _fmt(throughput_delta)
        row["storage_throughput_delta_percent"] = _fmt(storage_throughput_delta)
        row["storage_p95_delta_percent"] = _fmt(storage_p95_delta)
        row["storage_p99_delta_percent"] = _fmt(storage_p99_delta)
        row["host_cpu_delta_percent"] = _fmt(host_cpu_delta)
        row["fabric_rx_bytes_delta_percent"] = _fmt(fabric_rx_delta)
        row["fabric_tx_bytes_delta_percent"] = _fmt(fabric_tx_delta)
        row["media_read_bytes_delta_percent"] = _fmt(media_read_delta)
        row["media_write_bytes_delta_percent"] = _fmt(media_write_delta)
        row["cpcs_command_p95_ms"] = _fmt(cpcs["command_p95_ms"])
        row["cpcs_compute_p95_us"] = _fmt(cpcs["compute_p95_us"])
        row["pack_ratio_or_selectivity"] = _fmt(cpcs["pack_ratio_or_selectivity"])
        row["cpcs_bootstrap_ok"] = _fmt_bool(cpcs.get("bootstrap_ok"))
        row["cpcs_program_rsid"] = _fmt(cpcs.get("program_rsid"))
        row["cpcs_program_pind_default"] = _fmt(cpcs.get("program_pind_default"))
        row["cpcs_slm_read_mode"] = str(cpcs.get("slm_read_mode") or "")
        row["cpcs_slm_write_mode"] = str(cpcs.get("slm_write_mode") or "")
        row["cpcs_slm_rw_lba_bytes"] = _fmt(cpcs.get("slm_rw_lba_bytes"))
        claim_met, claim_reason = _evaluate_demo_claim(
            throughput_delta=throughput_delta,
            storage_throughput_delta=storage_throughput_delta,
            storage_p95_delta=storage_p95_delta,
            host_cpu_delta=host_cpu_delta,
            fabric_rx_delta=fabric_rx_delta,
            fabric_tx_delta=fabric_tx_delta,
            media_read_delta=media_read_delta,
            media_write_delta=media_write_delta,
        )
        row["demo_claim_met"] = "true" if claim_met else "false"
        row["demo_claim_reason"] = claim_reason

        failed = cpcs.get("commands_failed")
        if failed is not None and failed > 0:
            row["correctness_status"] = "command_failures"
        elif cpcs["pack_ratio_or_selectivity"] is not None:
            row["correctness_status"] = "commands_ok"
        else:
            row["correctness_status"] = "unknown"

        notes: List[str] = []
        if c["fabric_rx_bytes"] is None or c["fabric_tx_bytes"] is None:
            notes.append("fabric counters unavailable")
        if c["media_read_bytes"] is None or c["media_write_bytes"] is None:
            notes.append("media byte counters unavailable")
        if c["host_cpu"] is None:
            notes.append("host CPU metric unavailable")
        row["notes"] = "; ".join(notes)
        rows.append(row)

    _write_csv(Path(args.output_csv), rows)
    _write_markdown(Path(args.output_md), rows)
    print(f"Wrote comparison CSV: {args.output_csv}")
    print(f"Wrote comparison Markdown: {args.output_md}")


if __name__ == "__main__":
    main()
