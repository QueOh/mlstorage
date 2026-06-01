"""
CPCS metrics aggregation helpers.

This module keeps CPCS-specific counters and latency distributions separate
from the benchmark's generic cache-tier statistics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import numpy as np


@dataclass
class CPCSMetrics:
    """Accumulates CPCS command counters and byte/latency statistics."""

    commands_total: int = 0
    commands_failed: int = 0
    bytes_in_total: int = 0
    bytes_out_total: int = 0

    command_latency_ms: List[float] = field(default_factory=list)
    device_compute_us: List[float] = field(default_factory=list)

    mode_counts: Dict[str, int] = field(default_factory=dict)
    extra_accumulators: Dict[str, float] = field(default_factory=dict)

    def record(
        self,
        *,
        ok: bool,
        bytes_in: int,
        bytes_out: int,
        command_latency_s: float,
        device_compute_us: float,
        mode: str = "",
        extra: Dict[str, Any] | None = None,
    ) -> None:
        self.commands_total += 1
        if not ok:
            self.commands_failed += 1

        self.bytes_in_total += int(max(0, bytes_in))
        self.bytes_out_total += int(max(0, bytes_out))

        self.command_latency_ms.append(max(0.0, float(command_latency_s)) * 1000.0)
        self.device_compute_us.append(max(0.0, float(device_compute_us)))

        mode_name = str(mode or "").strip()
        if mode_name:
            self.mode_counts[mode_name] = self.mode_counts.get(mode_name, 0) + 1

        if extra:
            for key, value in extra.items():
                if isinstance(value, (int, float)):
                    self.extra_accumulators[key] = self.extra_accumulators.get(key, 0.0) + float(value)

    @staticmethod
    def _pct(values: List[float], pct: float) -> float:
        if not values:
            return 0.0
        return float(np.percentile(np.asarray(values, dtype=np.float64), pct))

    def to_dict(self) -> Dict[str, Any]:
        pack_ratio = 0.0
        if self.bytes_out_total > 0:
            pack_ratio = float(self.bytes_in_total) / float(self.bytes_out_total)

        summary: Dict[str, Any] = {
            "cpcs_commands_total": int(self.commands_total),
            "cpcs_commands_failed": int(self.commands_failed),
            "cpcs_bytes_in_total": int(self.bytes_in_total),
            "cpcs_bytes_out_total": int(self.bytes_out_total),
            "cpcs_pack_ratio": pack_ratio,
            "cpcs_compression_ratio": pack_ratio,
            "cpcs_command_latency_ms_p50": self._pct(self.command_latency_ms, 50.0),
            "cpcs_command_latency_ms_p95": self._pct(self.command_latency_ms, 95.0),
            "cpcs_command_latency_ms_p99": self._pct(self.command_latency_ms, 99.0),
            "cpcs_device_compute_us_p50": self._pct(self.device_compute_us, 50.0),
            "cpcs_device_compute_us_p95": self._pct(self.device_compute_us, 95.0),
            "cpcs_device_compute_us_p99": self._pct(self.device_compute_us, 99.0),
            "cpcs_mode_counts": dict(self.mode_counts),
        }
        selected_blocks = float(self.extra_accumulators.get("selector_selected_blocks", 0.0))
        total_blocks = float(self.extra_accumulators.get("selector_total_blocks", 0.0))
        if total_blocks > 0.0:
            summary["cpcs_selected_blocks_ratio"] = selected_blocks / total_blocks
        else:
            summary["cpcs_selected_blocks_ratio"] = 0.0
        media_read_est = int(max(0.0, float(self.extra_accumulators.get("media_read_bytes", 0.0))))
        media_write_est = int(max(0.0, float(self.extra_accumulators.get("media_write_bytes", 0.0))))
        summary["cpcs_media_bytes_read_est"] = media_read_est
        summary["cpcs_media_bytes_written_est"] = media_write_est
        if self.extra_accumulators:
            summary["cpcs_extra_totals"] = dict(self.extra_accumulators)
        return summary
