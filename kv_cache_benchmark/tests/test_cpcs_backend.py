#!/usr/bin/env python3
"""Unit tests for CPCS backend wiring."""

import argparse
import json

import numpy as np
import pytest

from kv_cache.cpcs_backend import CPCSNVMeBackend
from kv_cache.cache import MultiTierCache
from kv_cache.cpcs_metrics import CPCSMetrics
from kv_cache.models import MODEL_CONFIGS
from kv_cache.workload import validate_args


def _base_args() -> argparse.Namespace:
    return argparse.Namespace(
        num_users=1,
        duration=1,
        gpu_mem_gb=0.0,
        cpu_mem_gb=0.0,
        rag_num_docs=0,
        max_conversations=1,
        max_concurrent_allocs=0,
        request_rate=0.0,
        max_requests=0,
        storage_capacity_gb=0.0,
        precondition_size_gb=0.0,
        precondition_threads=0,
        trace_speedup=1.0,
        replay_cycles=0,
        target_saturation=0.5,
        num_gpus=1,
        tensor_parallel=1,
        cache_dir=None,
        nvme_backend='file',
        cpcs_mode='off',
        cpcs_client='mock',
        cpcs_storage_mode='file',
        cpcs_verify_every_n=0,
        cpcs_lossy_tolerance=0.0,
        spdk_inventory='',
        spdk_rpc_script='scripts/rpc.py',
        spdk_rpc_python='python3',
        spdk_rpc_socket='',
        bootstrap_subsystem_nqn='',
        cpcs_bootstrap_check=False,
        cpcs_bootstrap_install_builtins=False,
        cpcs_bootstrap_list_programs=False,
        cpcs_bootstrap_list_mrs=False,
        cpcs_required_rpc_methods='',
        traddr='',
        trsvcid='',
        subnqn='',
        hostnqn='',
        spdk_nvme_passthru='',
        dataset_nsid=0,
        slm_nsid=0,
        cpcs_nsid=0,
        cpcs_program_pind=0,
        cpcs_rsid=1,
        direct_probe_offset=0,
        direct_probe_length=4096,
        direct_probe_lba_bytes=0,
        cpcs_program_pind_pack_store=-1,
        cpcs_program_pind_unpack_load=-1,
        cpcs_program_pind_layout_repack=-1,
        cpcs_program_pind_block_select=-1,
        cpcs_program_pind_prefix_lookup=-1,
        cpcs_program_pind_batch_read=-1,
        cpcs_rsid_pack_store=-1,
        cpcs_rsid_unpack_load=-1,
        cpcs_rsid_layout_repack=-1,
        cpcs_rsid_block_select=-1,
        cpcs_rsid_prefix_lookup=-1,
        cpcs_rsid_batch_read=-1,
        cpcs_auto_create_mrs=False,
        cpcs_mrs_ranges='',
        cpcs_mrs_default_length_bytes=65536,
        cpcs_mrs_align_bytes=0,
        cpcs_mrs_align_mode='round',
        cpcs_load_program_path='',
        cpcs_load_program_pind=-1,
        cpcs_load_program_set_default_pind=False,
        cpcs_load_program_chunk_bytes=0,
        cpcs_load_program_ptype=0xC0,
        cpcs_load_program_pit=0x01,
        cpcs_load_program_puid=0xEBF00001,
        cpcs_activate_loaded_program=False,
        cpcs_slm_rw_lba_bytes=0,
        cpcs_slm_read_address_mode='byte',
        cpcs_slm_write_address_mode='lba',
    )


def test_validate_args_cpcs_mock_minimal_passes():
    args = _base_args()
    args.nvme_backend = 'cpcs'
    args.cpcs_mode = 'noop'
    args.cpcs_client = 'mock'
    validate_args(args)


def test_validate_args_spdk_passthru_requires_transport_fields():
    args = _base_args()
    args.nvme_backend = 'cpcs'
    args.cpcs_mode = 'noop'
    args.cpcs_client = 'spdk_passthru'
    with pytest.raises(ValueError):
        validate_args(args)


def test_cpcs_noop_roundtrip(tmp_path):
    backend = CPCSNVMeBackend(
        base_path=str(tmp_path),
        cpcs_config={
            'mode': 'noop',
            'client': 'mock',
            'verify_every_n': 1,
        },
    )
    src = (np.arange(128, dtype=np.float32).reshape(32, 4) / 10.0).astype(np.float16)
    backend.write("k0", src)
    out, _ = backend.read("k0")

    assert np.array_equal(src, out)
    summary = backend.get_metrics_summary()
    assert summary["cpcs_commands_total"] == 2
    assert summary["cpcs_commands_failed"] == 0


def test_cpcs_quantize_roundtrip_shape_dtype_and_metrics(tmp_path):
    backend = CPCSNVMeBackend(
        base_path=str(tmp_path),
        cpcs_config={
            'mode': 'int8_quantize',
            'client': 'mock',
        },
    )
    rng = np.random.default_rng(42)
    src = rng.normal(size=(64, 8)).astype(np.float16)
    backend.write("k1", src)
    out, _ = backend.read("k1")

    assert out.shape == src.shape
    assert out.dtype == src.dtype
    assert np.max(np.abs(out.astype(np.float32) - src.astype(np.float32))) < 0.2

    summary = backend.get_metrics_summary()
    assert summary["cpcs_commands_total"] == 2
    assert "cpcs_extra_totals" in summary
    assert summary["cpcs_extra_totals"].get("max_abs_error", 0.0) > 0.0


def test_multitier_cache_uses_cpcs_backend_when_enabled():
    cache = MultiTierCache(
        model_config=MODEL_CONFIGS['tiny-1b'],
        gpu_memory_gb=0.0,
        cpu_memory_gb=0.1,
        nvme_backend='cpcs',
        cpcs_config={'mode': 'noop', 'client': 'mock'},
        seed=7,
    )
    assert isinstance(cache.backends['nvme'], CPCSNVMeBackend)


def test_cpcs_metrics_selected_blocks_ratio():
    metrics = CPCSMetrics()
    metrics.record(
        ok=True,
        bytes_in=64,
        bytes_out=64,
        command_latency_s=0.001,
        device_compute_us=1000.0,
        mode="block_select",
        extra={
            "selector_total_blocks": 10,
            "selector_selected_blocks": 4,
        },
    )
    summary = metrics.to_dict()
    assert summary["cpcs_selected_blocks_ratio"] == pytest.approx(0.4, rel=1e-6)


def test_mrs_range_round_alignment_normalizes_ranges(tmp_path):
    backend = CPCSNVMeBackend(
        base_path=str(tmp_path),
        cpcs_config={
            'mode': 'noop',
            'client': 'mock',
            'mrs_align_mode': 'round',
            'mrs_align_bytes': 4096,
        },
    )
    ranges, adjusted = backend._normalize_mrs_ranges(
        [{'starting_byte': 123, 'length': 5000}]
    )
    assert adjusted is True
    assert ranges == [{'starting_byte': 0, 'length': 8192}]


def test_mrs_range_strict_alignment_rejects_unaligned_ranges(tmp_path):
    backend = CPCSNVMeBackend(
        base_path=str(tmp_path),
        cpcs_config={
            'mode': 'noop',
            'client': 'mock',
            'mrs_align_mode': 'strict',
            'mrs_align_bytes': 4096,
        },
    )
    with pytest.raises(ValueError, match='not aligned'):
        backend._normalize_mrs_ranges([{'starting_byte': 1, 'length': 4096}])


def test_arena_extents_are_block_aligned_and_non_overlapping(tmp_path):
    backend = CPCSNVMeBackend(
        base_path=str(tmp_path),
        cpcs_config={
            'mode': 'noop',
            'client': 'mock',
            'storage_mode': 'arena',
            'block_size_kb': 4,
        },
    )
    a = (np.arange(321, dtype=np.float32) / 10.0).astype(np.float16)
    b = (np.arange(777, dtype=np.float32) / 7.0).astype(np.float16)

    backend.write('k0', a)
    backend.write('k1', b)

    m0 = backend.metadata['k0']
    m1 = backend.metadata['k1']
    align = int(backend.block_size_bytes)

    assert m0['arena_offset'] % align == 0
    assert m1['arena_offset'] % align == 0
    assert m0['arena_end'] == m0['arena_offset'] + m0['arena_length']
    assert m1['arena_end'] == m1['arena_offset'] + m1['arena_length']
    assert m1['arena_offset'] >= m0['arena_end']
    assert m1['arena_offset'] == ((m0['arena_end'] + align - 1) // align) * align

    assert backend.index_path is not None
    index_payload = json.loads(backend.index_path.read_text(encoding='utf-8'))
    assert int(index_payload['k0']['arena_offset']) == int(m0['arena_offset'])
    assert int(index_payload['k1']['arena_offset']) == int(m1['arena_offset'])
