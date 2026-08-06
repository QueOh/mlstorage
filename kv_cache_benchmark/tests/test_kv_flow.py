"""A1W kv_flow arm tests against the device-faithful mock: hostnvm / pslm /
vslm write+read round-trips per workload mode, chunking, and store layout."""
import numpy as np
import pytest

from kv_cache import kv_kernels as K
from kv_cache.cpcs_backend import CPCSNVMeBackend

MODES = ["noop", "lossless_compress", "int8_quantize", "layout"]
FLOWS = ["hostnvm", "pslm", "vslm"]


def make_backend(tmp_path, flow, mode, **over):
    cfg = {
        "mode": mode, "client": "mock", "kv_flow": flow,
        "nvm_nsid": 2, "pslm_nsid": 101, "slm_nsid": 100, "cpcs_nsid": 200,
        "kv_exec_max_bytes": 8192,       # small -> force chunking
        "kv_vslm_arena_mb": 1, "kv_scratch_mb": 1, "kv_pslm_mr_mb": 1,
        "slm_rw_lba_bytes": 512, "direct_probe_lba_bytes": 512,
        "block_size_kb": 4,              # layout block 4096
    }
    cfg.update(over)
    return CPCSNVMeBackend(str(tmp_path / f"{flow}_{mode}"), cfg)


def sample_array(rng):
    # zero-runs + structure so RLE compresses and layout transposes
    v = np.zeros(6000, dtype="<f4")
    v[::7] = rng.standard_normal((len(v) + 6) // 7).astype("<f4") * 5
    return v


@pytest.mark.parametrize("flow", FLOWS)
@pytest.mark.parametrize("mode", MODES)
def test_flow_roundtrip(tmp_path, flow, mode):
    rng = np.random.default_rng(42)
    b = make_backend(tmp_path, flow, mode)
    data = sample_array(rng)
    t = b.write("k1", data)
    assert t.total >= t.device >= 0
    out, _ = b.read("k1")
    if mode == "int8_quantize":
        # lossy: must equal the host-kernel reference exactly (same algo
        # both sides), chunk by chunk
        raw = data.tobytes()
        cap = b._flow_chunk_bytes()
        ref = b"".join(K.quant_i8_decode(K.quant_i8_encode(raw[o:o + cap]))
                       if len(raw[o:o + cap]) % 4 == 0 and K.quant_i8_encode(raw[o:o + cap]) != raw[o:o + cap]
                       else raw[o:o + cap]
                       for o in range(0, len(raw), cap))
        assert out.tobytes() == ref
    else:
        assert np.array_equal(out, data)


@pytest.mark.parametrize("flow", FLOWS)
def test_flow_chunking_metadata(tmp_path, flow):
    b = make_backend(tmp_path, flow, "noop")
    data = np.arange(10000, dtype="<f4")  # 40000B > 8192 cap -> 5 chunks
    b.write("k", data)
    meta = b.metadata["k"]
    assert meta["kv_flow"] == flow
    assert len(meta["chunks"]) == 5
    assert meta["raw_size"] == 40000
    assert sum(c["raw_len"] for c in meta["chunks"]) == 40000


def test_hostnvm_store_bytes_match_host_kernel(tmp_path):
    b = make_backend(tmp_path, "hostnvm", "lossless_compress")
    data = sample_array(np.random.default_rng(1))
    b.write("k", data)
    meta = b.metadata["k"]
    assert meta["store_nsid"] == 2
    stored = b.client._ns_read(2, meta["store_off"], meta["packed_size"])
    raw = data.tobytes()
    cap = b._flow_chunk_bytes()
    ref = b"".join(K.rle_encode(raw[o:o + cap]) for o in range(0, len(raw), cap))
    assert stored == ref


def test_pslm_persists_to_nvm_ns(tmp_path):
    b = make_backend(tmp_path, "pslm", "lossless_compress")
    data = sample_array(np.random.default_rng(2))
    b.write("k", data)
    meta = b.metadata["k"]
    assert meta["store_nsid"] == 2          # host copy-out landed in NVM ns
    assert b.client._ns_store.get(2)        # bytes actually written
    assert b._flow_ranges[0]["nsid"] == 101  # scratch MRS on the pSLM ns


def test_vslm_persists_in_arena_no_copyout(tmp_path):
    b = make_backend(tmp_path, "vslm", "lossless_compress")
    data = sample_array(np.random.default_rng(3))
    b.write("k", data)
    meta = b.metadata["k"]
    assert meta["store_nsid"] == 100        # lives in the vSLM namespace
    assert 2 not in b.client._ns_store      # nothing went to the NVM ns
    # arena offsets are range-relative and non-overlapping
    offs = [c["rel_off"] for c in meta["chunks"]]
    assert offs == sorted(offs)
    # MRS: range 1 scratch + range 2 arena, both on nsid 100
    assert [r["nsid"] for r in b._flow_ranges] == [100, 100]


def test_vslm_arena_exhaustion_raises(tmp_path):
    b = make_backend(tmp_path, "vslm", "noop", kv_vslm_arena_mb=1)
    big = np.zeros(300000, dtype="<f4")  # 1.2MB > 1MB arena
    with pytest.raises(RuntimeError, match="arena exhausted"):
        b.write("k", big)


def test_flow_read_from_cold_metadata(tmp_path):
    b = make_backend(tmp_path, "hostnvm", "layout")
    data = sample_array(np.random.default_rng(4))
    b.write("k", data)
    b.metadata.clear()                       # force meta reload from disk
    out, _ = b.read("k")
    assert np.array_equal(out, data)
