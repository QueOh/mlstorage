"""P1 host-arm unit tests: offload-stages parsing, decode read policy,
tail-chunk selection, real RAG retrieval, real prefix index."""
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

np = pytest.importorskip("numpy")

from kv_cache.workload import (  # noqa: E402
    parse_offload_stages, validate_decode_read_policy)
from kv_cache.cpcs_backend import CPCSNVMeBackend  # noqa: E402
from kv_cache.rag import RAGDocumentManager, RAG_EMBED_DIM  # noqa: E402
from kv_cache.prefix_cache import PrefixMatcher  # noqa: E402


# ---- parse_offload_stages / decode policy ------------------------------

def test_parse_offload_stages_defaults_off():
    d = parse_offload_stages("")
    assert d == {k: "off" for k in ("s2", "s3", "s4", "s5", "s6")}


def test_parse_offload_stages_mixed():
    d = parse_offload_stages("s2=host, s5=HOST")
    assert d["s2"] == "host" and d["s5"] == "host"
    assert d["s3"] == "off" and d["s4"] == "off" and d["s6"] == "off"


def test_parse_offload_stages_rejects_unknown_and_device():
    with pytest.raises(ValueError, match="unknown stage"):
        parse_offload_stages("s9=host")
    with pytest.raises(ValueError, match="invalid mode"):
        parse_offload_stages("s2=fast")
    with pytest.raises(ValueError, match="not wired yet"):
        parse_offload_stages("s2=device")


def test_decode_read_policy_validation():
    assert validate_decode_read_policy("full") == "full"
    assert validate_decode_read_policy("tail:0.5") == "tail:0.5"
    with pytest.raises(ValueError):
        validate_decode_read_policy("tail:0")
    with pytest.raises(ValueError):
        validate_decode_read_policy("tail:1.5")
    with pytest.raises(ValueError):
        validate_decode_read_policy("window:4")


# ---- tail-chunk selection ----------------------------------------------

def _chunks(raw_lens):
    off = 0
    out = []
    for i, r in enumerate(raw_lens):
        out.append({"rel_off": off, "packed_len": r, "raw_len": r,
                    "idx": i})
        off += r
    return out


def test_select_tail_chunks_covers_fraction_from_the_end():
    cs = _chunks([100, 100, 100, 100])
    sel = CPCSNVMeBackend._select_tail_chunks(cs, 0.5)
    assert [c["idx"] for c in sel] == [2, 3]
    sel = CPCSNVMeBackend._select_tail_chunks(cs, 0.51)
    assert [c["idx"] for c in sel] == [1, 2, 3]


def test_select_tail_chunks_full_and_minimum():
    cs = _chunks([64, 64])
    assert len(CPCSNVMeBackend._select_tail_chunks(cs, 1.0)) == 2
    sel = CPCSNVMeBackend._select_tail_chunks(cs, 0.01)
    assert [c["idx"] for c in sel] == [1]  # at least one chunk, the last


def test_select_tail_chunks_order_preserved():
    cs = _chunks([10, 20, 30])
    sel = CPCSNVMeBackend._select_tail_chunks(cs, 0.8)
    assert [c["idx"] for c in sel] == sorted(c["idx"] for c in sel)


# ---- S5: real RAG retrieval --------------------------------------------

def test_rag_embeddings_deterministic_and_normalized():
    a = RAGDocumentManager._chunk_embedding("docA", 3)
    b = RAGDocumentManager._chunk_embedding("docA", 3)
    c = RAGDocumentManager._chunk_embedding("docA", 4)
    assert a.shape == (RAG_EMBED_DIM,) and a.dtype == np.float32
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)
    assert abs(float(np.linalg.norm(a)) - 1.0) < 1e-5


def test_rag_topk_matches_numpy_reference_and_ties_stable():
    rng = np.random.default_rng(11)
    emb = rng.standard_normal((32, RAG_EMBED_DIM)).astype(np.float32)
    q = RAGDocumentManager.query_embedding(1234)
    got = RAGDocumentManager.topk_host(emb, q, 5)
    ref = list(np.argsort(-(emb @ q), kind="stable")[:5])
    assert got == [int(i) for i in ref]
    # exact ties break by lower index
    emb2 = np.stack([q, q, q]).astype(np.float32)
    assert RAGDocumentManager.topk_host(emb2, q, 2) == [0, 1]


def test_rag_query_embedding_deterministic():
    assert np.array_equal(RAGDocumentManager.query_embedding(7),
                          RAGDocumentManager.query_embedding(7))
    assert not np.array_equal(RAGDocumentManager.query_embedding(7),
                              RAGDocumentManager.query_embedding(8))


# ---- S6: real prefix index ---------------------------------------------

def test_prefix_real_miss_then_hit_same_user():
    m = PrefixMatcher()
    assert m.detect_system_prompt_real("user_1") is None  # registers
    hit = m.detect_system_prompt_real("user_1")
    assert hit is not None
    assert hit.token_count > 0
    assert hit.use_count == 1


def test_prefix_real_cross_user_same_prompt_shares():
    m = PrefixMatcher()
    # find two users with the same deterministic prompt id
    uid_by_prompt = {}
    pair = None
    for i in range(64):
        pid = m._user_prompt_id(f"u{i}")
        if pid in uid_by_prompt:
            pair = (uid_by_prompt[pid], f"u{i}")
            break
        uid_by_prompt[pid] = f"u{i}"
    assert pair is not None
    assert m.detect_system_prompt_real(pair[0]) is None
    assert m.detect_system_prompt_real(pair[1]) is not None  # cross-user hit


def test_prefix_real_deterministic_no_rng():
    m1, m2 = PrefixMatcher(), PrefixMatcher()
    seq1 = [m1.detect_system_prompt_real(f"u{i}") is None for i in range(20)]
    seq2 = [m2.detect_system_prompt_real(f"u{i}") is None for i in range(20)]
    assert seq1 == seq2


def test_prefix_publish_records_32b():
    m = PrefixMatcher()
    m.detect_system_prompt_real("user_1")
    m.detect_system_prompt_real("user_2")
    img = m.publish_records()
    assert len(img) % 32 == 0
    assert len(img) >= 32
