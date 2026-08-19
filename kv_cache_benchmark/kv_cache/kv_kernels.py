"""Host-side KV transform kernels, byte-compatible with the CPCS device
builtins (spdk lib/nvmf/cpcs/cpcs_kernels_core.c, formats KVL1/KVQ1/KVR1).

These serve two roles in the A1W offload study:
  1. the REAL compute of the `hostnvm` arm (the host burns CPU here), and
  2. the golden oracle for all arms (device output must equal these bytes).

Byte-compatibility notes (mirrored from the C kernels — do not "improve"):
  - RLE (KVL1): 16B header {magic 'KVL1', kind=1, raw_len le32, encoded_len
    le32}; body = runs of [tag u8][run le16] with tag 1 = zero-run (no data)
    and tag 0 = literal run followed by the bytes; runs cap at 65535.
    INCOMPRESSIBLE FALLBACK: when the encoded stream would be >= the input
    length, the kernel returns the RAW INPUT VERBATIM (no header).
  - Quant (KVQ1): input is interpreted as a LITTLE-ENDIAN F32 STREAM
    regardless of the tensor's semantic dtype; when len % 4 != 0 the kernel
    passes the input through verbatim. scale = max|v|/127 (or 1.0 when all
    zero); q = lroundf(v / scale) clamped to [-127, 127] — lroundf rounds
    ties AWAY from zero (numpy's default rounds ties to even; do not use it).
    16B header {magic 'KVQ1', kind=2, elem_count le32, scale_bits le32}.
  - Layout (KVR1): 16B header {magic 'KVR1', kind=3, block_size le32,
    raw_len le32}; body = element-index-major transpose of the input viewed
    as ceil(raw_len/block_size) blocks, with the ragged tail's missing
    elements SKIPPED (stream is exactly raw_len bytes). When
    in_len <= block_size the kernel passes the input through verbatim.
"""
from __future__ import annotations

import struct

import numpy as np

MAGIC_LOSSLESS = b"KVL1"
MAGIC_QUANT = b"KVQ1"
MAGIC_LAYOUT = b"KVR1"
KIND_LOSSLESS_RLE = 1
KIND_QUANT_I8 = 2
KIND_LAYOUT_TRANSPOSE = 3
DEFAULT_BLOCK_SIZE = 4096
MAX_BLOCK_SIZE = 1024 * 1024
_HDR = struct.Struct("<4sB3xII")  # magic, kind, pad, two u32 fields
_RUN_CAP = 0xFFFF


def _runs(data: np.ndarray):
    """Yield (is_zero, start, length) runs over a uint8 array."""
    if data.size == 0:
        return
    zero = data == 0
    boundaries = np.flatnonzero(np.diff(zero.view(np.int8))) + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [data.size]))
    for s, e in zip(starts, ends):
        yield bool(zero[s]), int(s), int(e - s)


def rle_encode(payload: bytes) -> bytes:
    data = np.frombuffer(payload, dtype=np.uint8)
    if data.size > 0xFFFFFFFF:
        raise ValueError("payload too large for KVL1")
    parts = [b""]  # header patched last
    body_len = 0
    for is_zero, start, length in _runs(data):
        off = 0
        while off < length:
            run = min(length - off, _RUN_CAP)
            if is_zero:
                parts.append(struct.pack("<BH", 1, run))
                body_len += 3
            else:
                parts.append(struct.pack("<BH", 0, run))
                parts.append(data[start + off:start + off + run].tobytes())
                body_len += 3 + run
            off += run
    total = _HDR.size + body_len
    if total >= len(payload):
        return bytes(payload)  # incompressible fallback: raw input verbatim
    parts[0] = _HDR.pack(MAGIC_LOSSLESS, KIND_LOSSLESS_RLE,
                         len(payload), body_len)
    return b"".join(parts)


def rle_decode(packed: bytes) -> bytes:
    if len(packed) < _HDR.size:
        raise ValueError("short KVL1 payload")
    magic, kind, raw_len, encoded_len = _HDR.unpack_from(packed)
    if magic != MAGIC_LOSSLESS or kind != KIND_LOSSLESS_RLE:
        raise ValueError("not a KVL1 payload")
    if encoded_len > len(packed) - _HDR.size:
        raise ValueError("KVL1 encoded_len out of range")
    out = bytearray(raw_len)
    pos, produced, end = _HDR.size, 0, _HDR.size + encoded_len
    while pos < end:
        if pos + 3 > end:
            raise ValueError("truncated KVL1 run header")
        tag = packed[pos]
        run = packed[pos + 1] | (packed[pos + 2] << 8)
        pos += 3
        if run == 0 or produced + run > raw_len:
            raise ValueError("KVL1 run out of range")
        if tag == 1:
            produced += run  # already zero
            continue
        if tag != 0 or pos + run > end:
            raise ValueError("bad KVL1 literal run")
        out[produced:produced + run] = packed[pos:pos + run]
        pos += run
        produced += run
    if produced != raw_len:
        raise ValueError("KVL1 produced != raw_len")
    return bytes(out)


def _lroundf_away(r32: np.ndarray) -> np.ndarray:
    """lroundf semantics: nearest integer, ties away from zero. Compute in
    float64 over the exact float32 quotients (exact representation)."""
    r64 = r32.astype(np.float64)
    return np.sign(r64) * np.floor(np.abs(r64) + 0.5)


def quant_i8_encode(payload: bytes) -> bytes:
    if len(payload) == 0 or len(payload) % 4 != 0:
        return bytes(payload)  # device passthrough for non-f32-multiple input
    v = np.frombuffer(payload, dtype="<f4")
    if v.size > 0xFFFFFFFF:
        raise ValueError("payload too large for KVQ1")
    # The KVQ1 contract reinterprets RAW bytes as an f32 stream; FP16 KV
    # state reinterpreted this way yields arbitrary bit patterns including
    # NaN/inf, which produced RuntimeWarnings and nondeterministic int8
    # casts (08-18 A1 night). Sanitize non-finite lanes to 0.0 so the host
    # reference is deterministic; quantize equality claims apply to the
    # sanitized stream (documented in analysis_guide_a1.md).
    if not np.all(np.isfinite(v)):
        v = np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)
    max_abs = np.float32(np.max(np.abs(v))) if v.size else np.float32(0.0)
    scale = np.float32(max_abs / np.float32(127.0)) if max_abs > 0 else np.float32(1.0)
    r32 = (v / scale).astype(np.float32)
    q = np.clip(_lroundf_away(r32), -127, 127).astype(np.int8)
    hdr = _HDR.pack(MAGIC_QUANT, KIND_QUANT_I8, v.size,
                    int(np.float32(scale).view(np.uint32)))
    return hdr + q.tobytes()


def quant_i8_decode(packed: bytes) -> bytes:
    if len(packed) < _HDR.size:
        raise ValueError("short KVQ1 payload")
    magic, kind, elem_count, scale_bits = _HDR.unpack_from(packed)
    if magic != MAGIC_QUANT or kind != KIND_QUANT_I8:
        raise ValueError("not a KVQ1 payload")
    if elem_count > len(packed) - _HDR.size:
        raise ValueError("KVQ1 elem_count out of range")
    scale = np.uint32(scale_bits).view(np.float32)
    if not (scale > 0) or not np.isfinite(scale):
        raise ValueError("bad KVQ1 scale")
    q = np.frombuffer(packed, dtype=np.int8, count=elem_count,
                      offset=_HDR.size).astype(np.float32)
    return (q * scale).astype("<f4").tobytes()


def _transpose_index(raw_len: int, block_size: int) -> np.ndarray:
    """Source indices in output order: for i in range(block_size) for b in
    range(nblocks): b*block_size + i, skipping >= raw_len (ragged tail)."""
    nblocks = (raw_len + block_size - 1) // block_size
    idx = (np.arange(nblocks, dtype=np.int64)[None, :] * block_size
           + np.arange(block_size, dtype=np.int64)[:, None]).ravel()
    return idx[idx < raw_len]


def layout_encode(payload: bytes, block_size: int = DEFAULT_BLOCK_SIZE) -> bytes:
    if block_size <= 0 or block_size > MAX_BLOCK_SIZE:
        block_size = DEFAULT_BLOCK_SIZE
    if len(payload) == 0 or len(payload) <= block_size:
        return bytes(payload)  # device passthrough for single-block input
    if len(payload) > 0xFFFFFFFF:
        raise ValueError("payload too large for KVR1")
    data = np.frombuffer(payload, dtype=np.uint8)
    body = data[_transpose_index(len(payload), block_size)]
    hdr = _HDR.pack(MAGIC_LAYOUT, KIND_LAYOUT_TRANSPOSE, block_size,
                    len(payload))
    return hdr + body.tobytes()


def layout_decode(packed: bytes) -> bytes:
    if len(packed) < _HDR.size:
        raise ValueError("short KVR1 payload")
    magic, kind, block_size, raw_len = _HDR.unpack_from(packed)
    if magic != MAGIC_LAYOUT or kind != KIND_LAYOUT_TRANSPOSE:
        raise ValueError("not a KVR1 payload")
    if block_size <= 0 or block_size > MAX_BLOCK_SIZE:
        raise ValueError("bad KVR1 block_size")
    if raw_len > len(packed) - _HDR.size:
        raise ValueError("KVR1 raw_len out of range")
    body = np.frombuffer(packed, dtype=np.uint8, count=raw_len,
                         offset=_HDR.size)
    out = np.empty(raw_len, dtype=np.uint8)
    out[_transpose_index(raw_len, block_size)] = body
    return out.tobytes()


# mode name -> (encode, decode); passthrough-capable by design
KERNELS = {
    "lossless_compress": (rle_encode, rle_decode),
    "int8_quantize": (quant_i8_encode, quant_i8_decode),
    "layout": (layout_encode, layout_decode),
    "noop": (lambda p: bytes(p), lambda p: bytes(p)),
}


def encode(mode: str, payload: bytes, block_size: int = DEFAULT_BLOCK_SIZE) -> bytes:
    if mode == "layout":
        return layout_encode(payload, block_size)
    enc, _ = KERNELS.get(mode, KERNELS["noop"])
    return enc(payload)


def decode(mode: str, packed: bytes) -> bytes:
    _, dec = KERNELS.get(mode, KERNELS["noop"])
    try:
        return dec(packed)
    except ValueError:
        # device passthrough cases (incompressible RLE, non-f32 quant,
        # single-block layout) come back without a header
        return bytes(packed)
