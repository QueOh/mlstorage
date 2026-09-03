"""P3 device-arm unit tests: CPCSSLM1 descriptor wire format (must
mirror the firmware ABI at spdk-p2 0357b7e7b), client cparam1 flag
emission, offload-stage unlock matrix, prefix device parity plumbing."""
import struct
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

np = pytest.importorskip("numpy")

from kv_cache.workload import parse_offload_stages  # noqa: E402
from kv_cache.cpcs_backend import CPCSNVMeBackend  # noqa: E402


def _desc(*args, **kwargs):
    # _slm_descriptor only touches class-level constants -> call unbound
    return CPCSNVMeBackend._slm_descriptor(object.__new__(CPCSNVMeBackend),
                                           *args, **kwargs)


def test_slm_descriptor_wire_layout_matches_firmware():
    """Byte-for-byte mirror of the firmware's ut_kv_slm_fill: magic at 0,
    op u32 at 8, entry_count u32 at 12, flags u64 at 16, 48B entries."""
    d = _desc("batch_read",
              [{"src_mr": 2, "src_off": 4096, "len": 512, "raw_len": 512,
                "mode": 1},
               {"src_mr": 2, "src_off": 8192, "dst_mr": 3, "dst_off": 64,
                "len": 256, "raw_len": 1024, "mode": 3}],
              flags=7)
    assert d[:8] == b"CPCSSLM1"
    assert struct.unpack_from("<I", d, 8)[0] == 6      # op batch_read
    assert struct.unpack_from("<I", d, 12)[0] == 2     # entry_count
    assert struct.unpack_from("<Q", d, 16)[0] == 7     # flags
    assert len(d) == 24 + 2 * 48
    e0 = struct.unpack_from("<QQQQQII", d, 24)
    assert e0 == (2, 4096, 0, 0, 512, 512, 1)
    e1 = struct.unpack_from("<QQQQQII", d, 24 + 48)
    assert e1 == (2, 8192, 3, 64, 256, 1024, 3)


def test_slm_descriptor_ops_and_queries():
    qs = [0x1122334455667788, 0xAABBCCDDEEFF0011]
    d = _desc("prefix_lookup",
              [{"src_mr": 1, "src_off": 0, "len": 128}],
              flags=len(qs), queries=qs)
    assert struct.unpack_from("<I", d, 8)[0] == 5      # op prefix_lookup
    assert struct.unpack_from("<Q", d, 16)[0] == 2     # flags = n queries
    tail = 24 + 48
    assert struct.unpack_from("<Q", d, tail)[0] == qs[0]
    assert struct.unpack_from("<Q", d, tail + 8)[0] == qs[1]
    assert struct.unpack_from("<I", _desc("block_select", [], 0), 8)[0] == 4
    assert struct.unpack_from("<I", _desc("migrate", [], 0), 8)[0] == 7


def test_offload_stage_device_unlock_matrix():
    d = parse_offload_stages("s2=device,s4=device,s6=device")
    assert d["s2"] == "device" and d["s4"] == "device" and d["s6"] == "device"
    for blocked in ("s3=device", "s5=device"):
        with pytest.raises(ValueError, match="not wired"):
            parse_offload_stages(blocked)


def test_client_cparam1_flag_emission():
    import itertools
    import threading
    from kv_cache.cpcs_client import SpdkPassthruCPCSClient
    captured = {}

    client = object.__new__(SpdkPassthruCPCSClient)
    client.spdk_nvme_passthru = "/bin/true"
    client.trtype = "TCP"
    client.traddr = "127.0.0.1"
    client.trsvcid = "4420"
    client.subnqn = "nqn.test"
    client.hostnqn = ""
    client.src_addr = ""
    client.src_svcid = ""
    client.timeout_s = 5
    client._lcore_lock = threading.Lock()
    client._lcore_iter = itertools.cycle(["1"])

    def fake_run_checked(cmd):
        captured["cmd"] = list(cmd)
        return "Command completed: result=0x0000000400000004"

    client._run_checked = fake_run_checked
    res = client.execute(cpcs_nsid=200, rsid=1, pind=10, payload=b"x" * 8,
                         op="block_select", mode="noop",
                         extra={"output_mr_id": 1, "output_offset": 0,
                                "output_length": 64},
                         cparam1=1)
    cmd = captured["cmd"]
    assert "--cdw10" in cmd and cmd[cmd.index("--cdw10") + 1] == "1"
    assert "--cdw11" in cmd and cmd[cmd.index("--cdw11") + 1] == "0"
    assert res is not None

    captured.clear()
    client.execute(cpcs_nsid=200, rsid=1, pind=7, payload=b"x" * 8,
                   op="pack_store", mode="noop", extra={})
    assert "--cdw10" not in captured["cmd"]  # legacy: no flags emitted


def test_prefix_device_parity_counts_with_fake_backend():
    from kv_cache.prefix_cache import PrefixCacheManager
    from kv_cache.models import MODEL_CONFIGS

    class FakeBackend:
        def __init__(self):
            self.images = []

        def prefix_lookup_device(self, image, queries):
            self.images.append((len(image), len(queries)))
            # exact device semantics: match any query hash against the
            # published records' leading u64
            recs = {struct.unpack_from("<Q", image, off)[0]
                    for off in range(0, len(image), 32)}
            return [b"\x00" * 32 for q in queries if q in recs][:1]

    class FakeCache:
        def __init__(self):
            self.backends = {"nvme": FakeBackend()}

    class Req:
        user_id = "user_42"
        context_tokens = 4096

    mgr = PrefixCacheManager(FakeCache(), mode="device")
    model = list(MODEL_CONFIGS.values())[0]
    # first sight: host misses (registers); device sees the just-published
    # image and must also miss -> parity holds either way it's counted
    mgr.check_prefix_cache(Req(), model)
    e1, m1 = mgr.stats['device_lookup_errors'], mgr.stats['device_parity_mismatches']
    # second sight: host hits; device scans the registered record -> hit
    mgr.check_prefix_cache(Req(), model)
    assert mgr.stats['device_lookups'] == 2
    assert mgr.stats['device_lookup_errors'] == e1 == 0
    assert mgr.stats['device_parity_mismatches'] == m1 == 0


def test_select_tail_chunks_slot_grouping_consistency():
    """Entries handed to the device must reference slot-relative offsets;
    _arena_slot_of + _slot_arena partition the global arena space."""
    be = object.__new__(CPCSNVMeBackend)
    be.kv_flow_rsids = 4
    be.kv_vslm_arena_bytes = 16 * 4096 * 4
    slot_size = be._slot_arena(0)[1]
    assert slot_size == 16 * 4096
    for off, want in ((0, 0), (slot_size - 1, 0), (slot_size, 1),
                      (3 * slot_size + 5, 3), (10 * slot_size, 3)):
        assert be._arena_slot_of(off) == want
