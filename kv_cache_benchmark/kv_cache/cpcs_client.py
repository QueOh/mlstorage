"""
CPCS client abstractions.

This module provides:
  - Mock CPCS client for local tests/CI.
  - SPDK passthru CPCS client skeleton aligned with spdk_nvme_passthru usage.
"""

from __future__ import annotations

import io
import json
import re
import struct
import subprocess
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class CPCSResult:
    status: str
    output_offset: Optional[int]
    output_length: int
    bytes_in: int
    bytes_out: int
    device_compute_us: int
    command_latency_s: float
    extra: Dict[str, Any] = field(default_factory=dict)


class CPCSClient:
    """Abstract CPCS client interface."""

    def probe(self) -> Dict[str, Any]:
        raise NotImplementedError

    def ensure_runtime_ready(self) -> Dict[str, Any]:
        raise NotImplementedError

    def create_mrs(self, slm_nsid: int, ranges: List[Dict[str, int]], **kwargs: Any) -> int:
        raise NotImplementedError

    def load_program(
        self,
        *,
        cpcs_nsid: int,
        pind: int,
        program_bytes: bytes,
        ptype: int,
        pit: int,
        puid: int,
        chunk_bytes: int = 0,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def activate_program(self, *, cpcs_nsid: int, pind: int) -> Dict[str, Any]:
        raise NotImplementedError

    def execute(self, *, cpcs_nsid: int, rsid: int, pind: int, payload: bytes, **kwargs: Any) -> CPCSResult:
        raise NotImplementedError

    def slm_write(self, *, slm_nsid: int, offset_bytes: int, payload: bytes, **kwargs: Any) -> CPCSResult:
        raise NotImplementedError

    def slm_copy(self, *, slm_nsid: int, payload_desc: bytes, dest_offset: int, byte_len: int, **kwargs: Any) -> CPCSResult:
        raise NotImplementedError

    def slm_read(self, *, slm_nsid: int, offset_bytes: int, length_bytes: int, **kwargs: Any) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        return


class MockCPCSClient(CPCSClient):
    """Deterministic mock client for noop/compress/quantize flows."""

    def __init__(self, mode: str = "noop"):
        self.mode = str(mode)
        self._programs: Dict[str, int] = {}

    def probe(self) -> Dict[str, Any]:
        return {"status": "ok", "client": "mock", "mode": self.mode}

    def ensure_runtime_ready(self) -> Dict[str, Any]:
        return self.probe()

    def create_mrs(self, slm_nsid: int, ranges: List[Dict[str, int]], **kwargs: Any) -> int:
        _ = (slm_nsid, ranges, kwargs)
        return 1

    def load_program(
        self,
        *,
        cpcs_nsid: int,
        pind: int,
        program_bytes: bytes,
        ptype: int,
        pit: int,
        puid: int,
        chunk_bytes: int = 0,
    ) -> Dict[str, Any]:
        _ = (cpcs_nsid, ptype, pit, puid, chunk_bytes)
        self._programs[str(int(pind))] = int(len(program_bytes))
        return {
            "status": "ok",
            "pind": int(pind),
            "program_bytes": int(len(program_bytes)),
            "chunk_count": 1,
        }

    def activate_program(self, *, cpcs_nsid: int, pind: int) -> Dict[str, Any]:
        _ = cpcs_nsid
        if str(int(pind)) not in self._programs:
            return {"status": "error", "error": "program_not_loaded", "pind": int(pind)}
        return {"status": "ok", "pind": int(pind)}

    @staticmethod
    def _pack_quantized_npy(npy_payload: bytes) -> tuple[bytes, Dict[str, float]]:
        arr = np.load(io.BytesIO(npy_payload), allow_pickle=False)
        arr_fp32 = arr.astype(np.float32, copy=False)
        max_abs = float(np.max(np.abs(arr_fp32))) if arr_fp32.size else 0.0
        scale = max(max_abs / 127.0, 1e-12)
        q = np.round(arr_fp32 / scale).astype(np.int8)
        recon = q.astype(np.float32) * scale
        diff = np.abs(arr_fp32 - recon)
        metrics = {
            "max_abs_error": float(np.max(diff)) if diff.size else 0.0,
            "mean_abs_error": float(np.mean(diff)) if diff.size else 0.0,
            "mse": float(np.mean((arr_fp32 - recon) ** 2)) if diff.size else 0.0,
        }

        out = io.BytesIO()
        np.savez_compressed(
            out,
            quantized=q,
            scale=np.asarray([scale], dtype=np.float32),
            original_dtype=np.asarray([arr.dtype.str]),
            original_shape=np.asarray(arr.shape, dtype=np.int64),
        )
        return out.getvalue(), metrics

    @staticmethod
    def _unpack_quantized_npy(payload: bytes) -> bytes:
        with np.load(io.BytesIO(payload), allow_pickle=False) as packed:
            q = packed["quantized"].astype(np.int8, copy=False)
            scale = float(packed["scale"][0])
            dtype_str = str(packed["original_dtype"][0])
            shape = tuple(int(x) for x in packed["original_shape"].tolist())

        recon = (q.astype(np.float32) * scale).reshape(shape)
        restored = recon.astype(np.dtype(dtype_str), copy=False)
        out = io.BytesIO()
        np.save(out, restored, allow_pickle=False)
        return out.getvalue()

    def execute(self, *, cpcs_nsid: int, rsid: int, pind: int, payload: bytes, **kwargs: Any) -> CPCSResult:
        _ = (cpcs_nsid, rsid, pind)
        op = str(kwargs.get("op", "execute"))
        mode = str(kwargs.get("mode", self.mode))

        t0 = time.perf_counter()
        output = payload
        extra: Dict[str, Any] = {}

        if op in ("pack_store", "execute"):
            if mode == "lossless_compress":
                output = zlib.compress(payload, level=1)
                extra["pack_ratio"] = (len(payload) / max(len(output), 1))
            elif mode == "int8_quantize":
                output, qmetrics = self._pack_quantized_npy(payload)
                extra.update(qmetrics)
                extra["pack_ratio"] = (len(payload) / max(len(output), 1))
            else:
                output = payload
        elif op in ("unpack_load", "read"):
            if mode == "lossless_compress":
                output = zlib.decompress(payload)
            elif mode == "int8_quantize":
                output = self._unpack_quantized_npy(payload)
            else:
                output = payload

        elapsed = time.perf_counter() - t0
        extra["payload"] = output
        return CPCSResult(
            status="ok",
            output_offset=0,
            output_length=len(output),
            bytes_in=len(payload),
            bytes_out=len(output),
            device_compute_us=int(elapsed * 1_000_000.0),
            command_latency_s=elapsed,
            extra=extra,
        )

    def slm_write(self, *, slm_nsid: int, offset_bytes: int, payload: bytes, **kwargs: Any) -> CPCSResult:
        _ = (slm_nsid, offset_bytes, kwargs)
        return CPCSResult(
            status="ok",
            output_offset=int(offset_bytes),
            output_length=len(payload),
            bytes_in=len(payload),
            bytes_out=0,
            device_compute_us=0,
            command_latency_s=0.0,
            extra={"media_write_bytes": int(len(payload))},
        )

    def slm_copy(self, *, slm_nsid: int, payload_desc: bytes, dest_offset: int, byte_len: int, **kwargs: Any) -> CPCSResult:
        _ = (slm_nsid, payload_desc, dest_offset, byte_len, kwargs)
        return CPCSResult(
            status="ok",
            output_offset=dest_offset,
            output_length=int(byte_len),
            bytes_in=int(len(payload_desc)),
            bytes_out=0,
            device_compute_us=0,
            command_latency_s=0.0,
            extra={
                "media_read_bytes": int(byte_len),
                "media_write_bytes": int(byte_len),
            },
        )

    def slm_read(self, *, slm_nsid: int, offset_bytes: int, length_bytes: int, **kwargs: Any) -> bytes:
        _ = (slm_nsid, offset_bytes, kwargs)
        return bytes(int(max(0, length_bytes)))


class SpdkRpcBootstrap:
    """
    Optional helper for SPDK rpc.py bootstrap/probe calls.

    This wrapper is intentionally lightweight and does not enforce a specific
    lifecycle orchestration policy. Higher layers can opt into calling these
    methods when preparing `spdk_tgt` for passthru experiments.
    """

    def __init__(
        self,
        *,
        rpc_script: str = "scripts/rpc.py",
        rpc_python: str = "python3",
        rpc_socket: str = "",
        timeout_sec: float = 30.0,
    ):
        self.rpc_script = str(rpc_script)
        self.rpc_python = str(rpc_python)
        self.rpc_socket = str(rpc_socket or "")
        self.timeout_sec = float(timeout_sec)

    def _cmd(self, method: str, args: Optional[List[str]] = None) -> List[str]:
        cmd = [self.rpc_python, self.rpc_script]
        if self.rpc_socket:
            cmd.extend(["-s", self.rpc_socket])
        cmd.append(method)
        if args:
            cmd.extend(str(x) for x in args)
        return cmd

    def _run(self, method: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
        cmd = self._cmd(method, args)
        cp = subprocess.run(cmd, text=True, capture_output=True, timeout=self.timeout_sec)
        if cp.returncode != 0:
            raise RuntimeError(
                f"rpc.py failed (rc={cp.returncode})\n"
                f"cmd: {' '.join(cmd)}\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
            )
        out = (cp.stdout or "").strip()
        if not out:
            return {"raw": ""}
        try:
            return {"json": json.loads(out), "raw": out}
        except Exception:
            return {"raw": out}

    def get_rpc_methods(self) -> List[str]:
        payload = self._run("rpc_get_methods")
        data = payload.get("json", [])
        if isinstance(data, list):
            return [str(x) for x in data]
        return []

    def probe_runtime(self) -> Dict[str, Any]:
        methods = self.get_rpc_methods()
        summary: Dict[str, Any] = {
            "rpc_script": self.rpc_script,
            "rpc_socket": self.rpc_socket,
            "rpc_methods_count": len(methods),
            "has_nvmf_subsystem_query": "nvmf_get_subsystems" in methods,
            "has_cpcs_ns_create": "cpcs_ns_create" in methods,
            "has_program_install_builtins": "cpcs_program_install_builtins" in methods,
        }
        if "nvmf_get_subsystems" in methods:
            summary["nvmf_get_subsystems"] = self._run("nvmf_get_subsystems").get("json", [])
        return summary

    def verify_methods(self, required_methods: List[str]) -> Dict[str, Any]:
        methods = set(self.get_rpc_methods())
        req = [str(x) for x in required_methods]
        missing = [m for m in req if m not in methods]
        return {
            "required": req,
            "missing": missing,
            "ok": len(missing) == 0,
        }

    def install_builtins(self, subsystem_nqn: str, nsid: int) -> Dict[str, Any]:
        return self._run(
            "cpcs_program_install_builtins",
            ["--subsystem-nqn", str(subsystem_nqn), "--nsid", str(int(nsid))],
        )

    def list_programs(self, subsystem_nqn: str, nsid: int) -> Dict[str, Any]:
        return self._run(
            "cpcs_program_list",
            ["--subsystem-nqn", str(subsystem_nqn), "--nsid", str(int(nsid))],
        )

    def list_mrs(self, subsystem_nqn: str, nsid: int) -> Dict[str, Any]:
        return self._run(
            "cpcs_mrs_list",
            ["--subsystem-nqn", str(subsystem_nqn), "--nsid", str(int(nsid))],
        )


class SpdkPassthruCPCSClient(CPCSClient):
    """
    Real CPCS transport wrapper using spdk_nvme_passthru.

    This class is intentionally conservative for Milestone 2: it implements
    command framing and read-probe/execute primitives, while allowing higher
    layers to decide which CPCS PIND/payload format to use.
    """

    def __init__(
        self,
        *,
        spdk_nvme_passthru: str,
        trtype: str,
        traddr: str,
        trsvcid: str,
        subnqn: str,
        hostnqn: str,
        src_addr: str = "",
        src_svcid: str = "",
        passthru_lcores: str = "1",
        dataset_nsid: int = 1,
        direct_probe_offset: int = 0,
        direct_probe_length: int = 4096,
        direct_probe_lba_bytes: int = 4096,
        timeout_sec: float = 30.0,
    ):
        self.spdk_nvme_passthru = str(spdk_nvme_passthru)
        self.trtype = str(trtype)
        self.traddr = str(traddr)
        self.trsvcid = str(trsvcid)
        self.subnqn = str(subnqn)
        self.hostnqn = str(hostnqn)
        self.src_addr = str(src_addr or "")
        self.src_svcid = str(src_svcid or "")
        self.passthru_lcores = str(passthru_lcores or "1")
        self.dataset_nsid = int(dataset_nsid)
        self.direct_probe_offset = int(direct_probe_offset)
        self.direct_probe_length = int(direct_probe_length)
        self.direct_probe_lba_bytes = int(direct_probe_lba_bytes)
        self.timeout_sec = float(timeout_sec)

        if not self.spdk_nvme_passthru:
            raise ValueError("spdk_nvme_passthru path is required")
        if self.direct_probe_lba_bytes <= 0:
            raise ValueError("direct_probe_lba_bytes must be > 0")
        if self.direct_probe_offset < 0:
            raise ValueError("direct_probe_offset must be >= 0")
        if self.direct_probe_length <= 0:
            raise ValueError("direct_probe_length must be > 0")

    def _base_cmd(self, admin: bool) -> List[str]:
        mode = "--admin-cmd" if admin else "--io-cmd"
        cmd = [
            self.spdk_nvme_passthru,
            "--lcores",
            self.passthru_lcores,
            "--disable-cpumask-locks",
            "--no-rpc-server",
            mode,
            "--trtype",
            self.trtype,
            "--traddr",
            self.traddr,
            "--trsvcid",
            self.trsvcid,
            "--subnqn",
            self.subnqn,
            "--hostnqn",
            self.hostnqn,
        ]
        if self.src_addr:
            cmd.extend(["--src-addr", self.src_addr])
        if self.src_svcid:
            cmd.extend(["--src-svcid", self.src_svcid])
        return cmd

    def _run_checked(self, cmd: List[str]) -> str:
        cp = subprocess.run(cmd, text=True, capture_output=True, timeout=self.timeout_sec)
        if cp.returncode != 0:
            raise RuntimeError(
                f"spdk_nvme_passthru failed (rc={cp.returncode})\n"
                f"cmd: {' '.join(cmd)}\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
            )
        return (cp.stdout or "") + (cp.stderr or "")

    @staticmethod
    def _parse_result_hex(text: str) -> int:
        m = re.search(r"result[:=]\s*0x([0-9a-fA-F]+)", text)
        return int(m.group(1), 16) if m else 0

    @staticmethod
    def _align_up(value: int, align: int) -> int:
        return ((int(value) + int(align) - 1) // int(align)) * int(align)

    @staticmethod
    def _op_code(op: str) -> int:
        mapping = {
            "pack_store": 1,
            "unpack_load": 2,
            "layout_repack": 3,
            "block_select": 4,
            "prefix_lookup": 5,
            "batch_read": 6,
            "execute": 100,
        }
        return int(mapping.get(str(op), 0))

    @staticmethod
    def _mode_code(mode: str) -> int:
        mapping = {
            "off": 0,
            "noop": 1,
            "lossless_compress": 2,
            "int8_quantize": 3,
            "layout": 4,
            "block_select": 5,
            "prefix_index": 6,
        }
        return int(mapping.get(str(mode), 0))

    def _build_cpcs_request_payload(
        self,
        *,
        op: str,
        mode: str,
        payload: bytes,
        shape: Optional[List[int]] = None,
        dtype: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """
        Build a stable command payload envelope for CPCS programs.

        Envelope format (little-endian):
          - magic[8] = b"CPCSREQ1"
          - version_u32
          - op_u32
          - mode_u32
          - flags_u32
          - rank_u32
          - dtype_len_u32
          - extra_len_u32
          - payload_len_u64
          - shape[rank] (int64 each)
          - dtype bytes (utf-8)
          - extra json bytes (utf-8)
          - payload bytes
        """
        op_code = self._op_code(op)
        mode_code = self._mode_code(mode)

        flags = 0
        if str(mode) == "int8_quantize":
            flags |= 0x1
        if str(mode) == "lossless_compress":
            flags |= 0x2

        shape_vals = [int(x) for x in (shape or [])]
        dtype_bytes = str(dtype or "").encode("utf-8")
        extra_bytes = b""
        if extra:
            extra_bytes = json.dumps(extra, separators=(",", ":"), sort_keys=True).encode("utf-8")

        header = bytearray()
        header.extend(b"CPCSREQ1")
        header.extend(struct.pack("<IIIIIIIQ", 1, op_code, mode_code, flags, len(shape_vals), len(dtype_bytes), len(extra_bytes), len(payload)))
        for dim in shape_vals:
            header.extend(struct.pack("<q", int(dim)))
        header.extend(dtype_bytes)
        header.extend(extra_bytes)
        header.extend(payload)
        return bytes(header)

    def probe(self) -> Dict[str, Any]:
        passthru = Path(self.spdk_nvme_passthru)
        if not passthru.exists():
            raise FileNotFoundError(f"spdk_nvme_passthru not found: {passthru}")

        probe_offset = int(self.direct_probe_offset)
        probe_length = int(self.direct_probe_length)
        lba = int(self.direct_probe_lba_bytes)
        aligned_offset = (probe_offset // lba) * lba
        aligned_length = self._align_up(max(probe_length, lba), lba)
        _ = self._nvme_read_lba(
            nsid=self.dataset_nsid,
            offset_bytes=aligned_offset,
            length_bytes=aligned_length,
            lba_bytes=lba,
        )
        return {
            "status": "ok",
            "client": "spdk_passthru",
            "spdk_nvme_passthru": str(passthru),
            "trtype": self.trtype,
            "traddr": self.traddr,
            "trsvcid": self.trsvcid,
            "subnqn": self.subnqn,
            "hostnqn": self.hostnqn,
            "dataset_nsid": self.dataset_nsid,
            "direct_probe_offset": probe_offset,
            "direct_probe_length": probe_length,
            "direct_probe_offset_aligned": aligned_offset,
            "direct_probe_length_aligned": aligned_length,
            "direct_probe_lba_bytes": self.direct_probe_lba_bytes,
        }

    def ensure_runtime_ready(self) -> Dict[str, Any]:
        return self.probe()

    def create_mrs(self, slm_nsid: int, ranges: List[Dict[str, int]], **kwargs: Any) -> int:
        cpcs_nsid = int(kwargs.get("cpcs_nsid", 0) or 0)
        if cpcs_nsid <= 0:
            raise ValueError("cpcs_nsid must be > 0 for create_mrs")
        payload = bytearray()
        for item in ranges:
            start = int(item.get("starting_byte", item.get("offset", 0)))
            length = int(item.get("length", item.get("length_bytes", 0)))
            payload.extend(int(slm_nsid).to_bytes(4, byteorder="little", signed=False))
            payload.extend(int(length).to_bytes(4, byteorder="little", signed=False))
            payload.extend(int(start).to_bytes(8, byteorder="little", signed=False))
            payload.extend(bytes(16))

        with tempfile.NamedTemporaryFile(prefix="cpcs_mrs_", suffix=".bin", delete=False) as tf:
            tf.write(payload)
            tf.flush()
            path = tf.name
        try:
            cmd = self._base_cmd(admin=True) + [
                "--opcode",
                "0x89",
                "--nsid",
                str(int(cpcs_nsid)),
                "--cdw10",
                "0",
                "--cdw11",
                str(len(ranges)),
                "--data-len",
                str(len(payload)),
                "--write",
                "--input-file",
                path,
            ]
            out = self._run_checked(cmd)
            rsid = self._parse_result_hex(out)
            if rsid <= 0:
                raise RuntimeError(f"failed to parse RSID from output:\n{out}")
            return rsid
        finally:
            Path(path).unlink(missing_ok=True)

    def load_program(
        self,
        *,
        cpcs_nsid: int,
        pind: int,
        program_bytes: bytes,
        ptype: int,
        pit: int,
        puid: int,
        chunk_bytes: int = 0,
    ) -> Dict[str, Any]:
        total_size = int(len(program_bytes))
        if total_size <= 0:
            raise ValueError("program_bytes must be non-empty")
        chunk_size = int(chunk_bytes or 0)
        if chunk_size <= 0:
            chunk_size = total_size
        t0 = time.perf_counter()
        chunk_count = 0
        result_dw0 = 0
        offset = 0
        # SPCS demo_poc packing:
        # cdw10 = PIND[15:0] | PTYPE[23:16] | SEL[24] | PIT[27:25], SEL=0 for load.
        cdw10 = (
            (int(pind) & 0xFFFF)
            | ((int(ptype) & 0xFF) << 16)
            | ((0 & 0x1) << 24)
            | ((int(pit) & 0x7) << 25)
        )
        while offset < total_size:
            chunk = program_bytes[offset : offset + chunk_size]
            with tempfile.NamedTemporaryFile(prefix="cpcs_load_", suffix=".bin", delete=False) as tf:
                tf.write(chunk)
                tf.flush()
                path = tf.name
            try:
                cmd = self._base_cmd(admin=True) + [
                    "--opcode",
                    "0x85",
                    "--nsid",
                    str(int(cpcs_nsid)),
                    "--cdw10",
                    str(int(cdw10)),
                    "--cdw11",
                    str(total_size),
                    "--cdw12",
                    str(int(puid) & 0xFFFFFFFF),
                    "--cdw13",
                    str((int(puid) >> 32) & 0xFFFFFFFF),
                    "--cdw14",
                    str(len(chunk)),
                    "--cdw15",
                    str(offset),
                    "--data-len",
                    str(len(chunk)),
                    "--write",
                    "--input-file",
                    path,
                ]
                out = self._run_checked(cmd)
                result_dw0 = self._parse_result_hex(out)
                chunk_count += 1
            finally:
                Path(path).unlink(missing_ok=True)
            offset += len(chunk)

        elapsed = time.perf_counter() - t0
        return {
            "status": "ok",
            "pind": int(pind),
            "cpcs_nsid": int(cpcs_nsid),
            "program_bytes": total_size,
            "chunk_bytes": int(chunk_size),
            "chunk_count": int(chunk_count),
            "command_latency_s": float(elapsed),
            "result_dw0": int(result_dw0),
        }

    def activate_program(self, *, cpcs_nsid: int, pind: int) -> Dict[str, Any]:
        # SPCS demo_poc packing:
        # cdw10 = PIND[15:0] | SEL[16], SEL=1 for activate.
        cdw10 = (int(pind) & 0xFFFF) | (1 << 16)
        t0 = time.perf_counter()
        cmd = self._base_cmd(admin=True) + [
            "--opcode",
            "0x88",
            "--nsid",
            str(int(cpcs_nsid)),
            "--cdw10",
            str(int(cdw10)),
        ]
        out = self._run_checked(cmd)
        elapsed = time.perf_counter() - t0
        return {
            "status": "ok",
            "pind": int(pind),
            "cpcs_nsid": int(cpcs_nsid),
            "command_latency_s": float(elapsed),
            "result_dw0": self._parse_result_hex(out),
        }

    def execute(self, *, cpcs_nsid: int, rsid: int, pind: int, payload: bytes, **kwargs: Any) -> CPCSResult:
        op = str(kwargs.get("op", "execute"))
        mode = str(kwargs.get("mode", "noop"))
        shape_val = kwargs.get("shape", None)
        shape_list = [int(x) for x in shape_val] if shape_val is not None else []
        dtype_str = str(kwargs.get("dtype", ""))
        extra = kwargs.get("extra", None)
        extra_map: Optional[Dict[str, Any]] = None
        if isinstance(extra, dict):
            extra_map = dict(extra)

        framed_payload = self._build_cpcs_request_payload(
            op=op,
            mode=mode,
            payload=payload,
            shape=shape_list,
            dtype=dtype_str,
            extra=extra_map,
        )

        with tempfile.NamedTemporaryFile(prefix="cpcs_exec_", suffix=".bin", delete=False) as tf:
            tf.write(framed_payload)
            tf.flush()
            path = tf.name
        t0 = time.perf_counter()
        try:
            cmd = self._base_cmd(admin=False) + [
                "--opcode",
                "0x01",
                "--nsid",
                str(int(cpcs_nsid)),
                "--cdw2",
                str((int(rsid) << 16) | (int(pind) & 0xFFFF)),
                "--cdw3",
                "0",
                "--cdw4",
                str(len(framed_payload)),
                "--data-len",
                str(len(framed_payload)),
                "--write",
                "--input-file",
                path,
            ]
            out = self._run_checked(cmd)
            elapsed = time.perf_counter() - t0
            return CPCSResult(
                status="ok",
                output_offset=None,
                output_length=0,
                bytes_in=len(framed_payload),
                bytes_out=0,
                device_compute_us=int(elapsed * 1_000_000.0),
                command_latency_s=elapsed,
                extra={
                    "result_dw0": self._parse_result_hex(out),
                    "framed_request": True,
                    "request_op": op,
                    "request_mode": mode,
                    "request_payload_bytes": len(payload),
                    "request_framed_bytes": len(framed_payload),
                },
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def slm_write(self, *, slm_nsid: int, offset_bytes: int, payload: bytes, **kwargs: Any) -> CPCSResult:
        address_mode = str(kwargs.get("address_mode", "lba") or "lba").strip().lower()
        offset = int(offset_bytes)
        raw_payload = bytes(payload or b"")
        if offset < 0:
            raise ValueError("offset_bytes must be >= 0")
        if not raw_payload:
            return CPCSResult(
                status="ok",
                output_offset=offset,
                output_length=0,
                bytes_in=0,
                bytes_out=0,
                device_compute_us=0,
                command_latency_s=0.0,
                extra={},
            )

        write_payload = raw_payload
        cmd_extra: List[str] = []
        if address_mode == "byte":
            cmd_extra.extend(
                [
                    "--cdw10",
                    str(offset & 0xFFFFFFFF),
                    "--cdw11",
                    str((offset >> 32) & 0xFFFFFFFF),
                    "--cdw12",
                    str(len(write_payload)),
                ]
            )
        elif address_mode == "lba":
            lba = int(kwargs.get("lba_bytes", self.direct_probe_lba_bytes) or self.direct_probe_lba_bytes)
            if lba <= 0:
                raise ValueError("lba_bytes must be > 0 for slm_write address_mode=lba")
            if (offset % lba) != 0:
                raise ValueError(f"offset_bytes must align to lba_bytes={lba} for address_mode=lba")
            padded_len = self._align_up(len(write_payload), lba)
            if padded_len > len(write_payload):
                write_payload = write_payload + bytes(padded_len - len(write_payload))
            slba = offset // lba
            nlb = (len(write_payload) // lba) - 1
            cmd_extra.extend(
                [
                    "--cdw10",
                    str(slba & 0xFFFFFFFF),
                    "--cdw11",
                    str((slba >> 32) & 0xFFFFFFFF),
                    "--cdw12",
                    str(nlb),
                ]
            )
        else:
            raise ValueError(f"Unsupported slm_write address mode: {address_mode}")

        with tempfile.NamedTemporaryFile(prefix="slm_write_", suffix=".bin", delete=False) as tf:
            tf.write(write_payload)
            tf.flush()
            path = tf.name
        t0 = time.perf_counter()
        try:
            cmd = self._base_cmd(admin=False) + [
                "--opcode",
                "0x01",
                "--nsid",
                str(int(slm_nsid)),
            ] + cmd_extra + [
                "--data-len",
                str(len(write_payload)),
                "--write",
                "--input-file",
                path,
            ]
            self._run_checked(cmd)
            elapsed = time.perf_counter() - t0
            return CPCSResult(
                status="ok",
                output_offset=offset,
                output_length=len(raw_payload),
                bytes_in=len(raw_payload),
                bytes_out=0,
                device_compute_us=int(elapsed * 1_000_000.0),
                command_latency_s=elapsed,
                extra={
                    "address_mode": address_mode,
                    "wire_bytes": len(write_payload),
                    "media_write_bytes": int(len(write_payload)),
                },
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def slm_copy(self, *, slm_nsid: int, payload_desc: bytes, dest_offset: int, byte_len: int, **kwargs: Any) -> CPCSResult:
        cdw12 = int(kwargs.get("cdw12", 0))
        with tempfile.NamedTemporaryFile(prefix="slm_copy_", suffix=".bin", delete=False) as tf:
            tf.write(payload_desc)
            tf.flush()
            path = tf.name
        t0 = time.perf_counter()
        try:
            cmd = self._base_cmd(admin=False) + [
                "--opcode",
                "0x01",
                "--nsid",
                str(int(slm_nsid)),
                "--cdw2",
                str(int(byte_len) & 0xFFFFFFFF),
                "--cdw3",
                str((int(byte_len) >> 32) & 0xFFFFFFFF),
                "--cdw10",
                str(int(dest_offset) & 0xFFFFFFFF),
                "--cdw11",
                str((int(dest_offset) >> 32) & 0xFFFFFFFF),
                "--cdw12",
                str(cdw12),
                "--data-len",
                str(len(payload_desc)),
                "--write",
                "--input-file",
                path,
            ]
            self._run_checked(cmd)
            elapsed = time.perf_counter() - t0
            return CPCSResult(
                status="ok",
                output_offset=int(dest_offset),
                output_length=int(byte_len),
                bytes_in=len(payload_desc),
                bytes_out=0,
                device_compute_us=int(elapsed * 1_000_000.0),
                command_latency_s=elapsed,
                extra={
                    "media_read_bytes": int(byte_len),
                    "media_write_bytes": int(byte_len),
                },
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def _nvme_read_lba(self, *, nsid: int, offset_bytes: int, length_bytes: int, lba_bytes: int) -> bytes:
        offset = int(offset_bytes)
        length = int(length_bytes)
        lba = int(lba_bytes)
        if offset < 0:
            raise ValueError("offset_bytes must be >= 0")
        if length <= 0:
            raise ValueError("length_bytes must be > 0")
        if lba <= 0:
            raise ValueError("lba_bytes must be > 0")
        if (offset % lba) != 0 or (length % lba) != 0:
            raise ValueError("offset/length must be aligned to lba_bytes")

        slba = offset // lba
        nlb = (length // lba) - 1

        with tempfile.NamedTemporaryFile(prefix="nvme_read_", suffix=".bin", delete=False) as tf:
            out_path = tf.name
        try:
            cmd = self._base_cmd(admin=False) + [
                "--opcode",
                "0x02",
                "--nsid",
                str(int(nsid)),
                "--cdw10",
                str(slba & 0xFFFFFFFF),
                "--cdw11",
                str((slba >> 32) & 0xFFFFFFFF),
                "--cdw12",
                str(nlb),
                "--data-len",
                str(length),
                "--read",
                "--output-file",
                out_path,
            ]
            self._run_checked(cmd)
            return Path(out_path).read_bytes()
        finally:
            Path(out_path).unlink(missing_ok=True)

    def slm_read(self, *, slm_nsid: int, offset_bytes: int, length_bytes: int, **kwargs: Any) -> bytes:
        offset = int(offset_bytes)
        length = int(length_bytes)
        if offset < 0:
            raise ValueError("offset_bytes must be >= 0")
        if length <= 0:
            raise ValueError("length_bytes must be > 0")
        address_mode = str(kwargs.get("address_mode", "byte") or "byte").strip().lower()
        if address_mode == "lba":
            lba = int(kwargs.get("lba_bytes", self.direct_probe_lba_bytes) or self.direct_probe_lba_bytes)
            return self._nvme_read_lba(
                nsid=int(slm_nsid),
                offset_bytes=offset,
                length_bytes=length,
                lba_bytes=lba,
            )
        if address_mode != "byte":
            raise ValueError(f"Unsupported slm_read address mode: {address_mode}")

        with tempfile.NamedTemporaryFile(prefix="slm_read_", suffix=".bin", delete=False) as tf:
            out_path = tf.name
        try:
            cmd = self._base_cmd(admin=False) + [
                "--opcode",
                "0x02",
                "--nsid",
                str(int(slm_nsid)),
                "--cdw10",
                str(offset & 0xFFFFFFFF),
                "--cdw11",
                str((offset >> 32) & 0xFFFFFFFF),
                "--cdw12",
                str(length),
                "--data-len",
                str(length),
                "--read",
                "--output-file",
                out_path,
            ]
            self._run_checked(cmd)
            return Path(out_path).read_bytes()
        finally:
            Path(out_path).unlink(missing_ok=True)
