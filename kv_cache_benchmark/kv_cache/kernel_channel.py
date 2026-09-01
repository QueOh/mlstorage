"""Persistent kernel-initiator NVMe-oF channel (2026-09-01).

Replaces the per-command spdk_nvme_passthru PROCESS SPAWN (~300-430 ms
of fork + SPDK/DPDK EAL init + fabric connect per command) with ONE
kernel nvme-tcp connection at first use and an NVME_IOCTL_*64_CMD
ioctl per command (~tens of microseconds + transfer). No spdk changes;
the target sees a standard NVMe-oF host.

Mechanics:
- `nvme connect` (nvme-cli) once; controllers found via
  /sys/class/nvme/*/subsysnqn. IO commands go to the per-namespace
  GENERIC char devices /dev/ng*n* -- these exist even for namespaces
  whose command set the kernel does not support (CSI-3 SLM, the CPCS
  compute ns), which is exactly what they are for. Admin commands go
  to the controller char device /dev/nvmeX.
- nsid resolution via ioctl NVME_IOCTL_ID on each /dev/ng* node
  (returns the nsid), filtered to our subsystem NQN via sysfs.
- The 64-bit ioctls return CQE DW0|DW1<<32 in `result` -- MORE than
  the spawn path ever surfaced (the tool prints only DW0), so crc32
  side-band values become real.
- `run_passthru_argv()` accepts the exact spdk_nvme_passthru argv the
  existing clients already build and returns the same stdout text the
  tool would print ("Command completed: result=0x..." /
  "Command failed: sct=0x%x sc=0x%x") -- drop-in at the spawn funnel.

Requires root (arbitrary-opcode passthrough on ng nodes needs
CAP_SYS_ADMIN) and nvme-cli; both are givens under the campaign's
sudo-run convention. A sibling copy of this file lives at
experiments/real_apps/common/kernel_nvme_channel.py for the A2M
runner/stager -- keep them in sync.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# struct nvme_passthru_cmd64 (linux/nvme_ioctl.h), 80 bytes
_CMD64_FMT = "<BBHIIIQQIIIIIIIIIIQ"
assert struct.calcsize(_CMD64_FMT) == 80
_IOCTL_ADMIN64 = 0xC0504E47  # _IOWR('N', 0x47, nvme_passthru_cmd64)
_IOCTL_IO64 = 0xC0504E48     # _IOWR('N', 0x48, nvme_passthru_cmd64)
_IOCTL_ID = 0x4E40           # _IO('N', 0x40) -> returns nsid


class KernelChannelError(RuntimeError):
    pass


class Dw4Required(KernelChannelError):
    """The command carries a nonzero SQE DW4 (e.g. CPCS Execute: dlen
    rides the MPTR field), which the kernel passthru ioctl cannot
    convey (the kernel owns MPTR) -- the caller must fall back to the
    spawn path. Full execute coverage needs either a firmware
    dlen==0 -> transport-length fallback (one-liner, operator
    decision) or a passthru daemon mode (initiator-only spdk change,
    also an operator decision)."""


class NvmeStatusError(KernelChannelError):
    """Command completed with a non-zero NVMe status (sct/sc)."""

    def __init__(self, status: int):
        self.status = int(status)
        self.sc = self.status & 0xFF
        self.sct = (self.status >> 8) & 0x7
        super().__init__(f"sct=0x{self.sct:x} sc=0x{self.sc:x}")


class KernelNvmeChannel:
    def __init__(self, *, trtype: str, traddr: str, trsvcid: str,
                 subnqn: str, hostnqn: str = "",
                 timeout_ms: int = 600000):
        self.trtype = str(trtype or "tcp").lower()
        self.traddr = str(traddr)
        self.trsvcid = str(trsvcid)
        self.subnqn = str(subnqn)
        self.hostnqn = str(hostnqn or "")
        self.timeout_ms = int(timeout_ms)
        self._lock = threading.Lock()
        self._ctrl_dev: Optional[str] = None
        self._ns_fds: Dict[int, int] = {}
        self._ctrl_fd: Optional[int] = None
        if os.geteuid() != 0:
            raise KernelChannelError(
                "kernel channel needs root (arbitrary-opcode ioctls on "
                "/dev/ng* require CAP_SYS_ADMIN) -- run under sudo")
        if shutil.which("nvme") is None:
            raise KernelChannelError(
                "nvme-cli not found -- the kernel channel connects via "
                "'nvme connect'; install nvme-cli on the initiator")

    # ---- discovery / connection ---------------------------------------
    @staticmethod
    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _find_controllers(self) -> List[str]:
        out = []
        for c in sorted(Path("/sys/class/nvme").glob("nvme*")):
            if self._read(c / "subsysnqn") == self.subnqn:
                addr = self._read(c / "address")
                if not self.traddr or f"traddr={self.traddr}" in addr:
                    out.append(c.name)
        return out

    def _ng_subsysnqn(self, ng: Path) -> str:
        node = (ng / "device").resolve()
        for _ in range(4):
            nqn = self._read(node / "subsysnqn")
            if nqn:
                return nqn
            node = node.parent
        return ""

    def _map_namespaces(self) -> Dict[int, str]:
        found: Dict[int, str] = {}
        for ng in sorted(Path("/sys/class/nvme-generic").glob("ng*")):
            if self._ng_subsysnqn(ng) != self.subnqn:
                continue
            dev = f"/dev/{ng.name}"
            try:
                fd = os.open(dev, os.O_RDONLY)
            except OSError:
                continue
            try:
                nsid = fcntl.ioctl(fd, _IOCTL_ID)
                found[int(nsid)] = dev
            except OSError:
                pass
            finally:
                os.close(fd)
        return found

    def _connect(self) -> None:
        cmd = ["nvme", "connect", "-t", self.trtype, "-a", self.traddr,
               "-s", self.trsvcid, "-n", self.subnqn,
               "--ctrl-loss-tmo", "10"]
        if self.hostnqn:
            cmd += ["-q", self.hostnqn]
        cp = subprocess.run(cmd, text=True, capture_output=True,
                            timeout=60)
        if cp.returncode != 0 and "already connected" not in (
                (cp.stderr or "") + (cp.stdout or "")).lower():
            raise KernelChannelError(
                f"nvme connect failed rc={cp.returncode}: "
                f"{(cp.stderr or cp.stdout or '').strip()[:300]}")

    def ensure_connected(self) -> None:
        with self._lock:
            if self._ctrl_dev and self._ns_fds:
                return
            self._ensure_connected_locked()

    def _ensure_connected_locked(self) -> None:
        ctrls = self._find_controllers()
        if not ctrls:
            self._connect()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            ctrls = self._find_controllers()
            ns_map = self._map_namespaces() if ctrls else {}
            if ctrls and ns_map:
                self._ctrl_dev = f"/dev/{ctrls[0]}"
                self._close_fds_locked()
                for nsid, dev in ns_map.items():
                    self._ns_fds[nsid] = os.open(dev, os.O_RDWR)
                self._ctrl_fd = os.open(self._ctrl_dev, os.O_RDWR)
                return
            time.sleep(0.25)
        raise KernelChannelError(
            f"kernel channel: no controller/namespaces for {self.subnqn} "
            f"at {self.traddr}:{self.trsvcid} after connect (is the "
            "target up? does the kernel expose /dev/ng* nodes?)")

    def _close_fds_locked(self) -> None:
        for fd in self._ns_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self._ns_fds.clear()
        if self._ctrl_fd is not None:
            try:
                os.close(self._ctrl_fd)
            except OSError:
                pass
            self._ctrl_fd = None

    def _invalidate(self) -> None:
        with self._lock:
            self._close_fds_locked()
            self._ctrl_dev = None

    # ---- submission ----------------------------------------------------
    def submit(self, *, admin: bool, opcode: int, nsid: int,
               cdws: Dict[int, int], data: Optional[bytes],
               data_len: int, is_write: bool,
               timeout_ms: Optional[int] = None) -> Tuple[int, bytes]:
        """One command; returns (result_u64, read_data). Retries once
        through a reconnect on transport death (per-cell target
        restarts sever the kernel controller)."""
        try:
            return self._submit_once(admin=admin, opcode=opcode,
                                     nsid=nsid, cdws=cdws, data=data,
                                     data_len=data_len,
                                     is_write=is_write,
                                     timeout_ms=timeout_ms)
        except OSError:
            self._invalidate()
            self.ensure_connected()
            return self._submit_once(admin=admin, opcode=opcode,
                                     nsid=nsid, cdws=cdws, data=data,
                                     data_len=data_len,
                                     is_write=is_write,
                                     timeout_ms=timeout_ms)

    def _submit_once(self, *, admin: bool, opcode: int, nsid: int,
                     cdws: Dict[int, int], data: Optional[bytes],
                     data_len: int, is_write: bool,
                     timeout_ms: Optional[int]) -> Tuple[int, bytes]:
        self.ensure_connected()
        with self._lock:
            if admin:
                fd = self._ctrl_fd
            else:
                fd = self._ns_fds.get(int(nsid))
        if fd is None:
            raise KernelChannelError(
                f"kernel channel: nsid {nsid} has no /dev/ng* node "
                f"(known: {sorted(self._ns_fds)})")
        buf = None
        addr = 0
        if data_len > 0:
            buf = ctypes.create_string_buffer(data_len)
            if is_write and data:
                buf[:len(data)] = data
            addr = ctypes.addressof(buf)
        raw = bytearray(struct.pack(
            _CMD64_FMT, opcode & 0xFF, 0, 0, int(nsid),
            cdws.get(2, 0), cdws.get(3, 0), 0, addr, 0, int(data_len),
            cdws.get(10, 0), cdws.get(11, 0), cdws.get(12, 0),
            cdws.get(13, 0), cdws.get(14, 0), cdws.get(15, 0),
            int(timeout_ms or self.timeout_ms), 0, 0))
        rc = fcntl.ioctl(fd, _IOCTL_ADMIN64 if admin else _IOCTL_IO64,
                         raw, True)
        if rc != 0:
            raise NvmeStatusError(rc)
        result = struct.unpack_from("<Q", raw, 72)[0]
        out = bytes(buf.raw) if (buf is not None and not is_write) else b""
        return result, out

    # ---- drop-in argv translation --------------------------------------
    def run_passthru_argv(self, cmd: List[str]) -> str:
        """Execute an spdk_nvme_passthru argv via the channel and return
        the stdout text the tool would have printed. Raises
        RuntimeError-compatible errors on failure (message carries the
        tool's 'Command failed: sct=... sc=...' line so existing
        pattern-matching retry/diagnosis logic keeps working)."""
        admin = "--admin-cmd" in cmd
        opcode = nsid = data_len = 0
        cdws: Dict[int, int] = {}
        is_write = "--write" in cmd
        in_file = out_file = None
        i = 0
        while i < len(cmd):
            tok = cmd[i]

            def val() -> str:
                return cmd[i + 1]

            if tok == "--opcode":
                opcode = int(val(), 0)
                i += 2
            elif tok == "--nsid":
                nsid = int(val(), 0)
                i += 2
            elif tok.startswith("--cdw") and tok[5:].isdigit():
                cdws[int(tok[5:])] = int(val(), 0) & 0xFFFFFFFF
                i += 2
            elif tok == "--data-len":
                data_len = int(val(), 0)
                i += 2
            elif tok == "--input-file":
                in_file = val()
                i += 2
            elif tok == "--output-file":
                out_file = val()
                i += 2
            elif tok in ("--write", "--read", "--admin-cmd", "--io-cmd",
                         "--disable-cpumask-locks", "--no-rpc-server",
                         "--hex-dump"):
                i += 1
            elif tok in ("--lcores", "-s", "--trtype", "--traddr",
                         "--trsvcid", "--subnqn", "--hostnqn",
                         "--src-addr", "--src-svcid"):
                i += 2  # identity/env flags -- fixed at channel setup
            else:
                i += 1  # tool path / unknown positional
        if cdws.get(4):
            raise Dw4Required(
                "argv sets --cdw4 (SQE DW4/MPTR) -- not conveyable via "
                "the kernel ioctl; use the spawn path for this command")
        data = None
        if is_write and in_file:
            data = Path(in_file).read_bytes()
            if data_len <= 0:
                data_len = len(data)
        try:
            result, out = self.submit(admin=admin, opcode=opcode,
                                      nsid=nsid, cdws=cdws, data=data,
                                      data_len=data_len,
                                      is_write=is_write)
        except NvmeStatusError as exc:
            raise RuntimeError(
                f"Command failed: sct=0x{exc.sct:x} sc=0x{exc.sc:x} "
                f"(kernel channel)") from exc
        if (not is_write) and out_file and data_len > 0:
            Path(out_file).write_bytes(out[:data_len])
        return f"Command completed: result=0x{result:016x}\n"

    def close(self) -> None:
        self._invalidate()


_CHANNEL_CACHE: Dict[Tuple[str, str, str], "KernelNvmeChannel"] = {}
_CACHE_LOCK = threading.Lock()


def _argv_identity(cmd: List[str]) -> Dict[str, str]:
    out = {}
    for i, tok in enumerate(cmd):
        if tok in ("--trtype", "--traddr", "--trsvcid", "--subnqn",
                   "--hostnqn") and i + 1 < len(cmd):
            out[tok[2:]] = cmd[i + 1]
    return out

def run_argv_cached(cmd: List[str]) -> str:
    """Drop-in for a spawn funnel: build/reuse a channel keyed by the
    argv's fabric identity, then run the command through it."""
    ident = _argv_identity(cmd)
    key = (ident.get("traddr", ""), ident.get("trsvcid", ""),
           ident.get("subnqn", ""))
    with _CACHE_LOCK:
        ch = _CHANNEL_CACHE.get(key)
        if ch is None:
            ch = KernelNvmeChannel(
                trtype=ident.get("trtype", "tcp"),
                traddr=key[0], trsvcid=key[1], subnqn=key[2],
                hostnqn=ident.get("hostnqn", ""))
            _CHANNEL_CACHE[key] = ch
    return ch.run_passthru_argv(cmd)
