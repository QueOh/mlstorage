"""Format-fixture tests for kv_kernels: byte-compatibility with the CPCS
device builtins (spdk cpcs_kernels_core.c, KVL1/KVQ1/KVR1). These pin the
exact wire bytes so a device/host divergence fails loudly offline."""
import struct

import numpy as np
import pytest

from kv_cache import kv_kernels as K


# ---------------------------------------------------------------- RLE / KVL1
def test_rle_exact_bytes_zero_and_literal_runs():
    # NOTE: must be genuinely compressible — the device (and our mirror)
    # returns the RAW input whenever header+body >= raw_len.
    payload = b"\x00" * 100 + b"AB" + b"\x00" * 50 + b"C"
    packed = K.rle_encode(payload)
    # body = [zero x100] + [lit x2 'AB'] + [zero x50] + [lit x1 'C']
    #      = 3 + (3+2) + 3 + (3+1) = 15
    assert packed[:16] == struct.pack("<4sB3xII", b"KVL1", 1, 153, 15)
    body = packed[16:]
    assert body == (struct.pack("<BH", 1, 100)
                    + struct.pack("<BH", 0, 2) + b"AB"
                    + struct.pack("<BH", 1, 50)
                    + struct.pack("<BH", 0, 1) + b"C")
    assert K.rle_decode(packed) == payload


def test_rle_small_payload_hits_fallback_like_device():
    payload = b"\x00\x00\x00AB\x00C"      # 7B: 16B header alone exceeds it
    assert K.rle_encode(payload) == payload


def test_rle_incompressible_returns_raw_verbatim():
    rng = np.random.default_rng(7)
    payload = rng.integers(1, 255, 4096, dtype=np.uint8).tobytes()
    packed = K.rle_encode(payload)
    assert packed == payload            # device fallback: raw, no header
    assert K.decode("lossless_compress", packed) == payload  # passthrough


def test_rle_run_cap_65535():
    payload = b"\x00" * 70000
    packed = K.rle_encode(payload)
    body = packed[16:]
    assert body[:3] == struct.pack("<BH", 1, 65535)
    assert body[3:6] == struct.pack("<BH", 1, 70000 - 65535)
    assert K.rle_decode(packed) == payload


def test_rle_roundtrip_mixed():
    rng = np.random.default_rng(11)
    chunks = []
    for _ in range(50):
        if rng.random() < 0.5:
            chunks.append(b"\x00" * int(rng.integers(1, 300)))
        else:
            chunks.append(rng.integers(1, 255, int(rng.integers(1, 300)),
                                       dtype=np.uint8).tobytes())
    payload = b"".join(chunks)
    assert K.rle_decode(K.rle_encode(payload)) == payload


# -------------------------------------------------------------- Quant / KVQ1
def test_quant_header_and_scale_bits():
    v = np.array([0.0, 1.0, -2.0, 127.0], dtype="<f4")
    packed = K.quant_i8_encode(v.tobytes())
    magic, kind, elem_count, scale_bits = struct.unpack_from("<4sB3xII", packed)
    assert (magic, kind, elem_count) == (b"KVQ1", 2, 4)
    scale = np.uint32(scale_bits).view(np.float32)
    assert scale == np.float32(np.float32(127.0) / np.float32(127.0))  # 1.0
    q = np.frombuffer(packed, dtype=np.int8, offset=16)
    assert list(q) == [0, 1, -2, 127]


def test_quant_lroundf_ties_away_from_zero():
    # scale = 2.0 (max_abs 254); 1.0/2.0 = 0.5 exactly -> lroundf = 1 (away),
    # numpy's default round-half-even would give 0.
    v = np.array([254.0, 1.0, -1.0], dtype="<f4")
    packed = K.quant_i8_encode(v.tobytes())
    q = np.frombuffer(packed, dtype=np.int8, offset=16)
    assert list(q) == [127, 1, -1]


def test_quant_clamp_and_roundtrip_domain():
    rng = np.random.default_rng(3)
    v = (rng.standard_normal(4096) * 10).astype("<f4")
    packed = K.quant_i8_encode(v.tobytes())
    out = np.frombuffer(K.quant_i8_decode(packed), dtype="<f4")
    scale = np.uint32(struct.unpack_from("<I", packed, 12)[0]).view(np.float32)
    assert np.max(np.abs(out - v)) <= scale / 2 + 1e-6
    # int8-domain round-trip is EXACT: re-encoding the decoded values must
    # reproduce the same codes (the cross-arm correctness check)
    q1 = np.frombuffer(packed, dtype=np.int8, offset=16)
    packed2 = K.quant_i8_encode(out.tobytes())
    q2 = np.frombuffer(packed2, dtype=np.int8, offset=16)
    assert np.array_equal(q1, q2)


def test_quant_non_f32_multiple_passthrough():
    payload = b"abc"  # len % 4 != 0
    assert K.quant_i8_encode(payload) == payload
    assert K.decode("int8_quantize", payload) == payload


# ------------------------------------------------------------- Layout / KVR1
def test_layout_exact_bytes_ragged_tail():
    # raw_len 10, block_size 4 -> blocks [0..3][4..7][8..9]
    payload = bytes(range(10))
    packed = K.layout_encode(payload, block_size=4)
    assert packed[:16] == struct.pack("<4sB3xII", b"KVR1", 3, 4, 10)
    # element-major: i=0: 0,4,8; i=1: 1,5,9; i=2: 2,6(,skip); i=3: 3,7(,skip)
    assert packed[16:] == bytes([0, 4, 8, 1, 5, 9, 2, 6, 3, 7])
    assert K.layout_decode(packed) == payload


def test_layout_single_block_passthrough():
    payload = b"tiny"
    assert K.layout_encode(payload, block_size=4096) == payload
    assert K.decode("layout", payload) == payload


def test_layout_roundtrip_large():
    rng = np.random.default_rng(5)
    payload = rng.integers(0, 255, 1 << 20, dtype=np.uint8).tobytes()
    packed = K.layout_encode(payload, block_size=4096)
    assert len(packed) == 16 + len(payload)
    assert K.layout_decode(packed) == payload


# ------------------------------------------------------------------ dispatch
@pytest.mark.parametrize("mode", ["lossless_compress", "int8_quantize",
                                  "layout", "noop"])
def test_encode_decode_dispatch(mode):
    v = (np.arange(2048, dtype="<f4") % 7 - 3).tobytes()
    packed = K.encode(mode, v)
    assert K.decode(mode, packed) == v if mode != "int8_quantize" else True
    if mode == "int8_quantize":
        out = K.decode(mode, packed)
        assert len(out) == len(v)
