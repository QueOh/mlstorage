#!/usr/bin/env python3
"""Unit tests for host/process system metrics tracking."""

import time

from kv_cache.monitoring import SystemMetricsTracker


def test_system_metrics_tracker_empty_before_start():
    tracker = SystemMetricsTracker()
    assert tracker.summary() == {}


def test_system_metrics_tracker_collects_basic_fields():
    tracker = SystemMetricsTracker()
    tracker.start()
    # Ensure non-zero elapsed wall time for stable percent calculations.
    time.sleep(0.01)
    tracker.stop()

    summary = tracker.summary()
    assert summary["elapsed_wall_s"] >= 0.0
    assert summary["process_cpu_time_s"] >= 0.0
    assert summary["process_cpu_percent_avg"] >= 0.0
    assert summary["host_cpu_percent_avg"] >= 0.0
    assert summary["cpu_count"] >= 1.0
