"""Per-stage attribution: wall, thread-CPU, host bytes, and I/O splits.

The benchmark's request path runs entirely on the worker thread that
dequeued the request (demotions triggered inside allocate_cache
included), so a thread-local stage stack with time.thread_time()
deltas attributes CPU soundly. Nesting is EXCLUSIVE: entering a child
stage suspends the parent's accumulation, so a demotion that fires
inside a prefill write is charged to "demotion", not "prefill".

Stages marked excluded=True (kv_gen: synthetic input generation, the
XOR stamp) are measured but flagged `excluded_from_attribution` --
they are benchmark scaffolding, not offloadable work, and must not
pollute stage CPU comparisons.

Module state is process-global (one benchmark per process) and
reset via reset(), which MultiTierCache.reset_stats() calls so
prepopulation/preconditioning never contaminates the measured window.

Zero third-party dependencies by design: the unit tests for this
module must run on hosts without numpy.
"""
import json
import threading
import time
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

# Hard cap on retained per-stage wall samples (percentile source). At
# 200k floats/stage this is bounded MBs; overflow is counted, never
# silently dropped.
_MAX_SAMPLES = 200_000

_lock = threading.Lock()
_tls = threading.local()


class _StageAcc:
    __slots__ = ("count", "wall_sum", "cpu_sum", "walls", "dropped",
                 "excluded", "device_sum", "host_io_sum", "components",
                 "bytes_cur", "bytes_peak", "bytes_cum",
                 "bytes_moved_host", "bytes_moved_device")

    def __init__(self, excluded: bool = False):
        self.count = 0
        self.wall_sum = 0.0
        self.cpu_sum = 0.0
        self.walls: List[float] = []
        self.dropped = 0
        self.excluded = excluded
        self.device_sum = 0.0
        self.host_io_sum = 0.0
        self.components: Dict[str, float] = {}
        self.bytes_cur = 0
        self.bytes_peak = 0
        self.bytes_cum = 0
        self.bytes_moved_host = 0
        self.bytes_moved_device = 0


_stages: Dict[str, _StageAcc] = {}
_process_cpu_start: float = time.process_time()
_invariant_violations: int = 0


def _acc(name: str, excluded: bool = False) -> _StageAcc:
    acc = _stages.get(name)
    if acc is None:
        acc = _StageAcc(excluded=excluded)
        _stages[name] = acc
    if excluded:
        acc.excluded = True
    return acc


def _stack() -> List[dict]:
    st = getattr(_tls, "stack", None)
    if st is None:
        st = []
        _tls.stack = st
    return st


class _StageCtx:
    """Context manager for one stage interval (exclusive attribution)."""

    __slots__ = ("name", "excluded", "_wall0", "_cpu0", "_child_wall",
                 "_child_cpu")

    def __init__(self, name: str, excluded: bool):
        self.name = name
        self.excluded = excluded
        self._child_wall = 0.0
        self._child_cpu = 0.0

    def __enter__(self):
        self._wall0 = time.perf_counter()
        self._cpu0 = time.thread_time()
        _stack().append({"ctx": self})
        return self

    def __exit__(self, exc_type, exc, tb):
        wall = time.perf_counter() - self._wall0 - self._child_wall
        cpu = time.thread_time() - self._cpu0 - self._child_cpu
        wall = max(0.0, wall)
        cpu = max(0.0, cpu)
        st = _stack()
        st.pop()
        if st:  # charge our TOTAL interval to the parent's child-time
            parent = st[-1]["ctx"]
            parent._child_wall += (time.perf_counter() - self._wall0)
            parent._child_cpu += (time.thread_time() - self._cpu0)
        with _lock:
            acc = _acc(self.name, self.excluded)
            acc.count += 1
            acc.wall_sum += wall
            acc.cpu_sum += cpu
            if len(acc.walls) < _MAX_SAMPLES:
                acc.walls.append(wall)
            else:
                acc.dropped += 1
        return False


def stage(name: str, excluded: bool = False) -> _StageCtx:
    """Bracket a stage. Usage: `with stage_metrics.stage('decode'): ...`"""
    return _StageCtx(name, excluded)


def current_stage() -> Optional[str]:
    """Innermost active stage on THIS thread, or None."""
    st = getattr(_tls, "stack", None)
    return st[-1]["ctx"].name if st else None


def note_io(timing: Any, stage_name: Optional[str] = None) -> None:
    """Fold one IOTiming into the active (or named) stage.

    Accepts any object with .device/.host floats and an optional
    .components dict. Enforces the split invariant device+host <=
    total*1.02 when .total is present; violations are counted, the
    sample is still folded (host clamped)."""
    global _invariant_violations
    name = stage_name or current_stage()
    if name is None:
        name = "unstaged"
    device = float(getattr(timing, "device", 0.0) or 0.0)
    host = float(getattr(timing, "host", 0.0) or 0.0)
    total = getattr(timing, "total", None)
    if total is not None and device + host > float(total) * 1.02 + 1e-9:
        with _lock:
            _invariant_violations += 1
        host = max(0.0, float(total) - device)
    comps = getattr(timing, "components", None)
    with _lock:
        acc = _acc(name)
        acc.device_sum += device
        acc.host_io_sum += host
        if comps:
            for k, v in comps.items():
                acc.components[k] = acc.components.get(k, 0.0) + float(v)


def note_bytes(n: int, stage_name: Optional[str] = None) -> None:
    """A host buffer of n bytes materialized in this stage."""
    name = stage_name or current_stage() or "unstaged"
    with _lock:
        acc = _acc(name)
        acc.bytes_cur += n
        acc.bytes_cum += n
        if acc.bytes_cur > acc.bytes_peak:
            acc.bytes_peak = acc.bytes_cur


def release_bytes(n: int, stage_name: Optional[str] = None) -> None:
    name = stage_name or current_stage() or "unstaged"
    with _lock:
        acc = _acc(name)
        acc.bytes_cur = max(0, acc.bytes_cur - n)


def note_moved(host_bytes: int = 0, device_bytes: int = 0,
               stage_name: Optional[str] = None) -> None:
    """Bytes that crossed through the host / moved device-side."""
    name = stage_name or current_stage() or "unstaged"
    with _lock:
        acc = _acc(name)
        acc.bytes_moved_host += host_bytes
        acc.bytes_moved_device += device_bytes


def _pct(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1,
                   int(round(p / 100.0 * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[k]


def snapshot(toggles: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The `stage_breakdown` results block."""
    with _lock:
        out: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "toggles": dict(toggles or {}),
            "stages": {},
            "timing_invariant_violations": _invariant_violations,
        }
        attributed = 0.0
        excluded_cpu = 0.0
        for name, acc in sorted(_stages.items()):
            walls = sorted(acc.walls)
            out["stages"][name] = {
                "count": acc.count,
                "wall_s": {
                    "sum": round(acc.wall_sum, 6),
                    "mean": round(acc.wall_sum / acc.count, 6)
                    if acc.count else None,
                    "p50": _pct(walls, 50), "p95": _pct(walls, 95),
                    "p99": _pct(walls, 99),
                    "samples_dropped": acc.dropped,
                },
                "cpu_s": {
                    "sum": round(acc.cpu_sum, 6),
                    "per_req_mean": round(acc.cpu_sum / acc.count, 9)
                    if acc.count else None,
                },
                "device_s": {"sum": round(acc.device_sum, 6),
                             "components": {k: round(v, 6) for k, v in
                                            sorted(acc.components.items())}},
                "host_io_s": {"sum": round(acc.host_io_sum, 6)},
                "host_bytes": {"peak": acc.bytes_peak,
                               "cum": acc.bytes_cum},
                "bytes_moved_host": acc.bytes_moved_host,
                "bytes_moved_device": acc.bytes_moved_device,
                "excluded_from_attribution": acc.excluded,
            }
            if acc.excluded:
                excluded_cpu += acc.cpu_sum
            else:
                attributed += acc.cpu_sum
        process_cpu = time.process_time() - _process_cpu_start
        out["attributed_cpu_s"] = round(attributed, 6)
        out["excluded_cpu_s"] = round(excluded_cpu, 6)
        out["process_cpu_s_window"] = round(process_cpu, 6)
        out["unattributed_cpu_s"] = round(
            max(0.0, process_cpu - attributed - excluded_cpu), 6)
        return out


def reset() -> None:
    """Zero all accumulators and restart the process-CPU window."""
    global _stages, _process_cpu_start, _invariant_violations
    with _lock:
        _stages = {}
        _invariant_violations = 0
        _process_cpu_start = time.process_time()


def dump_json(toggles: Optional[Dict[str, Any]] = None) -> str:
    return json.dumps(snapshot(toggles), indent=2, sort_keys=True)
