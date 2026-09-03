"""Unit tests for kv_cache.stage_metrics (P0 measurement substrate).

Deliberately dependency-free (no numpy/torch): these must run on any
host, including the Mac dev machine where the heavy suites skip.
"""
import importlib.util
import os
import threading
import time
import unittest

# Load by file path: the kv_cache package __init__ pulls numpy-dependent
# modules, and this suite must run on numpy-less hosts.
_SPEC = importlib.util.spec_from_file_location(
    "stage_metrics",
    os.path.join(os.path.dirname(__file__), "..", "kv_cache",
                 "stage_metrics.py"))
stage_metrics = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(stage_metrics)


class _FakeTiming:
    def __init__(self, total, device, host, components=None):
        self.total = total
        self.device = device
        self.host = host
        self.components = components


def _busy(seconds):
    """Burn CPU (not sleep) so thread_time moves."""
    t0 = time.thread_time()
    x = 0
    while time.thread_time() - t0 < seconds:
        x += 1
    return x


class TestStageMetrics(unittest.TestCase):
    def setUp(self):
        stage_metrics.reset()

    def test_basic_bracket_counts_and_wall(self):
        with stage_metrics.stage("decode"):
            time.sleep(0.02)
        snap = stage_metrics.snapshot()
        st = snap["stages"]["decode"]
        self.assertEqual(st["count"], 1)
        self.assertGreaterEqual(st["wall_s"]["sum"], 0.019)
        self.assertFalse(st["excluded_from_attribution"])

    def test_nesting_is_exclusive(self):
        """Child time must NOT be double-charged to the parent."""
        with stage_metrics.stage("prefill"):
            _busy(0.01)
            with stage_metrics.stage("demotion"):
                _busy(0.05)
            _busy(0.01)
        snap = stage_metrics.snapshot()
        parent = snap["stages"]["prefill"]
        child = snap["stages"]["demotion"]
        self.assertGreaterEqual(child["cpu_s"]["sum"], 0.045)
        # parent's exclusive CPU is ~0.02, never including the 0.05 child
        self.assertLess(parent["cpu_s"]["sum"], 0.04)
        self.assertGreaterEqual(parent["cpu_s"]["sum"], 0.015)
        # same exclusivity for wall
        self.assertLess(parent["wall_s"]["sum"],
                        child["wall_s"]["sum"] + 0.04)

    def test_excluded_stage_flagged_and_separated(self):
        with stage_metrics.stage("prefill"):
            with stage_metrics.stage("kv_gen", excluded=True):
                _busy(0.03)
        snap = stage_metrics.snapshot()
        self.assertTrue(snap["stages"]["kv_gen"]["excluded_from_attribution"])
        self.assertGreaterEqual(snap["excluded_cpu_s"], 0.025)
        # excluded CPU is not in attributed_cpu_s
        self.assertLess(snap["attributed_cpu_s"], 0.02)

    def test_current_stage_and_unstaged_fold(self):
        self.assertIsNone(stage_metrics.current_stage())
        with stage_metrics.stage("rag"):
            self.assertEqual(stage_metrics.current_stage(), "rag")
        stage_metrics.note_io(_FakeTiming(1.0, 0.6, 0.4))
        snap = stage_metrics.snapshot()
        self.assertAlmostEqual(
            snap["stages"]["unstaged"]["device_s"]["sum"], 0.6)

    def test_note_io_folds_into_active_stage_with_components(self):
        with stage_metrics.stage("decode"):
            stage_metrics.note_io(_FakeTiming(
                2.0, 1.5, 0.5, components={"exec_cmd": 1.2, "slm_read": 0.3}))
            stage_metrics.note_io(_FakeTiming(
                1.0, 0.5, 0.5, components={"exec_cmd": 0.5}))
        st = stage_metrics.snapshot()["stages"]["decode"]
        self.assertAlmostEqual(st["device_s"]["sum"], 2.0)
        self.assertAlmostEqual(st["host_io_s"]["sum"], 1.0)
        self.assertAlmostEqual(st["device_s"]["components"]["exec_cmd"], 1.7)
        self.assertAlmostEqual(st["device_s"]["components"]["slm_read"], 0.3)

    def test_invariant_violation_counted_and_clamped(self):
        with stage_metrics.stage("decode"):
            stage_metrics.note_io(_FakeTiming(1.0, 0.9, 0.9))  # 1.8 > 1.02
        snap = stage_metrics.snapshot()
        self.assertEqual(snap["timing_invariant_violations"], 1)
        st = snap["stages"]["decode"]
        # host clamped to total - device
        self.assertAlmostEqual(st["host_io_s"]["sum"], 0.1, places=6)

    def test_bytes_peak_and_cum(self):
        with stage_metrics.stage("prefill"):
            stage_metrics.note_bytes(100)
            stage_metrics.note_bytes(50)
            stage_metrics.release_bytes(120)
            stage_metrics.note_bytes(30)
        st = stage_metrics.snapshot()["stages"]["prefill"]
        self.assertEqual(st["host_bytes"]["peak"], 150)
        self.assertEqual(st["host_bytes"]["cum"], 180)

    def test_moved_bytes(self):
        stage_metrics.note_moved(host_bytes=10, device_bytes=99,
                                 stage_name="demotion")
        st = stage_metrics.snapshot()["stages"]["demotion"]
        self.assertEqual(st["bytes_moved_host"], 10)
        self.assertEqual(st["bytes_moved_device"], 99)

    def test_threads_do_not_cross_attribute(self):
        errs = []

        def worker(name):
            try:
                with stage_metrics.stage(name):
                    if stage_metrics.current_stage() != name:
                        errs.append(f"cross-talk in {name}")
                    _busy(0.02)
            except Exception as e:  # pragma: no cover
                errs.append(repr(e))

        ts = [threading.Thread(target=worker, args=(f"stage{i}",))
              for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        snap = stage_metrics.snapshot()
        for i in range(4):
            self.assertGreaterEqual(
                snap["stages"][f"stage{i}"]["cpu_s"]["sum"], 0.015)

    def test_reset_zeroes_everything(self):
        with stage_metrics.stage("decode"):
            pass
        stage_metrics.note_io(_FakeTiming(1.0, 2.0, 2.0),
                              stage_name="decode")
        stage_metrics.reset()
        snap = stage_metrics.snapshot()
        self.assertEqual(snap["stages"], {})
        self.assertEqual(snap["timing_invariant_violations"], 0)

    def test_snapshot_schema(self):
        with stage_metrics.stage("prefix"):
            pass
        snap = stage_metrics.snapshot(toggles={"s2": "host"})
        self.assertEqual(snap["schema_version"], 1)
        self.assertEqual(snap["toggles"], {"s2": "host"})
        for k in ("attributed_cpu_s", "excluded_cpu_s",
                  "process_cpu_s_window", "unattributed_cpu_s",
                  "timing_invariant_violations"):
            self.assertIn(k, snap)
        st = snap["stages"]["prefix"]
        for k in ("count", "wall_s", "cpu_s", "device_s", "host_io_s",
                  "host_bytes", "bytes_moved_host", "bytes_moved_device",
                  "excluded_from_attribution"):
            self.assertIn(k, st)


if __name__ == "__main__":
    unittest.main()
