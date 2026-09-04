"""S3 (arena compaction) + S5 (device RAG top-k) unit tests."""
import json
import struct
import sys
import os
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

np = pytest.importorskip("numpy")

from kv_cache.cpcs_backend import CPCSNVMeBackend  # noqa: E402
from kv_cache.rag import RAGDocumentManager, RAG_EMBED_DIM  # noqa: E402


# ---- S5: eval-family wire golden ---------------------------------------

def test_eval_topk_request_wire_sizes():
    hdr = struct.pack("<IHHQQII", 1, 2, (1 << 1) | (1 << 2), 1, 0, 160, 0)
    body = struct.pack("<QQQQQIIIHHHHII", 1, 0, 1, 4096, 300,
                       32, 64, 256, 1, 5, 0, 0, 256, 0)
    assert len(hdr) == 32       # spdk_cpcs_builtin_eval_req_header
    assert len(hdr + body) == 100  # filtered_topk_exact req static assert
    rec = struct.pack("<QIIHHfII", 7, 0, 0, 0, 0, 0.0, 0, 7)
    assert len(rec) == 32       # cpcs_builtin_metadata_record


def test_rag_device_images_layout():
    class Doc:
        embeddings = np.random.default_rng(3).standard_normal(
            (5, RAG_EMBED_DIM)).astype(np.float32)
        chunks = list(range(5))
    meta, vec = RAGDocumentManager._device_images(Doc())
    assert len(meta) == 5 * 32
    assert len(vec) == 5 * RAG_EMBED_DIM * 4
    # doc_id and vector_index both equal the chunk index
    for i in range(5):
        did = struct.unpack_from("<Q", meta, i * 32)[0]
        vidx = struct.unpack_from("<I", meta, i * 32 + 28)[0]
        assert did == i and vidx == i


def _mk_manager(mode="device", backend=None):
    class FakeCache:
        def __init__(self):
            self.backends = {"nvme": backend}
    return RAGDocumentManager(FakeCache(), mode=mode)


class _Doc:
    def __init__(self, n=8):
        rng = np.random.default_rng(11)
        self.embeddings = rng.standard_normal(
            (n, RAG_EMBED_DIM)).astype(np.float32)
        self.chunks = list(range(n))


def test_rag_device_parity_and_fallback():
    doc = _Doc()
    q = RAGDocumentManager.query_embedding(9)
    host = RAGDocumentManager.topk_host(doc.embeddings, q, 5)

    class GoodBackend:
        def rag_topk_device(self, m, v, qb, k, dim):
            assert dim == RAG_EMBED_DIM and k == 5
            return list(host)
    mgr = _mk_manager(backend=GoodBackend())
    got = mgr._retrieve_device(doc, q, host)
    assert got == list(host)
    assert mgr.stats["rag_device_lookups"] == 1
    assert mgr.stats["rag_device_mismatches"] == 0

    class WrongBackend:
        def rag_topk_device(self, m, v, qb, k, dim):
            # replace the top id with the WORST id -> a real mismatch
            scores = doc.embeddings @ q
            worst = int(np.argmin(scores))
            out = list(host)
            out[0] = worst
            return out
    mgr = _mk_manager(backend=WrongBackend())
    mgr._retrieve_device(doc, q, host)
    assert mgr.stats["rag_device_mismatches"] == 1

    class BoomBackend:
        def rag_topk_device(self, m, v, qb, k, dim):
            raise RuntimeError("boom")
    mgr = _mk_manager(backend=BoomBackend())
    assert mgr._retrieve_device(doc, q, host) is None
    assert mgr.stats["rag_device_errors"] == 1


# ---- S3: arena compaction ----------------------------------------------

def _mk_backend(tmp_path, arena):
    be = object.__new__(CPCSNVMeBackend)
    be.kv_flow = "vslm"
    be.kv_flow_rsids = 1
    be.kv_vslm_arena_bytes = 1 << 20     # one 1 MiB slot
    be.kv_scratch_bytes = 0
    be.slm_rw_lba_bytes = 4096
    be.slm_nsid = 100
    be._flow_alloc_lock = threading.Lock()
    be._flow_free = {"vslm": {0: [(4096, 4096)]}, "nvm": []}
    be._flow_resv = {"vslm": {}, "nvm": {}}
    be._vslm_arena_next = {0: 0}
    be.offload_stages = {"s3": "host"}
    be._maintenance_compactions = 0
    be._maintenance_extents_moved = 0
    be.metadata = {}
    be._meta_path = lambda k: tmp_path / f"{k}.meta.json"

    def fake_read(nsid, off, length, mmode):
        return bytes(arena[off:off + length])

    def fake_write(nsid, off, data, mmode):
        arena[off:off + len(data)] = data
    be._flow_store_read = fake_read
    be._flow_store_write = fake_write
    return be


def test_compact_slot_host_arm_moves_and_rebuilds(tmp_path):
    arena = bytearray(1 << 20)
    be = _mk_backend(tmp_path, arena)
    # two live extents with a hole between them (fragmentation)
    a = b"A" * 100
    b = b"B" * 200
    arena[8192:8192 + len(a)] = a          # extent 1 at 8192
    arena[40960:40960 + len(b)] = b        # extent 2 at 40960
    be._flow_resv["vslm"] = {8192: 8192, 40960: 8192}
    be.metadata = {
        "k1": {"kv_flow": "vslm", "storage_mode": "kv_flow",
               "chunks": [{"rel_off": 8192, "packed_len": 100,
                           "raw_len": 100}]},
        "k2": {"kv_flow": "vslm", "storage_mode": "kv_flow",
               "chunks": [{"rel_off": 40960, "packed_len": 200,
                           "raw_len": 200}]},
    }
    moved = be._flow_compact_slot(0, device=False)
    assert moved >= 1
    # extents now packed from the slot base, ascending, page-aligned
    o1 = be.metadata["k1"]["chunks"][0]["rel_off"]
    o2 = be.metadata["k2"]["chunks"][0]["rel_off"]
    assert o1 == 0 and o2 == 8192
    # bytes travelled with the extents
    assert bytes(arena[o1:o1 + 100]) == a
    assert bytes(arena[o2:o2 + 200]) == b
    # allocator state rebuilt
    assert be._flow_free["vslm"][0] == []
    assert be._vslm_arena_next[0] == 8192 + 8192
    assert set(be._flow_resv["vslm"]) == {0, 8192}
    # meta persisted
    assert json.loads((tmp_path / "k1.meta.json").read_text())[
        "chunks"][0]["rel_off"] == 0


def test_alloc_with_maintenance_retries_after_compaction(tmp_path):
    arena = bytearray(1 << 20)
    be = _mk_backend(tmp_path, arena)
    calls = {"n": 0}

    def fake_alloc(length, slot=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("vSLM arena slice exhausted -- test")
        return 12345
    be._flow_alloc = fake_alloc
    be._flow_compact_slot = lambda slot, device: 3
    off = be._flow_alloc_with_maintenance(4096, 0)
    assert off == 12345
    assert be._maintenance_compactions == 1
    assert be._maintenance_extents_moved == 3
