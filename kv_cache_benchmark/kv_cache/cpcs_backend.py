"""
CPCS-enabled NVMe backend.

This backend preserves the StorageBackend contract while delegating
transform/load/store operations through a CPCS client.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import time
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from kv_cache import stage_metrics
from kv_cache.backends import StorageBackend
from kv_cache.cpcs_client import CPCSClient, KernelChannelCPCSClient, MockCPCSClient, SpdkPassthruCPCSClient, SpdkRpcBootstrap
from kv_cache.cpcs_metrics import CPCSMetrics

logger = logging.getLogger(__name__)


class CPCSNVMeBackend(StorageBackend):
    """Storage backend with CPCS mode support."""

    _SPDK_KV_BUILTIN_PIND_DEFAULTS: Dict[str, int] = {
        "pack_store": 7,
        "unpack_load": 8,
        "layout_repack": 9,
        "block_select": 10,
        "prefix_lookup": 11,
        "batch_read": 12,
        "migrate": 13,
    }

    def __init__(self, base_path: Optional[str], cpcs_config: Optional[Dict[str, Any]] = None):
        config = dict(cpcs_config or {})

        self.mode = str(config.get("mode", "noop"))
        self.client_type = str(config.get("client", "mock"))
        self.verify_every_n = int(config.get("verify_every_n", 0) or 0)
        self.lossy_tolerance = float(config.get("lossy_tolerance", 0.0) or 0.0)
        self.fallback_on_error = bool(config.get("fallback_on_error", False))
        self.block_size_bytes = int(config.get("block_size_kb", 1024) or 1024) * 1024
        self.batch_size = int(config.get("batch_size", 1) or 1)
        self.metrics_output_path = str(config.get("metrics_output", "") or "").strip()
        self.bootstrap_check = bool(config.get("bootstrap_check", False))
        self.required_rpc_methods = self._parse_required_methods(config.get("required_rpc_methods", ""))
        self.bootstrap_install_builtins = bool(config.get("bootstrap_install_builtins", False))
        self.bootstrap_list_programs = bool(config.get("bootstrap_list_programs", False))
        self.bootstrap_list_mrs = bool(config.get("bootstrap_list_mrs", False))
        self.auto_create_mrs = bool(config.get("auto_create_mrs", False))
        self.mrs_ranges_spec = config.get("mrs_ranges", "")
        self.mrs_default_length_bytes = int(config.get("mrs_default_length_bytes", 65536) or 65536)
        self.mrs_align_bytes = int(config.get("mrs_align_bytes", 0) or 0)
        self.mrs_align_mode = str(config.get("mrs_align_mode", "round") or "round").strip().lower()
        self._known_mrs_ranges: List[Dict[str, int]] = []
        self._execute_output_ops = {"pack_store", "unpack_load", "layout_repack"}
        self.execute_output_enable = bool(config.get("execute_output_enable", True))
        self.execute_output_mr_id = int(config.get("execute_output_mr_id", 1) or 1)
        self.execute_output_offset_bytes = int(config.get("execute_output_offset_bytes", 0) or 0)
        self.execute_output_max_bytes = int(config.get("execute_output_max_bytes", 0) or 0)
        self.load_program_path = str(config.get("load_program_path", "") or "").strip()
        self.load_program_pind = int(config.get("load_program_pind", -1) or -1)
        self.load_program_set_default_pind = bool(config.get("load_program_set_default_pind", False))
        self.load_program_chunk_bytes = int(config.get("load_program_chunk_bytes", 0) or 0)
        self.load_program_ptype = int(config.get("load_program_ptype", 0xC0) or 0xC0)
        self.load_program_pit = int(config.get("load_program_pit", 0x01) or 0x01)
        self.load_program_puid = int(config.get("load_program_puid", 0xEBF00001) or 0xEBF00001)
        self.activate_loaded_program = bool(config.get("activate_loaded_program", False))
        self.bootstrap_status: Dict[str, Any] = {}
        self.bootstrap_helper: Optional[SpdkRpcBootstrap] = None

        explicit_storage_mode = str(config.get("storage_mode", "") or "").strip().lower()
        has_arena_path = bool(str(config.get("arena_path", "") or "").strip())
        if explicit_storage_mode:
            self.storage_mode = explicit_storage_mode
        else:
            self.storage_mode = "arena" if has_arena_path else "file"
        if self.storage_mode not in ("file", "arena"):
            raise ValueError(f"Unsupported CPCS storage mode: {self.storage_mode}")

        self.temp_dir = None
        if base_path is None:
            self.temp_dir = tempfile.TemporaryDirectory(prefix="kv_cache_cpcs_")
            self.base_path = Path(self.temp_dir.name)
        else:
            self.base_path = Path(base_path)

        if self.base_path.exists():
            if not self.base_path.is_dir():
                raise NotADirectoryError(f"Cache path {self.base_path} exists but is not a directory.")
        else:
            self.base_path.mkdir(parents=True, exist_ok=True)

        self.arena_path: Optional[Path] = None
        self.index_path: Optional[Path] = None
        self._arena_next_offset = 0

        self.cpcs_nsid = int(config.get("cpcs_nsid", 200))
        self.slm_nsid = int(config.get("slm_nsid", 100))
        self.cpcs_pind = int(config.get("cpcs_program_pind", 0))
        if self.load_program_set_default_pind and self.load_program_pind >= 0:
            self.cpcs_pind = int(self.load_program_pind)
        self.cpcs_rsid = int(config.get("cpcs_rsid", 1))
        self.direct_probe_lba_bytes = int(config.get("direct_probe_lba_bytes", 4096) or 4096)
        self.slm_rw_lba_bytes = int(config.get("slm_rw_lba_bytes", self.direct_probe_lba_bytes) or self.direct_probe_lba_bytes)
        self.slm_read_address_mode = str(config.get("slm_read_address_mode", "byte") or "byte").strip().lower()
        self.slm_write_address_mode = str(config.get("slm_write_address_mode", "lba") or "lba").strip().lower()
        if self.mrs_align_mode not in {"none", "strict", "round"}:
            self.mrs_align_mode = "round"
        if self.mrs_align_bytes <= 0:
            self.mrs_align_bytes = int(max(1, self.direct_probe_lba_bytes))
        if self.slm_rw_lba_bytes <= 0:
            self.slm_rw_lba_bytes = int(max(1, self.direct_probe_lba_bytes))
        if self.slm_read_address_mode not in {"byte", "lba"}:
            self.slm_read_address_mode = "byte"
        if self.slm_write_address_mode not in {"byte", "lba"}:
            self.slm_write_address_mode = "lba"
        if self.execute_output_mr_id <= 0:
            self.execute_output_mr_id = 1
        if self.execute_output_offset_bytes < 0:
            self.execute_output_offset_bytes = 0
        if self.execute_output_max_bytes < 0:
            self.execute_output_max_bytes = 0
        self._load_known_mrs_ranges_from_config()

        self.metadata: Dict[str, Dict[str, Any]] = {}
        self.metrics = CPCSMetrics()
        self.client = self._build_client(config)
        self.client.ensure_runtime_ready()
        self._run_bootstrap_checks(config)

        self.cpcs_pind_by_op = self._build_pind_map(config)
        self.cpcs_rsid_by_op = self._build_rsid_map(config)

        self.kv_flow = str(config.get("kv_flow", "") or "").strip().lower()
        if self.kv_flow not in ("", "hostnvm", "pslm", "vslm"):
            raise ValueError(f"Unsupported kv_flow: {self.kv_flow}")
        self.nvm_nsid = int(config.get("nvm_nsid", 2) or 2)
        self.pslm_nsid = int(config.get("pslm_nsid", 101) or 101)
        self.kv_exec_max_bytes = int(config.get("kv_exec_max_bytes", 2 << 20) or (2 << 20))
        self.kv_vslm_arena_bytes = int(config.get("kv_vslm_arena_mb", 1024) or 1024) * 1024 * 1024
        self.kv_scratch_bytes = int(config.get("kv_scratch_mb", 64) or 64) * 1024 * 1024
        self.kv_pslm_mr_bytes = int(config.get("kv_pslm_mr_mb", 64) or 64) * 1024 * 1024
        self._nvm_next_offset = 0
        # Extent reuse for the flow stores. Bump-only allocation exhausts
        # ANY arena size under demote churn (each re-demotion of a block
        # allocated fresh space); the cache calls delete(key) whenever an
        # entry leaves a tier, so freed extents are recycled first-fit.
        # No splitting: serving KV blocks are uniform enough that whole-
        # extent reuse keeps fragmentation negligible. The vslm arena is
        # per-SLOT since 08-31 (per-worker RSIDs): free lists and bump
        # cursors are keyed by slot; offsets stay GLOBAL arena-relative.
        self._vslm_arena_next: Dict[int, int] = {}  # slot -> bump cursor
        self._flow_free = {"vslm": {}, "nvm": []}   # vslm: slot -> [(off, len)]
        self._flow_resv = {"vslm": {}, "nvm": {}}   # pool -> {off: len}
        # Concurrency (08-24 cluster finding, VM-reproduced): serving runs
        # this backend from MANY worker threads.
        #  - _flow_alloc_lock: the free-list scan/del races unlocked --
        #    two threads can claim the SAME extent (data corruption).
        #  - _exec_lock: firmware 46795d731 leases EVERY resolved range of
        #    the RSID in full per Execute (execute.c: acquire loop over
        #    resolved_ranges), so concurrent executes on one MRS are
        #    refused with MEMORY_RANGE_SET_IN_USE (01/95). Serialize
        #    client-side: waiting is cheaper than a ~400 ms spawn that
        #    gets rejected. Device exec parallelism per MRS is 1 BY
        #    FIRMWARE DESIGN -- disclose, don't fight.
        import threading as _threading
        self._flow_alloc_lock = _threading.Lock()
        # Per-worker RSIDs (08-31 tuning lever): firmware leases are
        # PER-RANGE per lease_id (execute.c), so N MRSes with disjoint
        # (4 KiB-page-disjoint on vSLM) ranges execute CONCURRENTLY on
        # distinct compute threads. N=1 keeps the legacy single-RSID
        # serialization. Worker threads are assigned slots round-robin
        # on first use; each slot gets its own execute lock and (vslm)
        # its own arena slice + allocator.
        self.kv_flow_rsids = max(1, int(config.get("kv_flow_rsids", 1) or 1))
        self._exec_locks = [_threading.Lock()
                            for _ in range(self.kv_flow_rsids)]
        self._exec_lock = self._exec_locks[0]  # legacy alias (N=1 path)
        self._slot_local = _threading.local()
        self._slot_next = 0
        self._slot_lock = _threading.Lock()
        self._flow_rsid = 0
        self._flow_rsids: List[int] = []
        self._flow_ranges: List[Dict[str, int]] = []
        # flow-attributable initiator memory accounting (08-31, the
        # figure-(b) metric): output bytes MATERIALIZED host-side
        # (hostnvm transform product), bytes READ BACK device->host
        # (pslm copy-out + decode read-backs), and a max-updated
        # per-request working-buffer peak. WRITE-direction is the
        # headline; read-path read-backs are counted separately so the
        # claim stays honest (vslm also reads back on decode).
        self._flow_output_bytes_total = 0
        self._flow_readback_bytes_total = 0
        self._flow_read_readback_bytes_total = 0
        self._flow_workbuf_peak_bytes = 0
        self._flow_mem_requests_total = 0
        # 09-03: the hostnvm arm's transform-kernel wall time. Measured
        # since 08-31 but previously DROPPED (never reported).
        self._flow_host_kernel_s_total = 0.0
        # 09-03: per-stage offload toggles + decode read policy (S2/S4).
        self.offload_stages = dict(
            (cpcs_config or {}).get('offload_stages') or {})
        self.decode_read_policy = str(
            (cpcs_config or {}).get('decode_read_policy') or 'full')
        # SLM-source device-path accounting (P3): executes taken, and
        # graceful fallbacks to the per-chunk host path (each logged).
        self._slm_device_execs = 0
        self._slm_device_fallbacks = 0
        self._batch_local = _threading.local()
        if self.kv_flow in ("pslm", "vslm"):
            self._flow_bootstrap_mrs()

        self._verify_counter = 0
        self._initialize_storage_layout(config)

    @staticmethod
    def _resolve_id_override(value: Any, fallback: int, min_value: int) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = -1

        base = int(fallback)
        if parsed < 0:
            parsed = base
        if parsed < min_value:
            return int(max(min_value, base))
        return int(parsed)

    @classmethod
    def _resolve_pind_override(cls, value: Any, fallback: int) -> int:
        return cls._resolve_id_override(value, fallback, 0)

    @classmethod
    def _resolve_rsid_override(cls, value: Any, fallback: int) -> int:
        return cls._resolve_id_override(value, fallback, 1)

    def _build_pind_map(self, config: Dict[str, Any]) -> Dict[str, int]:
        default_pind = int(self.cpcs_pind)
        if self.client_type == "spdk_passthru":
            default_pind = int(max(0, self._SPDK_KV_BUILTIN_PIND_DEFAULTS["pack_store"]))
        return {
            "pack_store": self._resolve_pind_override(
                config.get("cpcs_program_pind_pack_store", -1),
                self._SPDK_KV_BUILTIN_PIND_DEFAULTS["pack_store"] if self.client_type == "spdk_passthru" else default_pind,
            ),
            "unpack_load": self._resolve_pind_override(
                config.get("cpcs_program_pind_unpack_load", -1),
                self._SPDK_KV_BUILTIN_PIND_DEFAULTS["unpack_load"] if self.client_type == "spdk_passthru" else default_pind,
            ),
            "layout_repack": self._resolve_pind_override(
                config.get("cpcs_program_pind_layout_repack", -1),
                self._SPDK_KV_BUILTIN_PIND_DEFAULTS["layout_repack"] if self.client_type == "spdk_passthru" else default_pind,
            ),
            "block_select": self._resolve_pind_override(
                config.get("cpcs_program_pind_block_select", -1),
                self._SPDK_KV_BUILTIN_PIND_DEFAULTS["block_select"] if self.client_type == "spdk_passthru" else default_pind,
            ),
            "prefix_lookup": self._resolve_pind_override(
                config.get("cpcs_program_pind_prefix_lookup", -1),
                self._SPDK_KV_BUILTIN_PIND_DEFAULTS["prefix_lookup"] if self.client_type == "spdk_passthru" else default_pind,
            ),
            "batch_read": self._resolve_pind_override(
                config.get("cpcs_program_pind_batch_read", -1),
                self._SPDK_KV_BUILTIN_PIND_DEFAULTS["batch_read"] if self.client_type == "spdk_passthru" else default_pind,
            ),
        }

    def _build_rsid_map(self, config: Dict[str, Any]) -> Dict[str, int]:
        return {
            "pack_store": self._resolve_rsid_override(config.get("cpcs_rsid_pack_store", -1), self.cpcs_rsid),
            "unpack_load": self._resolve_rsid_override(config.get("cpcs_rsid_unpack_load", -1), self.cpcs_rsid),
            "layout_repack": self._resolve_rsid_override(config.get("cpcs_rsid_layout_repack", -1), self.cpcs_rsid),
            "block_select": self._resolve_rsid_override(config.get("cpcs_rsid_block_select", -1), self.cpcs_rsid),
            "prefix_lookup": self._resolve_rsid_override(config.get("cpcs_rsid_prefix_lookup", -1), self.cpcs_rsid),
            "batch_read": self._resolve_rsid_override(config.get("cpcs_rsid_batch_read", -1), self.cpcs_rsid),
        }

    def _resolve_program_binding(self, op: str) -> Tuple[int, int]:
        op_name = str(op)
        rsid = int(self.cpcs_rsid_by_op.get(op_name, self.cpcs_rsid))
        pind = int(self.cpcs_pind_by_op.get(op_name, self.cpcs_pind))
        return int(max(1, rsid)), int(max(0, pind))

    @staticmethod
    def _prefix_token_from_key(key: str) -> str:
        key_text = str(key)
        if ":" in key_text:
            return key_text.split(":", 1)[0]
        if "/" in key_text:
            return key_text.split("/", 1)[0]
        return key_text[:32]

    def _load_metadata_hint(self, key: str) -> Dict[str, Any]:
        if key in self.metadata:
            return dict(self.metadata[key])

        if self.storage_mode != "file":
            return {}

        meta_path = self._meta_path(key)
        if not meta_path.exists():
            return {}
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                self.metadata[key] = dict(meta)
                return dict(meta)
        except Exception:
            return {}
        return {}

    def _command_profile(
        self,
        *,
        phase: str,
        key: str,
        mode: str,
        payload_bytes: int,
        shape: Tuple[int, ...] = (),
        dtype: str = "",
        meta: Optional[Dict[str, Any]] = None,
        keys: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        phase_name = str(phase)
        mode_name = str(mode)
        op = "pack_store" if phase_name == "write" else "unpack_load"
        metrics_mode = mode_name
        extra: Dict[str, Any] = {
            "phase": phase_name,
            "cache_key": str(key),
            "payload_bytes": int(max(0, payload_bytes)),
            "storage_mode": self.storage_mode,
        }

        if shape:
            extra["shape"] = [int(x) for x in shape]
        if dtype:
            extra["dtype"] = str(dtype)

        if mode_name == "layout":
            if phase_name == "batch":
                op = "batch_read"
            else:
                op = "layout_repack"
            extra.update(
                {
                    "layout_block_size_bytes": int(self.block_size_bytes),
                    "layout_batch_size_hint": int(self.batch_size),
                }
            )
        elif mode_name == "block_select":
            if phase_name == "read":
                op = "block_select"
                total_blocks = (int(max(payload_bytes, 1)) + self.block_size_bytes - 1) // self.block_size_bytes
                selected_blocks = min(max(1, int(self.batch_size)), max(1, int(total_blocks)))
                extra.update(
                    {
                        "selector_total_blocks": int(total_blocks),
                        "selector_selected_blocks": int(selected_blocks),
                        "selector_block_size_bytes": int(self.block_size_bytes),
                    }
                )
            elif phase_name == "batch":
                op = "batch_read"
            else:
                op = "pack_store"
                extra["selector_block_size_bytes"] = int(self.block_size_bytes)
        elif mode_name == "prefix_index":
            prefix = self._prefix_token_from_key(key)
            if phase_name == "read":
                op = "prefix_lookup"
                extra["prefix_op"] = "lookup"
            elif phase_name == "batch":
                op = "batch_read"
                extra["prefix_op"] = "batch_lookup"
            else:
                op = "pack_store"
                extra["prefix_op"] = "upsert"
            extra["prefix_token"] = prefix

        if meta:
            if "packed_size" in meta:
                extra["meta_packed_size"] = int(meta.get("packed_size", 0) or 0)
            if "raw_size" in meta:
                extra["meta_raw_size"] = int(meta.get("raw_size", 0) or 0)
            if "arena_offset" in meta:
                extra["arena_offset"] = int(meta.get("arena_offset", 0) or 0)
            if "arena_length" in meta:
                extra["arena_length"] = int(meta.get("arena_length", 0) or 0)

        if keys:
            extra["batch_key_count"] = int(len(keys))

        return {
            "op": op,
            "mode": mode_name,
            "extra": extra,
            "metrics_mode": metrics_mode,
        }

    def _issue_batch_descriptor(self, keys: List[str]) -> None:
        if len(keys) <= 1:
            return
        mode_name = str(self.mode)
        if mode_name not in {"layout", "block_select", "prefix_index"}:
            return

        entries: List[Dict[str, Any]] = []
        for key in keys:
            meta = self._load_metadata_hint(str(key))
            shape_val = meta.get("shape", [])
            shape_list: List[int] = []
            if isinstance(shape_val, (list, tuple)):
                shape_list = [int(x) for x in shape_val]
            item: Dict[str, Any] = {
                "key": str(key),
                "mode": str(meta.get("mode", mode_name)),
                "shape": shape_list,
                "dtype": str(meta.get("dtype", "")),
                "packed_size": int(meta.get("packed_size", 0) or 0),
                "raw_size": int(meta.get("raw_size", 0) or 0),
                "storage_mode": str(meta.get("storage_mode", self.storage_mode)),
                "prefix_token": self._prefix_token_from_key(str(key)),
            }
            if "arena_offset" in meta:
                item["arena_offset"] = int(meta.get("arena_offset", 0) or 0)
            if "arena_length" in meta:
                item["arena_length"] = int(meta.get("arena_length", 0) or 0)
            entries.append(item)

        descriptor = {
            "version": 1,
            "mode": mode_name,
            "batch_size_hint": int(self.batch_size),
            "entries": entries,
        }
        payload = json.dumps(descriptor, separators=(",", ":"), sort_keys=True).encode("utf-8")
        profile = self._command_profile(
            phase="batch",
            key="batch",
            mode=mode_name,
            payload_bytes=len(payload),
            dtype="json",
            keys=keys,
        )

        try:
            rsid, pind = self._resolve_program_binding(str(profile["op"]))
            exec_extra = dict(profile["extra"])
            exec_extra["program_rsid"] = int(rsid)
            exec_extra["program_pind"] = int(pind)
            result = self.client.execute(
                cpcs_nsid=self.cpcs_nsid,
                rsid=rsid,
                pind=pind,
                payload=payload,
                op=str(profile["op"]),
                mode=str(profile["mode"]),
                shape=(len(entries),),
                dtype="uint8",
                extra=exec_extra,
            )
            self._record_metrics(result, str(profile["metrics_mode"]))
        except Exception:
            if not self.fallback_on_error:
                raise
            self._record_command_failure(str(profile["metrics_mode"]), len(payload))

    @staticmethod
    def _parse_required_methods(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [m.strip() for m in value.split(",") if m.strip()]
        if isinstance(value, (list, tuple)):
            return [str(m).strip() for m in value if str(m).strip()]
        return []

    @staticmethod
    def _to_int(value: Any) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, np.integer)):
            return int(value)
        text = str(value).strip()
        if not text:
            return 0
        return int(text, 0)

    @staticmethod
    def _parse_mrs_ranges_spec(spec: Any) -> List[Dict[str, int]]:
        if spec is None:
            return []
        if isinstance(spec, (list, tuple)):
            raw_items = list(spec)
        else:
            text = str(spec or "").strip()
            if not text:
                return []
            if text.startswith("["):
                parsed = json.loads(text)
                if not isinstance(parsed, list):
                    raise ValueError("mrs_ranges JSON must be a list")
                raw_items = list(parsed)
            else:
                raw_items = []
                for chunk in text.split(","):
                    item = chunk.strip()
                    if not item:
                        continue
                    if ":" not in item:
                        raise ValueError(f"Invalid mrs range token: {item}")
                    off_text, len_text = item.split(":", 1)
                    raw_items.append({"offset": CPCSNVMeBackend._to_int(off_text), "length": CPCSNVMeBackend._to_int(len_text)})

        out: List[Dict[str, int]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise ValueError("Each mrs range entry must be a mapping")
            start = CPCSNVMeBackend._to_int(item.get("starting_byte", item.get("offset", 0)) or 0)
            length = CPCSNVMeBackend._to_int(item.get("length", item.get("length_bytes", 0)) or 0)
            if start < 0:
                raise ValueError(f"MRS range offset must be >= 0, got {start}")
            if length <= 0:
                raise ValueError(f"MRS range length must be > 0, got {length}")
            out.append({"starting_byte": int(start), "length": int(length)})
        return out

    def _default_mrs_ranges(self) -> List[Dict[str, int]]:
        default_len = max(
            int(self.mrs_default_length_bytes),
            int(self.block_size_bytes),
            int(max(4096, self.direct_probe_lba_bytes)),
        )
        return [{"starting_byte": 0, "length": int(default_len)}]

    def _normalize_mrs_ranges(self, ranges: List[Dict[str, int]]) -> Tuple[List[Dict[str, int]], bool]:
        mode = str(self.mrs_align_mode)
        align = int(max(1, self.mrs_align_bytes))
        if mode == "none" or align <= 1:
            return list(ranges), False

        out: List[Dict[str, int]] = []
        adjusted = False
        for item in ranges:
            start = int(item.get("starting_byte", item.get("offset", 0)) or 0)
            length = int(item.get("length", item.get("length_bytes", 0)) or 0)
            end = start + length
            if mode == "strict":
                if (start % align) != 0 or (length % align) != 0:
                    raise ValueError(
                        f"MRS range is not aligned (start={start}, length={length}, align={align})"
                    )
                out.append({"starting_byte": start, "length": length})
                continue

            aligned_start = (start // align) * align
            aligned_end = self._align_up(end, align)
            aligned_len = int(aligned_end - aligned_start)
            if aligned_start != start or aligned_len != length:
                adjusted = True
            out.append({"starting_byte": int(aligned_start), "length": int(aligned_len)})
        return out, adjusted

    def _set_known_mrs_ranges(self, ranges: List[Dict[str, int]]) -> None:
        normalized: List[Dict[str, int]] = []
        for item in ranges:
            start = int(item.get("starting_byte", item.get("offset", 0)) or 0)
            length = int(item.get("length", item.get("length_bytes", 0)) or 0)
            if start < 0 or length <= 0:
                continue
            normalized.append({"starting_byte": int(start), "length": int(length)})
        self._known_mrs_ranges = normalized

    def _load_known_mrs_ranges_from_config(self) -> None:
        try:
            ranges = self._parse_mrs_ranges_spec(self.mrs_ranges_spec)
        except Exception:
            return
        if not ranges:
            if not self.auto_create_mrs:
                return
            ranges = self._default_mrs_ranges()
        ranges, _ = self._normalize_mrs_ranges(ranges)
        self._set_known_mrs_ranges(ranges)

    def _build_execute_output_target(
        self,
        op: str,
        rsid: int,
        payload_bytes: int,
        exec_extra: Dict[str, Any],
    ) -> Optional[Dict[str, int]]:
        if self.client_type != "spdk_passthru" or not self.execute_output_enable:
            return None
        if str(op) not in self._execute_output_ops:
            return None
        if int(rsid) != int(self.cpcs_rsid):
            return None
        if not self._known_mrs_ranges:
            return None

        mr_id = int(max(1, self.execute_output_mr_id))
        mr_index = mr_id - 1
        if mr_index < 0 or mr_index >= len(self._known_mrs_ranges):
            return None

        mr = self._known_mrs_ranges[mr_index]
        mr_off = int(max(0, self.execute_output_offset_bytes))
        mr_len = int(mr["length"])
        if mr_off >= mr_len:
            return None

        max_bytes = int(mr_len - mr_off)
        if self.execute_output_max_bytes > 0:
            max_bytes = int(min(max_bytes, self.execute_output_max_bytes))
        if max_bytes <= 0:
            return None

        # Skip output targeting when payload is clearly larger than available MR capacity.
        if payload_bytes > 0 and int(payload_bytes) > int(max_bytes):
            return None

        exec_extra["output_mr_id"] = int(mr_id)
        exec_extra["output_offset"] = int(mr_off)
        exec_extra["output_length"] = int(max_bytes)
        exec_extra["output_contract"] = "mr_range"

        return {
            "mr_id": int(mr_id),
            "mr_offset": int(mr_off),
            "max_bytes": int(max_bytes),
            "slm_offset": int(mr["starting_byte"] + mr_off),
        }

    def _read_slm_payload(self, *, offset: int, length: int, metrics_mode: str) -> bytes:
        if length <= 0:
            return b""

        if self.slm_read_address_mode == "lba":
            lba = int(max(1, self.slm_rw_lba_bytes))
            aligned_offset = (int(offset) // lba) * lba
            head = int(offset - aligned_offset)
            aligned_length = self._align_up(int(max(1, head + length)), lba)
            read_t0 = time.perf_counter()
            raw = self.client.slm_read(
                slm_nsid=self.slm_nsid,
                offset_bytes=aligned_offset,
                length_bytes=aligned_length,
                address_mode="lba",
                lba_bytes=lba,
            )
            self._record_slm_read_metrics(metrics_mode, aligned_length, time.perf_counter() - read_t0)
            payload = raw[head : head + length]
        else:
            read_t0 = time.perf_counter()
            payload = self.client.slm_read(
                slm_nsid=self.slm_nsid,
                offset_bytes=int(offset),
                length_bytes=int(length),
                address_mode="byte",
            )
            self._record_slm_read_metrics(metrics_mode, length, time.perf_counter() - read_t0)

        if len(payload) != int(length):
            raise IOError(f"Short SLM read for execute output: expected {length}, got {len(payload)}")
        return bytes(payload)

    def _consume_execute_output_payload(
        self,
        *,
        op: str,
        result: Any,
        target: Optional[Dict[str, int]],
        metrics_mode: str,
    ) -> Optional[bytes]:
        if target is None:
            return None
        if not isinstance(getattr(result, "extra", None), dict):
            return None

        extra = result.extra
        result_raw = int(extra.get("result_raw", extra.get("result_dw0", 0)) or 0)
        result_value = int(extra.get("result_value", result_raw & 0xFFFFFFFF) or 0)
        result_crc = int(extra.get("result_crc32", (result_raw >> 32) & 0xFFFFFFFF) or 0)
        if result_value <= 0:
            return None
        if result_value > int(target["max_bytes"]):
            extra["output_mr_truncated"] = True
            return None

        payload = self._read_slm_payload(
            offset=int(target["slm_offset"]),
            length=int(result_value),
            metrics_mode=f"{metrics_mode}_exec_output_read",
        )
        host_crc = int(zlib.crc32(payload) & 0xFFFFFFFF)
        if result_crc != 0 and host_crc != result_crc:
            extra["output_mr_crc_mismatch"] = True
            extra["output_mr_crc_host"] = int(host_crc)
            extra["output_mr_crc_device"] = int(result_crc)
            return None

        extra["output_mr_consumed"] = True
        extra["output_mr_id"] = int(target["mr_id"])
        extra["output_mr_offset"] = int(target["mr_offset"])
        extra["output_payload_bytes"] = int(len(payload))
        extra["output_crc_host"] = int(host_crc)
        if result_crc != 0:
            extra["output_crc_match"] = True
        return payload

    def _bootstrap_fail(self, message: str) -> None:
        if self.fallback_on_error:
            self.bootstrap_status["ok"] = False
            self.bootstrap_status["warning"] = str(message)
            return
        raise RuntimeError(str(message))

    def _maybe_create_mrs(self) -> None:
        if not self.auto_create_mrs:
            return
        try:
            ranges = self._parse_mrs_ranges_spec(self.mrs_ranges_spec)
        except Exception as exc:
            self._bootstrap_fail(f"Failed parsing --cpcs-mrs-ranges: {exc}")
            return

        if not ranges:
            ranges = self._default_mrs_ranges()
        ranges, adjusted = self._normalize_mrs_ranges(ranges)

        rsid = self.client.create_mrs(
            self.slm_nsid,
            ranges,
            cpcs_nsid=self.cpcs_nsid,
        )
        self.cpcs_rsid = int(max(1, rsid))
        self._set_known_mrs_ranges(ranges)
        self.bootstrap_status["mrs"] = {
            "auto_create": True,
            "rsid": int(self.cpcs_rsid),
            "ranges": ranges,
            "align_mode": str(self.mrs_align_mode),
            "align_bytes": int(self.mrs_align_bytes),
            "ranges_adjusted": bool(adjusted),
        }

    def _maybe_load_and_activate_program(self) -> None:
        if not self.load_program_path:
            return
        program_path = Path(self.load_program_path).expanduser().resolve()
        if not program_path.exists():
            self._bootstrap_fail(f"CPCS program file not found: {program_path}")
            return

        program_bytes = program_path.read_bytes()
        pind = int(self.load_program_pind if self.load_program_pind >= 0 else self.cpcs_pind)
        load_info = self.client.load_program(
            cpcs_nsid=self.cpcs_nsid,
            pind=pind,
            program_bytes=program_bytes,
            ptype=int(self.load_program_ptype),
            pit=int(self.load_program_pit),
            puid=int(self.load_program_puid),
            chunk_bytes=int(self.load_program_chunk_bytes),
        )
        self.bootstrap_status["program_load"] = {
            "path": str(program_path),
            "pind": int(pind),
            "set_default_pind": bool(self.load_program_set_default_pind),
            "chunk_bytes": int(self.load_program_chunk_bytes),
            "ptype": int(self.load_program_ptype),
            "pit": int(self.load_program_pit),
            "puid": int(self.load_program_puid),
            "result": load_info,
        }
        if self.activate_loaded_program:
            activate_info = self.client.activate_program(cpcs_nsid=self.cpcs_nsid, pind=pind)
            self.bootstrap_status["program_activate"] = activate_info

    def _initialize_storage_layout(self, config: Dict[str, Any]) -> None:
        if self.storage_mode == "file":
            for entry in self.base_path.glob("*.cpcs"):
                entry.unlink(missing_ok=True)
            for entry in self.base_path.glob("*.cpcs.meta.json"):
                entry.unlink(missing_ok=True)
            return

        arena_text = str(config.get("arena_path", "") or "").strip()
        index_text = str(config.get("index_path", "") or "").strip()
        self.arena_path = Path(arena_text) if arena_text else (self.base_path / "cpcs_arena.bin")
        self.index_path = Path(index_text) if index_text else (self.base_path / "cpcs_index.json")

        self.arena_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        # Benchmark runs should start from a clean CPCS arena/index state.
        self.arena_path.write_bytes(b"")
        self.index_path.write_text("{}", encoding="utf-8")
        self.metadata.clear()
        self._arena_next_offset = 0

    def _build_client(self, config: Dict[str, Any]) -> CPCSClient:
        if self.client_type == "mock":
            return MockCPCSClient(mode=self.mode)
        if self.client_type != "spdk_passthru":
            raise ValueError(f"Unsupported CPCS client: {self.client_type}")

        # kernel_channel: same client, spawn funnel rerouted to the
        # persistent kernel-initiator channel (09-01 tuning lever).
        cls = (KernelChannelCPCSClient
               if bool(config.get("kernel_channel"))
               else SpdkPassthruCPCSClient)
        return cls(
            spdk_nvme_passthru=str(config.get("spdk_nvme_passthru", "")),
            trtype=str(config.get("trtype", "TCP")),
            traddr=str(config.get("traddr", "")),
            trsvcid=str(config.get("trsvcid", "")),
            subnqn=str(config.get("subnqn", "")),
            hostnqn=str(config.get("hostnqn", "")),
            src_addr=str(config.get("src_addr", "")),
            src_svcid=str(config.get("src_svcid", "")),
            passthru_lcores=str(config.get("passthru_lcores", "1")),
            dataset_nsid=int(config.get("dataset_nsid", 1)),
            direct_probe_offset=int(config.get("direct_probe_offset", 0)),
            direct_probe_length=int(config.get("direct_probe_length", 4096)),
            direct_probe_lba_bytes=int(config.get("direct_probe_lba_bytes", 4096)),
        )

    def _run_bootstrap_checks(self, config: Dict[str, Any]) -> None:
        if self.client_type != "spdk_passthru":
            return

        if self.bootstrap_check or self.bootstrap_install_builtins or self.bootstrap_list_programs or self.bootstrap_list_mrs:
            self.bootstrap_helper = SpdkRpcBootstrap(
                rpc_script=str(config.get("spdk_rpc_script", "scripts/rpc.py")),
                rpc_python=str(config.get("spdk_rpc_python", "python3")),
                rpc_socket=str(config.get("spdk_rpc_socket", "")),
            )

        try:
            self.bootstrap_status = {"ok": True}

            if self.bootstrap_check:
                if not self.bootstrap_helper:
                    self._bootstrap_fail("Bootstrap helper is not initialized")
                    return
                summary = self.bootstrap_helper.probe_runtime()
                self.bootstrap_status["probe"] = summary

                if self.required_rpc_methods:
                    check = self.bootstrap_helper.verify_methods(self.required_rpc_methods)
                    self.bootstrap_status["required_methods"] = check
                    if not bool(check.get("ok", False)):
                        self._bootstrap_fail(f"Missing required SPDK RPC methods: {check.get('missing', [])}")

            subsystem_nqn = str(config.get("bootstrap_subsystem_nqn", "") or "").strip()
            if not subsystem_nqn:
                subsystem_nqn = str(config.get("subnqn", "") or "").strip()

            if self.bootstrap_install_builtins:
                if not self.bootstrap_helper:
                    self._bootstrap_fail("bootstrap_install_builtins requires RPC bootstrap helper")
                elif not subsystem_nqn:
                    self._bootstrap_fail("bootstrap_install_builtins requires subsystem NQN (use --subnqn)")
                else:
                    out = self.bootstrap_helper.install_builtins(subsystem_nqn, self.cpcs_nsid)
                    self.bootstrap_status["install_builtins"] = out

            if self.bootstrap_list_programs:
                if not self.bootstrap_helper:
                    self._bootstrap_fail("bootstrap_list_programs requires RPC bootstrap helper")
                elif not subsystem_nqn:
                    self._bootstrap_fail("bootstrap_list_programs requires subsystem NQN (use --subnqn)")
                else:
                    out = self.bootstrap_helper.list_programs(subsystem_nqn, self.cpcs_nsid)
                    self.bootstrap_status["program_list"] = out.get("json", out.get("raw", out))

            if self.bootstrap_list_mrs:
                if not self.bootstrap_helper:
                    self._bootstrap_fail("bootstrap_list_mrs requires RPC bootstrap helper")
                elif not subsystem_nqn:
                    self._bootstrap_fail("bootstrap_list_mrs requires subsystem NQN (use --subnqn)")
                else:
                    out = self.bootstrap_helper.list_mrs(subsystem_nqn, self.cpcs_nsid)
                    self.bootstrap_status["mrs_list"] = out.get("json", out.get("raw", out))

            self._maybe_create_mrs()
            self._maybe_load_and_activate_program()
        except Exception as exc:
            self.bootstrap_status = {"ok": False, "error": str(exc)}
            if not self.fallback_on_error:
                raise

    def _path(self, key: str) -> Path:
        return self.base_path / f"{key}.cpcs"

    def _meta_path(self, key: str) -> Path:
        return self.base_path / f"{key}.cpcs.meta.json"

    @staticmethod
    def _serialize_array(data: np.ndarray) -> bytes:
        buf = io.BytesIO()
        np.save(buf, data, allow_pickle=False)
        return buf.getvalue()

    @staticmethod
    def _deserialize_array(payload: bytes) -> np.ndarray:
        return np.load(io.BytesIO(payload), allow_pickle=False)

    @staticmethod
    def _align_up(value: int, align: int) -> int:
        if align <= 1:
            return int(value)
        return ((int(value) + int(align) - 1) // int(align)) * int(align)

    def _persist_metrics_output(self) -> None:
        if not self.metrics_output_path:
            return
        path = Path(self.metrics_output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.get_metrics_summary()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _persist_arena_index(self) -> None:
        if self.storage_mode != "arena" or not self.index_path:
            return
        serializable: Dict[str, Dict[str, Any]] = {}
        for key, meta in self.metadata.items():
            item = dict(meta)
            shape = item.get("shape")
            if isinstance(shape, tuple):
                item["shape"] = list(shape)
            serializable[str(key)] = item
        self.index_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")

    def _use_spdk_arena_io(self) -> bool:
        return self.storage_mode == "arena" and self.client_type == "spdk_passthru"

    def _record_metrics(self, result, mode: str) -> None:
        self.metrics.record(
            ok=str(result.status).lower() == "ok",
            bytes_in=int(result.bytes_in),
            bytes_out=int(result.bytes_out),
            command_latency_s=float(result.command_latency_s),
            device_compute_us=float(result.device_compute_us),
            mode=mode,
            extra=result.extra,
        )
        self._persist_metrics_output()

    def _record_command_failure(self, mode: str, bytes_in: int) -> None:
        self.metrics.record(
            ok=False,
            bytes_in=int(bytes_in),
            bytes_out=0,
            command_latency_s=0.0,
            device_compute_us=0.0,
            mode=mode,
            extra={"error_count": 1},
        )
        self._persist_metrics_output()

    def _record_slm_read_metrics(self, mode: str, wire_bytes: int, elapsed_s: float) -> None:
        self.metrics.record(
            ok=True,
            bytes_in=0,
            bytes_out=int(max(0, wire_bytes)),
            command_latency_s=float(max(0.0, elapsed_s)),
            device_compute_us=float(max(0.0, elapsed_s)) * 1_000_000.0,
            mode=mode,
            extra={"media_read_bytes": int(max(0, wire_bytes))},
        )
        self._persist_metrics_output()

    # ------------------------------------------------------------------ A1W
    # kv_flow arms (2026-08-06): hostnvm / pslm / vslm. All three end with
    # the packed block durable in device storage; they differ in WHERE the
    # transform runs and WHO moves the result:
    #   hostnvm: host kernel (kv_kernels) burns initiator CPU, one NVMe
    #            WRITE per chunk to the NVM kvstore ns (nvm_nsid).
    #   pslm:    inline Execute per chunk with output into the BOUNDED
    #            pSLM scratch MRS range (chunk cap = kv_pslm_mr_bytes --
    #            the capacity axis), host reads the output back and writes
    #            it to the NVM kvstore ns (host-mediated copy-out, charged).
    #   vslm:    inline Execute per chunk with output into the vSLM arena
    #            MRS range; eager publish writes through to the backing ns
    #            at command completion -- no host copy-out.
    # MRS layout (built by _flow_bootstrap_mrs): range 1 = decode/transform
    # scratch, range 2 (vslm only) = arena. Execute output targeting uses
    # extra {output_mr_id, output_offset, output_length}; the device packs
    # out_len in CQE DW0 and crc32 in DW1 (checked on every read-back).

    # --- per-worker slot geometry (N = kv_flow_rsids; N=1 == legacy) ----
    _SLOT_PAGE = 4096  # vSLM lease-overlap granularity (page-rounded)

    def _slot_scratch(self, slot: int) -> tuple:
        n = self.kv_flow_rsids
        size = self._align_down(self.kv_scratch_bytes // n, self._SLOT_PAGE)
        return slot * size, size

    def _slot_arena(self, slot: int) -> tuple:
        """(global arena-relative base, slice length) for a slot."""
        n = self.kv_flow_rsids
        size = self._align_down(self.kv_vslm_arena_bytes // n,
                                self._SLOT_PAGE)
        return slot * size, size

    def _slot_pslm(self, slot: int) -> tuple:
        n = self.kv_flow_rsids
        size = self._align_down(self.kv_pslm_mr_bytes // n, self._SLOT_PAGE)
        return slot * size, size

    @staticmethod
    def _align_down(v: int, a: int) -> int:
        return (int(v) // a) * a

    def _flow_slot(self) -> int:
        """This worker thread's slot (round-robin on first use)."""
        slot = getattr(self._slot_local, "slot", None)
        if slot is None:
            with self._slot_lock:
                slot = self._slot_next % self.kv_flow_rsids
                self._slot_next += 1
            self._slot_local.slot = slot
        return slot

    def _flow_bootstrap_mrs(self) -> None:
        n = self.kv_flow_rsids
        mrs_list = []
        for slot in range(n):
            if self.kv_flow == "vslm":
                s_base, s_len = self._slot_scratch(slot)
                a_base, a_len = self._slot_arena(slot)
                ranges = [
                    {"nsid": self.slm_nsid, "starting_byte": s_base,
                     "length": s_len},
                    {"nsid": self.slm_nsid,
                     "starting_byte": self.kv_scratch_bytes + a_base,
                     "length": a_len},
                ]
            else:  # pslm
                p_base, p_len = self._slot_pslm(slot)
                ranges = [
                    {"nsid": self.pslm_nsid, "starting_byte": p_base,
                     "length": p_len},
                ]
            rsid = self.client.create_mrs(self.slm_nsid, ranges,
                                          cpcs_nsid=self.cpcs_nsid)
            self._flow_rsids.append(int(max(1, rsid)))
            mrs_list.append({"flow": self.kv_flow, "slot": slot,
                             "rsid": self._flow_rsids[-1],
                             "ranges": ranges})
        self._flow_rsid = self._flow_rsids[0]
        self._flow_ranges = mrs_list[0]["ranges"]
        self.bootstrap_status["kv_flow_mrs"] = (
            mrs_list[0] if n == 1 else mrs_list)

    def _flow_chunk_bytes(self) -> int:
        cap = int(self.kv_exec_max_bytes)
        if self.kv_flow == "pslm":
            cap = min(cap, self._slot_pslm(0)[1])
        return max(cap, self.slm_rw_lba_bytes)

    def _arena_slot_of(self, off: int) -> int:
        """Owning slot of a GLOBAL arena-relative offset."""
        _, size = self._slot_arena(0)
        return min(int(off) // max(1, size), self.kv_flow_rsids - 1)

    def _flow_alloc(self, length: int, slot: int = 0) -> int:
        """Allocate in the flow's persistent store; returns the store
        offset (namespace-absolute for nvm, GLOBAL arena-relative for
        vslm -- slot slices are carved from the global arena space).
        Reuses freed extents first (see _flow_reclaim), then bumps.
        Lock-guarded: the free-list scan/del raced under threaded serving
        and could hand the SAME extent to two writers (08-24)."""
        align = max(1, int(self.slm_rw_lba_bytes))
        with self._flow_alloc_lock:
            if self.kv_flow == "vslm":
                free = self._flow_free["vslm"].setdefault(slot, [])
            else:
                free = self._flow_free["nvm"]
            pool = "vslm" if self.kv_flow == "vslm" else "nvm"
            for i, (foff, flen) in enumerate(free):
                if flen >= length:
                    del free[i]
                    self._flow_resv[pool][foff] = flen
                    return foff
            if self.kv_flow == "vslm":
                base, size = self._slot_arena(slot)
                nxt = self._vslm_arena_next.setdefault(slot, 0)
                off_rel = self._align_up(nxt, align)
                if off_rel + length > size:
                    raise RuntimeError(
                        "vSLM arena slice exhausted -- raise "
                        "--kv-vslm-arena-mb or lower --kv-flow-rsids "
                        f"(slot={slot}/{self.kv_flow_rsids} want={length} "
                        f"next={off_rel} slice={size} "
                        f"free_extents={len(free)} "
                        f"free_bytes={sum(l for _, l in free)})")
                self._vslm_arena_next[slot] = off_rel + length
                off = base + off_rel
                self._flow_resv[pool][off] = length
                return off
            off = self._align_up(self._nvm_next_offset, align)
            self._nvm_next_offset = off + length
            self._flow_resv[pool][off] = length
            return off

    def _flow_reclaim(self, key: str) -> None:
        """Return a deleted/overwritten key's flow extents to the free list.
        Reserved lengths come from _flow_resv (whole-extent reuse), falling
        back to the meta-recorded sizes for extents from earlier runs."""
        if not self.kv_flow:
            return
        meta = self.metadata.get(key)
        if not isinstance(meta, dict) or meta.get("storage_mode") != "kv_flow":
            return
        with self._flow_alloc_lock:
            if meta.get("kv_flow") == "vslm":
                resv = self._flow_resv["vslm"]
                for c in meta.get("chunks", []):
                    off = c.get("rel_off")
                    if isinstance(off, int):
                        # route the extent back to its OWNING slot's list
                        self._flow_free["vslm"].setdefault(
                            self._arena_slot_of(off), []).append(
                            (off, resv.pop(
                                off, int(c.get("raw_len", 0)) + 4096)))
            else:
                off = meta.get("store_off")
                if isinstance(off, int):
                    self._flow_free["nvm"].append((off, self._flow_resv["nvm"].pop(
                        off, int(meta.get("packed_size", 0)))))

    def _flow_kernel_mode(self) -> str:
        return self.mode if self.mode in ("lossless_compress", "int8_quantize",
                                          "layout", "noop") else "noop"

    def _flow_execute_chunk(self, op: str, chunk: bytes, out_mr_id: int,
                            out_rel_off: int, out_cap: int,
                            metrics_mode: str, phase: str = "write") -> Dict[str, Any]:
        """One inline Execute with MR-window output; returns
        {out_len, crc, latency_s}. Raises on device error/truncation.
        phase MUST be "read" on the decode direction: the device's
        LAYOUT_REPACK dispatch selects decode by extra["phase"]=="read"
        (not by container magic) -- without it the read path re-encodes
        the container (the 08-20 VM checksum-mismatch finding)."""
        extra = {"output_mr_id": int(out_mr_id),
                 "output_offset": int(out_rel_off),
                 "output_length": int(out_cap),
                 "kv_flow": self.kv_flow,
                 "phase": str(phase)}
        if self.mode == "layout":
            extra["layout_block_size_bytes"] = int(min(self.block_size_bytes,
                                                       1024 * 1024))
        slot = self._flow_slot()
        rsid = (self._flow_rsids[slot] if self._flow_rsids
                else self._flow_rsid)
        pind = self._resolve_program_binding(op)[1]
        # One in-flight Execute per SLOT: the firmware leases every
        # resolved range of an RSID per Execute, so concurrency on ONE
        # MRS is refused with 01/95 -- but leases are per-range, so N
        # slot-disjoint MRSes execute in parallel (kv_flow_rsids > 1).
        with self._exec_locks[slot % len(self._exec_locks)]:
            result = self.client.execute(
                cpcs_nsid=self.cpcs_nsid, rsid=rsid, pind=pind, payload=chunk,
                op=op, mode=self.mode, extra=extra)
        self._record_metrics(result, metrics_mode)
        raw = int(result.extra.get("result_raw", result.extra.get("result_dw0", 0)) or 0)
        out_len = int(result.extra.get("result_value", raw & 0xFFFFFFFF) or 0)
        crc = int(result.extra.get("result_crc32", (raw >> 32) & 0xFFFFFFFF) or 0)
        if out_len <= 0 or out_len > out_cap:
            raise RuntimeError(
                f"kv_flow execute bad out_len={out_len} (cap {out_cap}, op {op})")
        return {"out_len": out_len, "crc": crc,
                "latency_s": float(result.command_latency_s)}

    def _flow_addr_mode(self, nsid: int) -> str:
        """SLM namespaces (vSLM 100 / pSLM 101) are CSI-3, BYTE-addressed:
        LBA-addressed opcodes on them return INVALID FIELD or garbage --
        the 08-19 VM finding that explained every pslm/vslm read failure
        (the exec output was byte-perfect all along; only the read-back
        addressing was wrong). The NVM kvstore stays LBA-addressed."""
        if nsid in (int(self.slm_nsid), int(self.pslm_nsid)):
            return "byte"
        return "lba"

    def _flow_store_read(self, nsid: int, offset: int, length: int,
                         metrics_mode: str) -> bytes:
        mode = self._flow_addr_mode(nsid)
        t0 = time.perf_counter()
        if mode == "byte":
            raw = self.client.slm_read(
                slm_nsid=nsid, offset_bytes=offset, length_bytes=length,
                address_mode="byte")
            self._record_slm_read_metrics(metrics_mode, length,
                                          time.perf_counter() - t0)
            return raw[:length]
        aligned_off = (offset // self.slm_rw_lba_bytes) * self.slm_rw_lba_bytes
        pre = offset - aligned_off
        aligned_len = self._align_up(pre + length, self.slm_rw_lba_bytes)
        raw = self.client.slm_read(
            slm_nsid=nsid, offset_bytes=aligned_off, length_bytes=aligned_len,
            address_mode="lba", lba_bytes=self.slm_rw_lba_bytes)
        self._record_slm_read_metrics(metrics_mode, aligned_len,
                                      time.perf_counter() - t0)
        return raw[pre:pre + length]

    def _flow_store_write(self, nsid: int, offset: int, payload: bytes,
                          metrics_mode: str) -> float:
        dev = 0.0
        cap = self._flow_chunk_bytes()
        mode = self._flow_addr_mode(nsid)
        for off in range(0, len(payload), cap):
            piece = payload[off:off + cap]
            if mode == "byte":
                result = self.client.slm_write(
                    slm_nsid=nsid, offset_bytes=offset + off, payload=piece,
                    address_mode="byte")
            else:
                result = self.client.slm_write(
                    slm_nsid=nsid, offset_bytes=offset + off, payload=piece,
                    address_mode="lba", lba_bytes=self.slm_rw_lba_bytes)
            self._record_metrics(result, metrics_mode)
            dev += float(result.command_latency_s)
        return dev

    def _flow_write(self, key: str, data: np.ndarray) -> StorageBackend.IOTiming:
        """Leak guard: a write that dies mid-key (device error, arena
        exhaustion on a later chunk) has no metadata yet, so its already-
        allocated extents would be unreclaimable -- under exhaustion
        pressure every failed write then leaks, a death spiral. Track this
        call's allocations and return them to the free list on failure."""
        allocs: List[int] = []
        try:
            return self._flow_write_inner(key, data, allocs)
        except Exception:
            with self._flow_alloc_lock:
                if self.kv_flow == "vslm":
                    resv = self._flow_resv["vslm"]
                    for off in allocs:
                        if off in resv:
                            self._flow_free["vslm"].setdefault(
                                self._arena_slot_of(off), []).append(
                                (off, resv.pop(off)))
                else:
                    free = self._flow_free["nvm"]
                    resv = self._flow_resv["nvm"]
                    for off in allocs:
                        if off in resv:
                            free.append((off, resv.pop(off)))
            raise

    def _flow_write_inner(self, key: str, data: np.ndarray,
                          allocs: List[int]) -> StorageBackend.IOTiming:
        from . import kv_kernels
        self._flow_reclaim(key)  # overwrite: recycle the old extents
        start = time.perf_counter()
        # RAW tensor bytes, not the .npy container: the device kernels treat
        # the payload as an f32 stream, and a lossy transform must not chew
        # a serialization header. shape/dtype travel in the metadata.
        raw_payload = np.ascontiguousarray(data).tobytes()
        post_serialize = time.perf_counter()
        kmode = self._flow_kernel_mode()
        device_time = 0.0
        host_kernel_s = 0.0
        chunks: List[Dict[str, Any]] = []
        cap = self._flow_chunk_bytes()
        profile = self._command_profile(
            phase="write", key=key, mode=self.mode,
            payload_bytes=len(raw_payload), shape=tuple(data.shape),
            dtype=data.dtype.str)
        op = str(profile["op"])
        mmode = str(profile["metrics_mode"]) + f"_{self.kv_flow}"

        req_output = 0     # host-materialized transform output bytes
        req_readback = 0   # device->host copy-out bytes (write dir)
        req_workbuf = 0    # peak flow-held output/return buffer bytes
        if self.kv_flow == "hostnvm":
            t0 = time.perf_counter()
            pieces = [kv_kernels.encode(kmode, raw_payload[o:o + cap],
                                        min(self.block_size_bytes, 1024 * 1024))
                      if kmode == "layout" else
                      kv_kernels.encode(kmode, raw_payload[o:o + cap])
                      for o in range(0, len(raw_payload), cap)]
            host_kernel_s = time.perf_counter() - t0
            self._flow_host_kernel_s_total += host_kernel_s
            pos = 0
            store_parts = []
            for piece, o in zip(pieces, range(0, len(raw_payload), cap)):
                # per-chunk crc (09-03): selective reads (tail policy)
                # cannot verify the whole-block checksum, so each chunk
                # carries its own. Old records without it skip per-chunk
                # verification (back-compat, disclosed).
                chunks.append({"packed_len": len(piece), "rel_off": pos,
                               "raw_len": min(cap, len(raw_payload) - o),
                               "crc": int(zlib.crc32(piece) & 0xFFFFFFFF)})
                store_parts.append(piece)
                pos += len(piece)
            packed_all = b"".join(store_parts)
            # host arm materializes the FULL transformed block (pieces +
            # the joined copy live simultaneously) -- the figure-(b) term
            req_output = pos
            req_workbuf = pos + len(packed_all)
            store_off = self._flow_alloc(len(packed_all))
            allocs.append(store_off)
            device_time += self._flow_store_write(self.nvm_nsid, store_off,
                                                  packed_all, mmode)
            store_nsid = self.nvm_nsid
        else:
            out_mr = 2 if self.kv_flow == "vslm" else 1
            slot = self._flow_slot()
            arena_chunks = []
            if self.kv_flow == "pslm":
                # STREAMING copy-out (08-31): the bounded-MR protocol
                # already forces chunking, so each chunk's read-back is
                # written straight to the kvstore instead of being
                # accumulated + joined (the old triple-buffer). Extent
                # is allocated up-front at an upper bound (output never
                # exceeds input; +container/alignment slack per chunk).
                lba = max(1, int(self.slm_rw_lba_bytes))
                n_chunks = max(1, -(-len(raw_payload) // cap))
                store_off = self._flow_alloc(
                    len(raw_payload) + n_chunks * (4096 + lba))
                allocs.append(store_off)
                p_base, p_len = self._slot_pslm(slot)
                pos = 0
            for o in range(0, len(raw_payload), cap):
                chunk = raw_payload[o:o + cap]
                # Device encoders raw-fallback when output >= input, so the
                # result never exceeds the chunk; +4096 covers containers.
                if self.kv_flow == "vslm":
                    rel_g = self._flow_alloc(len(chunk) + 4096, slot)
                    allocs.append(rel_g)
                    out_cap = len(chunk) + 4096
                    out_rel = rel_g - self._slot_arena(slot)[0]
                else:
                    out_rel = 0
                    out_cap = min(p_len, len(chunk) + 4096)
                r = self._flow_execute_chunk(op, chunk, out_mr, out_rel,
                                             out_cap, mmode)
                device_time += r["latency_s"]
                if self.kv_flow == "pslm":
                    packed = self._flow_store_read(self.pslm_nsid, p_base,
                                                   r["out_len"], mmode)
                    if (zlib.crc32(packed) & 0xFFFFFFFF) != r["crc"] and r["crc"]:
                        raise RuntimeError("kv_flow pslm read-back crc mismatch")
                    device_time += self._flow_store_write(
                        self.nvm_nsid, store_off + pos, packed, mmode)
                    chunks.append({"packed_len": r["out_len"],
                                   "rel_off": pos,
                                   "raw_len": len(chunk), "crc": r["crc"]})
                    req_readback += r["out_len"]
                    req_workbuf = max(req_workbuf, r["out_len"])
                    pos = self._align_up(pos + r["out_len"], lba)
                    del packed
                else:
                    arena_chunks.append({"rel_off": rel_g,
                                         "packed_len": r["out_len"],
                                         "raw_len": len(chunk),
                                         "crc": r["crc"]})
            if self.kv_flow == "pslm":
                store_nsid = self.nvm_nsid
            else:
                chunks = arena_chunks
                store_off = 0  # per-chunk GLOBAL rel offsets authoritative
                store_nsid = self.slm_nsid

        checksum = int(zlib.crc32(raw_payload) & 0xFFFFFFFF)
        meta = {
            "shape": list(data.shape), "dtype": data.dtype.str,
            "mode": self.mode, "kv_flow": self.kv_flow,
            "raw_size": len(raw_payload), "checksum": checksum,
            "store_nsid": store_nsid, "store_off": store_off,
            "chunk_bytes": cap, "chunks": chunks,
            "packed_size": sum(c["packed_len"] for c in chunks),
            "storage_mode": "kv_flow",
        }
        self.metadata[key] = meta
        allocs.clear()  # committed to metadata -- no longer abortable
        self._meta_path(key).write_text(json.dumps(meta), encoding="utf-8")
        # figure-(b) accounting (write direction): totals summed, peak
        # max-updated; per-request line at DEBUG for the return-data
        # logger the operator asked for (08-31).
        self._flow_output_bytes_total += req_output
        self._flow_readback_bytes_total += req_readback
        self._flow_workbuf_peak_bytes = max(self._flow_workbuf_peak_bytes,
                                            req_workbuf)
        self._flow_mem_requests_total += 1
        logger.debug(
            "kv_flow %s write mem: output=%dB readback=%dB workbuf=%dB "
            "block=%dB", self.kv_flow, req_output, req_readback,
            req_workbuf, len(raw_payload))
        end = time.perf_counter()
        host_time = (end - start) - device_time
        return StorageBackend.IOTiming(
            total=end - start, device=device_time, host=host_time,
            components=({"host_kernel": host_kernel_s}
                        if host_kernel_s > 0 else None))

    def _flow_read(self, key: str) -> Tuple[np.ndarray, StorageBackend.IOTiming]:
        from . import kv_kernels
        start = time.perf_counter()
        meta = self.metadata.get(key)
        if meta is None:
            mp = self._meta_path(key)
            if not mp.exists():
                raise KeyError(f"Key {key} not found in kv_flow store")
            meta = json.loads(mp.read_text(encoding="utf-8"))
            self.metadata[key] = meta
        kmode = str(meta.get("mode", self.mode))
        if kmode not in ("lossless_compress", "int8_quantize", "layout", "noop"):
            kmode = "noop"
        device_time = 0.0
        out_parts: List[bytes] = []
        mmode = f"{kmode}_read_{self.kv_flow}"
        store_nsid = int(meta["store_nsid"])
        store_off = int(meta.get("store_off", 0))
        arena_base = self.kv_scratch_bytes  # vslm range-2 namespace offset

        # S2 (09-03): decode-stage selective reads. tail:F reads only the
        # trailing chunks covering >= F of the block's raw bytes -- the
        # decode semantics need the most recent tokens' KV. Applies ONLY
        # to decode-stage reads (stage tag) with s2 enabled; multi-turn/
        # rag/verification reads stay full. Disclosed config column.
        sel_chunks = list(meta["chunks"])
        partial = False
        if (self.decode_read_policy.startswith("tail:")
                and self.offload_stages.get("s2") in ("host", "device")
                and stage_metrics.current_stage() == "decode"):
            frac = float(self.decode_read_policy.split(":", 1)[1])
            sel_chunks = self._select_tail_chunks(sel_chunks, frac)
            partial = len(sel_chunks) != len(meta["chunks"])

        # Device SLM-source path (P3): ONE select+decode execute per slot
        # group instead of per-chunk read+execute. Taken for decode-stage
        # partial reads (s2=device) and batch reads (s4=device), vslm
        # only. Any failure falls back to the per-chunk path, counted.
        in_batch = bool(getattr(self._batch_local, "active", False))
        want_device = (
            self.kv_flow == "vslm"
            and ((partial and self.offload_stages.get("s2") == "device")
                 or (in_batch and self.offload_stages.get("s4") == "device")))
        if want_device:
            try:
                raw, dev_s = self._flow_device_select_read(
                    sel_chunks, kmode, mmode)
                device_time += dev_s
                if partial:
                    arr = np.frombuffer(
                        raw, dtype=np.dtype(str(meta["dtype"]))).copy()
                else:
                    if kmode != "int8_quantize":
                        if (zlib.crc32(raw) & 0xFFFFFFFF) != int(
                                meta.get("checksum", 0)):
                            raise IOError(
                                f"kv_flow device read checksum mismatch for {key}")
                    arr = np.frombuffer(raw, dtype=np.dtype(str(meta["dtype"])))
                    arr = arr.reshape(
                        [int(x) for x in meta["shape"]]).copy()
                self._flow_read_readback_bytes_total += len(raw)
                end = time.perf_counter()
                return arr, StorageBackend.IOTiming(
                    total=end - start, device=device_time,
                    host=(end - start) - device_time,
                    components={"exec_cmd": dev_s})
            except Exception as exc:
                self._slm_device_fallbacks += 1
                logger.warning(
                    "slm device select fell back to per-chunk path: %s", exc)

        for c in sel_chunks:
            if self.kv_flow == "vslm":
                packed = self._flow_store_read(
                    self.slm_nsid, arena_base + int(c["rel_off"]),
                    int(c["packed_len"]), mmode)
            else:
                packed = self._flow_store_read(
                    store_nsid, store_off + int(c["rel_off"]),
                    int(c["packed_len"]), mmode)
            if "crc" in c and (zlib.crc32(packed) & 0xFFFFFFFF) != int(c["crc"]):
                raise IOError(f"kv_flow chunk crc mismatch for {key} "
                              f"rel_off={c['rel_off']}")
            if self.kv_flow == "hostnvm":
                t0 = time.perf_counter()
                decoded_piece = kv_kernels.decode(kmode, packed)
                self._flow_host_kernel_s_total += time.perf_counter() - t0
                out_parts.append(decoded_piece)
                continue
            prof = self._command_profile(phase="read", key=key, mode=kmode,
                                         payload_bytes=len(packed))
            r = self._flow_execute_chunk(str(prof["op"]), packed, 1, 0,
                                         int(c["raw_len"]) + 4096, mmode,
                                         phase="read")
            device_time += r["latency_s"]
            # decode output lands in THIS worker slot's scratch slice
            slot = self._flow_slot()
            if self.kv_flow == "pslm":
                scratch_nsid, s_base = self.pslm_nsid, self._slot_pslm(slot)[0]
            else:
                scratch_nsid, s_base = self.slm_nsid, self._slot_scratch(slot)[0]
            decoded = self._flow_store_read(scratch_nsid, s_base,
                                            r["out_len"], mmode)
            self._flow_read_readback_bytes_total += r["out_len"]
            out_parts.append(decoded)
        raw = b"".join(out_parts)
        if partial:
            # tail read: whole-block checksum/shape are unavailable by
            # construction (per-chunk crcs verified above); the decode
            # consumer discards the data, so return the flat tail.
            arr = np.frombuffer(raw, dtype=np.dtype(str(meta["dtype"]))).copy()
        else:
            if kmode != "int8_quantize":
                if (zlib.crc32(raw) & 0xFFFFFFFF) != int(meta.get("checksum", 0)):
                    raise IOError(f"kv_flow read checksum mismatch for {key}")
            arr = np.frombuffer(raw, dtype=np.dtype(str(meta["dtype"])))
            arr = arr.reshape([int(x) for x in meta["shape"]]).copy()
        end = time.perf_counter()
        return arr, StorageBackend.IOTiming(
            total=end - start, device=device_time,
            host=(end - start) - device_time)

    @staticmethod
    def _select_tail_chunks(chunks: List[Dict[str, Any]],
                            frac: float) -> List[Dict[str, Any]]:
        """Minimal trailing chunk set covering >= frac of the raw bytes
        (order preserved). Pure helper -- unit-tested directly."""
        total_raw = sum(int(c["raw_len"]) for c in chunks)
        need = max(1, int(frac * total_raw))
        got = 0
        picked: List[Dict[str, Any]] = []
        for c in reversed(list(chunks)):
            picked.append(c)
            got += int(c["raw_len"])
            if got >= need:
                break
        return list(reversed(picked))

    # ---- SLM-source device paths (P3, stage-offload vintage) -----------
    # Firmware ABI mirror (spdk-p2 0357b7e7b): CPCSSLM1 binary entry
    # table riding the CPCSREQ1 payload; cparam1=1 selects the device's
    # SLM-source engine. vslm flow ONLY: pslm's persisted chunks live in
    # the kvstore namespace, which is outside every MRS (reachability
    # change = operator item; typed limitation, never silently degraded).

    _SLM_MAGIC = b"CPCSSLM1"
    _SLM_OP = {"block_select": 4, "prefix_lookup": 5, "batch_read": 6,
               "migrate": 7}
    _SLM_TRAILER = 16
    _KV_MODE_INT = {"off": 0, "noop": 1, "lossless_compress": 2,
                    "int8_quantize": 3, "layout": 4}

    def _slm_descriptor(self, op_name: str, entries: List[Dict[str, Any]],
                        flags: int = 0,
                        queries: Optional[List[int]] = None) -> bytes:
        parts = [self._SLM_MAGIC,
                 struct.pack("<II", self._SLM_OP[op_name], len(entries)),
                 struct.pack("<Q", int(flags))]
        for e in entries:
            parts.append(struct.pack(
                "<QQQQQII",
                int(e.get("src_mr", 0)), int(e.get("src_off", 0)),
                int(e.get("dst_mr", 0)), int(e.get("dst_off", 0)),
                int(e.get("len", 0)), int(e.get("raw_len", 0)),
                int(e.get("mode", 0))))
        for q in (queries or []):
            parts.append(struct.pack("<Q", int(q) & 0xFFFFFFFFFFFFFFFF))
        return b"".join(parts)

    def _slm_execute(self, op_name: str, slot: int, desc: bytes,
                     extra: Dict[str, Any], kmode: str = "noop"):
        rsid = (self._flow_rsids[slot] if self._flow_rsids
                else self._flow_rsid)
        try:
            pind = self._resolve_program_binding(op_name)[1]
        except Exception:
            pind = self._SPDK_KV_BUILTIN_PIND_DEFAULTS.get(op_name, 13)
        with self._exec_locks[slot % len(self._exec_locks)]:
            result = self.client.execute(
                cpcs_nsid=self.cpcs_nsid, rsid=rsid, pind=pind,
                payload=desc, op=op_name, mode=kmode, extra=extra,
                cparam1=1)
        self._slm_device_execs += 1
        raw = int(result.extra.get("result_raw",
                                   result.extra.get("result_dw0", 0)) or 0)
        served = int(result.extra.get("result_value",
                                      raw & 0xFFFFFFFF) or 0)
        return result, served

    def _flow_device_select_read(self, sel_chunks: List[Dict[str, Any]],
                                 kmode: str, mmode: str):
        """ONE PIND-10 SLM-source execute per slot-group (device decodes
        the selected arena chunks into the slot's scratch window) + ONE
        window read per group. Returns (raw_bytes, device_time_s).
        Raises on any mismatch -- the caller falls back to the per-chunk
        path and counts the fallback."""
        mode_int = self._KV_MODE_INT.get(kmode, 1)
        groups: Dict[int, List[Dict[str, Any]]] = {}
        for c in sel_chunks:
            groups.setdefault(self._arena_slot_of(int(c["rel_off"])),
                              []).append(c)
        pieces: Dict[int, bytes] = {}
        device_time = 0.0
        for slot, chunks in sorted(groups.items()):
            a_base, _ = self._slot_arena(slot)
            s_base, s_len = self._slot_scratch(slot)
            # 09-05 (STAGE night BUG 4): decode windows reach tens of MiB
            # while a slot's scratch slice is scratch_mb/rsids (4 MiB at
            # the defaults) -- the un-batched window overflowed on 100%
            # of attempts. Split the selection into window-budget batches
            # (one execute + one window read per batch); command count is
            # ceil(bytes/slice) instead of 1 -- still far below
            # per-chunk. A SINGLE chunk that cannot fit is a hard error
            # (falls back, counted).
            batches: List[List[Dict[str, Any]]] = []
            cur: List[Dict[str, Any]] = []
            cur_len = 0
            for c in chunks:
                need = int(c["raw_len"]) + self._SLM_TRAILER
                if need > s_len:
                    raise RuntimeError(
                        f"slm select single chunk {need} exceeds scratch "
                        f"slice {s_len}")
                if cur and cur_len + need > s_len:
                    batches.append(cur)
                    cur, cur_len = [], 0
                cur.append(c)
                cur_len += need
            if cur:
                batches.append(cur)
            for batch in batches:
                entries = [{"src_mr": 2,
                            "src_off": int(c["rel_off"]) - a_base,
                            "len": int(c["packed_len"]),
                            "raw_len": int(c["raw_len"]),
                            "mode": mode_int} for c in batch]
                payload_total = sum(int(c["raw_len"]) for c in batch)
                win_len = payload_total + len(entries) * self._SLM_TRAILER
                desc = self._slm_descriptor("block_select", entries)
                extra = {"output_mr_id": 1, "output_offset": 0,
                         "output_length": int(win_len),
                         "kv_flow": self.kv_flow, "phase": "read"}
                result, served = self._slm_execute("block_select", slot,
                                                   desc, extra, kmode)
                device_time += float(result.command_latency_s)
                if served != len(entries):
                    raise RuntimeError(
                        f"slm select served {served}/{len(entries)}")
                window = self._flow_store_read(self.slm_nsid, s_base,
                                               win_len, mmode)
                tbase = payload_total
                for i, c in enumerate(batch):
                    t = window[tbase + i * self._SLM_TRAILER:
                               tbase + (i + 1) * self._SLM_TRAILER]
                    rel_off, out_len, crc = struct.unpack("<III", t[:12])
                    piece = window[rel_off:rel_off + out_len]
                    if (out_len != int(c["raw_len"]) or
                            (zlib.crc32(piece) & 0xFFFFFFFF) != crc):
                        raise IOError("slm select trailer/crc mismatch")
                    pieces[id(c)] = piece
        raw = b"".join(pieces[id(c)] for c in sel_chunks)
        return raw, device_time

    def prefix_lookup_device(self, image: bytes,
                             queries: List[int]) -> List[bytes]:
        """S6 device arm: publish the 32-byte-record index image into
        this slot's scratch and scan it with ONE PIND-11 execute.
        Returns the matched records (32B each)."""
        if self.kv_flow != "vslm":
            raise RuntimeError("prefix_lookup device path needs vslm flow")
        if not image or len(image) % 32 != 0 or not queries:
            return []
        slot = self._flow_slot()
        s_base, s_len = self._slot_scratch(slot)
        out_off = ((len(image) + 4095) // 4096) * 4096
        win_len = len(queries) * (32 + self._SLM_TRAILER)
        if out_off + win_len > s_len:
            raise RuntimeError("prefix index + window exceed scratch")
        mmode = f"prefix_index_{self.kv_flow}"
        t0 = time.perf_counter()
        self._flow_store_write(self.slm_nsid, s_base, image, mmode)
        desc = self._slm_descriptor(
            "prefix_lookup",
            [{"src_mr": 1, "src_off": 0, "len": len(image)}],
            flags=len(queries), queries=queries)
        extra = {"output_mr_id": 1, "output_offset": int(out_off),
                 "output_length": int(win_len),
                 "kv_flow": self.kv_flow, "phase": "read"}
        result, matched = self._slm_execute("prefix_lookup", slot, desc,
                                            extra)
        recs: List[bytes] = []
        if matched > 0:
            window = self._flow_store_read(
                self.slm_nsid, s_base + out_off,
                matched * (32 + self._SLM_TRAILER), mmode)
            recs = [window[i * 32:(i + 1) * 32] for i in range(matched)]
        # 09-05 (STAGE night BUG 3): fold the lookup's device time into
        # the active stage (prefix) -- previously S6's round-trips were
        # visible only in wall_s and device_s read 0 exactly where the
        # ablation acts.
        total = time.perf_counter() - t0
        exec_s = float(result.command_latency_s)
        stage_metrics.note_io(StorageBackend.IOTiming(
            total=total, device=exec_s,
            host=max(0.0, total - exec_s),
            components={"exec_cmd": exec_s}))
        return recs

    def migrate_extents(self, moves: List[Dict[str, Any]]) -> float:
        """S3 capability: multi-extent SLM->SLM copy in ONE device
        command (PIND 13). NOT wired to a measured benchmark consumer:
        the tier ladder has no device-resident->device-resident demotion
        (cpu->nvme demotions carry host-resident data by physics). The
        twin golden test drives this; a measured consumer is a future
        operator-approved workload change. Returns device latency (s)."""
        slot = self._flow_slot()
        desc = self._slm_descriptor("migrate", moves)
        extra = {"kv_flow": self.kv_flow, "phase": "write"}
        result, served = self._slm_execute("migrate", slot, desc, extra)
        if served != len(moves):
            raise RuntimeError(f"migrate served {served}/{len(moves)}")
        return float(result.command_latency_s)

    def _store_packed_payload(self, key: str, packed_payload: bytes, meta: Dict[str, Any]) -> float:
        """
        Persist packed payload to current storage layout.

        Returns:
            Device-side persistence time (fsync path) in seconds.
        """
        if self.storage_mode == "file":
            path = self._path(key)
            with open(path, "wb") as f:
                f.write(packed_payload)
                post_write = time.perf_counter()
                f.flush()
                os.fsync(f.fileno())
                post_fsync = time.perf_counter()
            self._meta_path(key).write_text(json.dumps(meta, indent=2), encoding="utf-8")
            self.metadata[key] = dict(meta)
            return post_fsync - post_write

        if not self.arena_path:
            raise RuntimeError("arena_path is not initialized for arena mode")

        arena_offset = self._align_up(self._arena_next_offset, self.block_size_bytes)
        arena_length = int(len(packed_payload))
        post_write = time.perf_counter()
        post_fsync = post_write

        def _local_arena_write() -> Tuple[float, float]:
            if not self.arena_path:
                raise RuntimeError("arena_path is not initialized for local arena write")
            with open(self.arena_path, "r+b") as f:
                current_size = int(f.seek(0, os.SEEK_END))
                if arena_offset > current_size:
                    f.seek(current_size)
                    f.write(bytes(arena_offset - current_size))
                f.seek(arena_offset)
                f.write(packed_payload)
                t_write = time.perf_counter()
                f.flush()
                os.fsync(f.fileno())
                t_fsync = time.perf_counter()
            return t_write, t_fsync

        if self._use_spdk_arena_io():
            try:
                result = self.client.slm_write(
                    slm_nsid=self.slm_nsid,
                    offset_bytes=arena_offset,
                    payload=packed_payload,
                    address_mode=self.slm_write_address_mode,
                    lba_bytes=self.slm_rw_lba_bytes,
                )
                self._record_metrics(result, f"{self.mode}_slm_write")
                post_fsync = post_write + float(result.command_latency_s)
                meta["arena_io"] = "spdk_slm_write"
            except Exception:
                if not self.fallback_on_error:
                    raise
                self._record_command_failure(f"{self.mode}_slm_write", arena_length)
                post_write, post_fsync = _local_arena_write()
                meta["arena_io"] = "local_fallback"
        else:
            post_write, post_fsync = _local_arena_write()
            meta["arena_io"] = "local_file"

        arena_end = arena_offset + arena_length
        self._arena_next_offset = max(self._arena_next_offset, arena_end)

        stored_meta = dict(meta)
        stored_meta["arena_offset"] = arena_offset
        stored_meta["arena_length"] = arena_length
        stored_meta["arena_end"] = arena_end
        self.metadata[key] = stored_meta
        self._persist_arena_index()
        return post_fsync - post_write

    def _load_packed_payload_and_meta(self, key: str) -> Tuple[bytes, Dict[str, Any], float]:
        """
        Load packed payload for key.

        Returns:
            (payload, metadata, disk_time_seconds)
        """
        if self.storage_mode == "file":
            path = self._path(key)
            meta_path = self._meta_path(key)
            if not path.exists():
                raise KeyError(f"Key {key} not found in CPCS cache")
            if not meta_path.exists():
                raise KeyError(f"Metadata missing for key {key}")
            t0 = time.perf_counter()
            packed = path.read_bytes()
            t1 = time.perf_counter()
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.metadata[key] = dict(meta)
            return packed, meta, (t1 - t0)

        if key not in self.metadata:
            raise KeyError(f"Key {key} not found in CPCS arena index")
        meta = dict(self.metadata[key])
        offset = int(meta.get("arena_offset", -1))
        length = int(meta.get("arena_length", -1))
        if offset < 0 or length < 0:
            raise KeyError(f"Arena metadata missing for key {key}")

        t0 = time.perf_counter()
        arena_io = str(meta.get("arena_io", "") or "")
        if self._use_spdk_arena_io() and arena_io != "local_fallback":
            try:
                if self.slm_read_address_mode == "lba":
                    lba = max(1, int(self.slm_rw_lba_bytes))
                    aligned_offset = (offset // lba) * lba
                    aligned_length = self._align_up(max(length, 1), lba)
                    read_t0 = time.perf_counter()
                    raw = self.client.slm_read(
                        slm_nsid=self.slm_nsid,
                        offset_bytes=aligned_offset,
                        length_bytes=aligned_length,
                        address_mode="lba",
                        lba_bytes=lba,
                    )
                    self._record_slm_read_metrics(f"{self.mode}_slm_read", aligned_length, time.perf_counter() - read_t0)
                    start = offset - aligned_offset
                    packed = raw[start : start + length]
                else:
                    read_t0 = time.perf_counter()
                    packed = self.client.slm_read(
                        slm_nsid=self.slm_nsid,
                        offset_bytes=offset,
                        length_bytes=length,
                        address_mode="byte",
                    )
                    self._record_slm_read_metrics(f"{self.mode}_slm_read", length, time.perf_counter() - read_t0)
            except Exception:
                if not self.fallback_on_error:
                    raise
                self._record_command_failure(f"{self.mode}_slm_read", int(length))
                arena_io = "local_fallback"
        else:
            arena_io = "local_file"

        if arena_io in ("local_file", "local_fallback"):
            if not self.arena_path or not self.arena_path.exists():
                raise KeyError(f"Arena file missing for key {key}: {self.arena_path}")
            with open(self.arena_path, "rb") as f:
                f.seek(offset)
                packed = f.read(length)
        t1 = time.perf_counter()
        if len(packed) != length:
            raise IOError(
                f"Short read for key {key}: expected {length} bytes at offset {offset}, got {len(packed)}"
            )
        return packed, meta, (t1 - t0)

    def write(self, key: str, data: np.ndarray) -> StorageBackend.IOTiming:
        if self.kv_flow:
            return self._flow_write(key, data)
        start = time.perf_counter()
        raw_payload = self._serialize_array(data)
        post_serialize = time.perf_counter()

        command_latency = 0.0
        mode_for_entry = self.mode
        extra: Dict[str, Any] = {}
        profile = self._command_profile(
            phase="write",
            key=key,
            mode=self.mode,
            payload_bytes=len(raw_payload),
            shape=tuple(data.shape),
            dtype=data.dtype.str,
        )
        try:
            rsid, pind = self._resolve_program_binding(str(profile["op"]))
            exec_extra = dict(profile["extra"])
            exec_extra["program_rsid"] = int(rsid)
            exec_extra["program_pind"] = int(pind)
            output_target = self._build_execute_output_target(
                str(profile["op"]),
                int(rsid),
                len(raw_payload),
                exec_extra,
            )
            result = self.client.execute(
                cpcs_nsid=self.cpcs_nsid,
                rsid=rsid,
                pind=pind,
                payload=raw_payload,
                op=str(profile["op"]),
                mode=str(profile["mode"]),
                shape=tuple(data.shape),
                dtype=data.dtype.str,
                extra=exec_extra,
            )
            self._record_metrics(result, str(profile["metrics_mode"]))
            consumed_output = self._consume_execute_output_payload(
                op=str(profile["op"]),
                result=result,
                target=output_target,
                metrics_mode=str(profile["metrics_mode"]),
            )
            packed_payload = consumed_output if consumed_output is not None else result.extra.get("payload", raw_payload)
            extra = dict(result.extra)
            command_latency = float(result.command_latency_s)
        except Exception:
            if not self.fallback_on_error:
                raise
            self._record_command_failure(str(profile["metrics_mode"]), len(raw_payload))
            packed_payload = raw_payload
            mode_for_entry = "fallback_raw"

        checksum = int(zlib.crc32(raw_payload) & 0xFFFFFFFF)
        meta = {
            "shape": list(data.shape),
            "dtype": data.dtype.str,
            "mode": mode_for_entry,
            "packed_size": int(len(packed_payload)),
            "raw_size": int(len(raw_payload)),
            "checksum": checksum,
            "storage_mode": self.storage_mode,
        }
        extra.pop("payload", None)
        if extra:
            meta["extra"] = extra

        persist_device_time = self._store_packed_payload(key, packed_payload, meta)
        end = time.perf_counter()

        total = end - start
        device_time = persist_device_time + command_latency
        # 09-03: host is the REMAINDER of the wall, not the whole wall --
        # the old arithmetic double-counted (device + host > total).
        host_time = max(0.0, total - device_time)
        return StorageBackend.IOTiming(total=total, device=device_time, host=host_time)

    def read(self, key: str) -> Tuple[np.ndarray, StorageBackend.IOTiming]:
        if self.kv_flow:
            return self._flow_read(key)
        start = time.perf_counter()
        packed_payload, meta, disk_time = self._load_packed_payload_and_meta(key)
        mode = str(meta.get("mode", self.mode))
        profile = self._command_profile(
            phase="read",
            key=key,
            mode=mode,
            payload_bytes=len(packed_payload),
            shape=tuple(int(x) for x in meta.get("shape", [])),
            dtype=str(meta.get("dtype", "")),
            meta=meta,
        )

        rsid, pind = self._resolve_program_binding(str(profile["op"]))
        exec_extra = dict(profile["extra"])
        exec_extra["program_rsid"] = int(rsid)
        exec_extra["program_pind"] = int(pind)
        output_target = self._build_execute_output_target(
            str(profile["op"]),
            int(rsid),
            len(packed_payload),
            exec_extra,
        )

        result = self.client.execute(
            cpcs_nsid=self.cpcs_nsid,
            rsid=rsid,
            pind=pind,
            payload=packed_payload,
            op=str(profile["op"]),
            mode=str(profile["mode"]),
            shape=tuple(meta.get("shape", [])),
            dtype=str(meta.get("dtype", "float16")),
            extra=exec_extra,
        )
        self._record_metrics(result, str(profile["metrics_mode"]))

        consumed_output = self._consume_execute_output_payload(
            op=str(profile["op"]),
            result=result,
            target=output_target,
            metrics_mode=str(profile["metrics_mode"]),
        )
        raw_payload = consumed_output if consumed_output is not None else result.extra.get("payload", packed_payload)
        data = self._deserialize_array(raw_payload)
        end = time.perf_counter()

        if self.verify_every_n > 0 and mode not in ("int8_quantize",):
            self._verify_counter += 1
            if (self._verify_counter % self.verify_every_n) == 0:
                expected = int(meta.get("checksum", -1))
                actual = int(zlib.crc32(raw_payload) & 0xFFFFFFFF)
                if expected >= 0 and actual != expected:
                    raise ValueError(f"CPCS checksum mismatch for key {key}: expected={expected} actual={actual}")

        total = end - start
        device_time = float(result.command_latency_s)
        # 09-03: same double-count fix as write; the payload disk/arena
        # read stays host-side but is disclosed as a component.
        host_time = max(0.0, total - device_time)
        return data, StorageBackend.IOTiming(
            total=total, device=device_time, host=host_time,
            components={"nvm_io": float(disk_time)})

    def supports_batch_read(self) -> bool:
        return self.batch_size > 1 or self.mode in {"layout", "block_select", "prefix_index"} or self.storage_mode == "arena"

    def read_many(self, keys: List[str]) -> Dict[str, Tuple[np.ndarray, StorageBackend.IOTiming]]:
        ks = [str(k) for k in keys]
        self._issue_batch_descriptor(ks)
        # S4 host arm (09-03): coalesced issue order -- ascending storage
        # offset for sequential locality. The host cannot merge commands
        # across the flow protocol; the TRUE single-command device gather
        # (PIND 12 cparam1=1) lands with P3. Result dict preserves caller
        # key order either way.
        if self.offload_stages.get("s4") in ("host", "device"):
            def _off(k: str):
                m = self.metadata.get(k) or {}
                return (int(m.get("store_nsid", 0) or 0),
                        int(m.get("store_off", 0) or 0))
            ordered = sorted(ks, key=_off)
        else:
            ordered = ks
        got: Dict[str, Tuple[np.ndarray, StorageBackend.IOTiming]] = {}
        self._batch_local.active = (
            self.offload_stages.get("s4") == "device")
        try:
            for key in ordered:
                got[key] = self.read(key)
        finally:
            self._batch_local.active = False
        return {k: got[k] for k in ks}

    def delete(self, key: str):
        self._flow_reclaim(key)
        if self.storage_mode == "file":
            self._path(key).unlink(missing_ok=True)
            self._meta_path(key).unlink(missing_ok=True)
        else:
            self.metadata.pop(key, None)
            self._persist_arena_index()
            return
        self.metadata.pop(key, None)

    def clear(self):
        if self.storage_mode == "file":
            for file in self.base_path.glob("*.cpcs"):
                file.unlink(missing_ok=True)
            for file in self.base_path.glob("*.cpcs.meta.json"):
                file.unlink(missing_ok=True)
        else:
            if self.arena_path:
                self.arena_path.write_bytes(b"")
            self._arena_next_offset = 0
            self.metadata.clear()
            self._persist_arena_index()
            return
        self.metadata.clear()

    def get_metrics_summary(self) -> Dict[str, Any]:
        summary = self.metrics.to_dict()
        summary["cpcs_backend_mode"] = self.mode
        summary["cpcs_storage_mode"] = self.storage_mode
        summary["cpcs_client"] = self.client_type
        summary["cpcs_program_rsid"] = int(self.cpcs_rsid)
        summary["cpcs_program_pind_default"] = int(self.cpcs_pind)
        summary["cpcs_program_pind_by_op"] = dict(self.cpcs_pind_by_op)
        summary["cpcs_program_rsid_by_op"] = dict(self.cpcs_rsid_by_op)
        summary["cpcs_execute_output_enable"] = bool(self.execute_output_enable)
        summary["cpcs_execute_output_mr_id"] = int(self.execute_output_mr_id)
        summary["cpcs_execute_output_offset_bytes"] = int(self.execute_output_offset_bytes)
        summary["cpcs_execute_output_max_bytes"] = int(self.execute_output_max_bytes)
        if self._known_mrs_ranges:
            summary["cpcs_known_mrs_ranges"] = list(self._known_mrs_ranges)
        summary["cpcs_slm_read_address_mode"] = str(self.slm_read_address_mode)
        summary["cpcs_slm_write_address_mode"] = str(self.slm_write_address_mode)
        summary["cpcs_slm_rw_lba_bytes"] = int(self.slm_rw_lba_bytes)
        # figure-(b) flow-memory scalars: TOP-LEVEL numerics so cache.py
        # auto-hoists them into cache_stats -> benchmark summary (08-31).
        summary["cpcs_flow_output_bytes_total"] = int(self._flow_output_bytes_total)
        summary["cpcs_flow_readback_bytes_total"] = int(self._flow_readback_bytes_total)
        summary["cpcs_flow_read_readback_bytes_total"] = int(
            self._flow_read_readback_bytes_total)
        summary["cpcs_flow_workbuf_peak_bytes"] = int(self._flow_workbuf_peak_bytes)
        summary["cpcs_flow_mem_requests_total"] = int(self._flow_mem_requests_total)
        summary["cpcs_flow_host_kernel_s_total"] = float(
            self._flow_host_kernel_s_total)
        summary["cpcs_slm_device_execs"] = int(self._slm_device_execs)
        summary["cpcs_slm_device_fallbacks"] = int(
            self._slm_device_fallbacks)
        summary["cpcs_flow_rsids"] = int(self.kv_flow_rsids)
        if self.bootstrap_status:
            summary["cpcs_bootstrap"] = dict(self.bootstrap_status)
        if self.storage_mode == "arena":
            summary["cpcs_arena_path"] = str(self.arena_path) if self.arena_path else ""
            summary["cpcs_index_path"] = str(self.index_path) if self.index_path else ""
            summary["cpcs_arena_next_offset"] = int(self._arena_next_offset)
        return summary

    def reset_flow_mem_counters(self) -> None:
        """Zero the figure-(b) accounting. Called via cache.reset_stats()
        after prepopulation so the serving window's numbers are clean
        (08-31: backend counters otherwise include the fill phase)."""
        self._flow_output_bytes_total = 0
        self._flow_readback_bytes_total = 0
        self._flow_read_readback_bytes_total = 0
        self._flow_workbuf_peak_bytes = 0
        self._flow_mem_requests_total = 0
        self._flow_host_kernel_s_total = 0.0

    def __del__(self):
        try:
            self.client.close()
        except Exception:
            pass
        try:
            td = getattr(self, "temp_dir", None)
            if td is not None:
                td.cleanup()
        except Exception:
            pass  # partially-constructed backend (08-18 __del__ traceback)
