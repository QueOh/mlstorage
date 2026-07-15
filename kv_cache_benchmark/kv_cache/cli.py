"""
Command-line interface for KV Cache Benchmark.

Contains validate_args(), main(), and export_results_to_xlsx().
"""

import os
import sys
import json
import random
import logging
import argparse
from datetime import datetime
from dataclasses import is_dataclass, asdict
from typing import Dict

import numpy as np

from kv_cache._compat import (
    TORCH_AVAILABLE, CUPY_AVAILABLE, PANDAS_AVAILABLE, OPENPYXL_AVAILABLE,
)
from kv_cache.config import ConfigLoader, set_config, cfg
from kv_cache.models import (
    MODEL_CONFIGS, ModelConfig, GenerationMode, QoSLevel,
    QOS_PROFILES, get_qos_profiles,
)
from kv_cache.workload import validate_args
from kv_cache.benchmark import IntegratedBenchmark
from kv_cache.cpcs_spdk_inventory import load_spdk_inventory_defaults

if TORCH_AVAILABLE:
    import torch
if CUPY_AVAILABLE:
    import cupy as cp
if PANDAS_AVAILABLE:
    import pandas as pd

logger = logging.getLogger(__name__)


def export_results_to_xlsx(results: Dict, args, output_path: str):
    """
    Export benchmark results to an Excel file with run parameters embedded.
    Falls back to CSV if openpyxl is not available.
    """
    if not PANDAS_AVAILABLE:
        logger.warning("pandas not available, skipping XLSX export. Install with: pip install pandas")
        return

    summary = results.get('summary', {})
    if not summary:
        logger.warning("No summary data available for XLSX export")
        return

    def get_nested(d, keys, default=None):
        for key in keys:
            if isinstance(d, dict):
                d = d.get(key, default)
            else:
                return default
        return d

    def first_non_none(*values):
        for value in values:
            if value is not None:
                return value
        return None

    run_params = {
        'Timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'Model': args.model,
        'Num Users': args.num_users,
        'Duration (s)': args.duration,
        'GPU Memory per Card (GiB)': args.gpu_mem_gb,
        'Num GPUs': args.num_gpus,
        'Tensor Parallel': args.tensor_parallel,
        'Total GPU Memory (GiB)': args.gpu_mem_gb * args.num_gpus,
        'CPU Memory (GiB)': args.cpu_mem_gb,
        'Generation Mode': args.generation_mode,
        'Performance Profile': args.performance_profile,
        'Multi-turn': not args.disable_multi_turn,
        'Prefix Caching': not args.disable_prefix_caching,
        'RAG Enabled': args.enable_rag,
        'Autoscaling': args.enable_autoscaling,
        'Seed': args.seed,
        'Max Concurrent Allocs': args.max_concurrent_allocs,
        'Request Rate': args.request_rate,
        'Arrival': args.arrival,
        'SLO (ms)': args.slo_ms or 'N/A',
        'Max Requests': args.max_requests,
        'Dataset Path': args.dataset_path or 'N/A',
        'Cache Dir': args.cache_dir or 'temp',
        'Storage Capacity (GiB)': args.storage_capacity_gb,
        'Precondition': args.precondition,
        'Precondition Size (GiB)': args.precondition_size_gb,
        'Precondition Threads': args.precondition_threads if args.precondition_threads > 0 else (os.cpu_count() or 4),
        'Trace Speedup': args.trace_speedup,
        'Replay Cycles': args.replay_cycles,
        'NVMe Backend': getattr(args, 'nvme_backend', 'file'),
        'CPCS Mode': getattr(args, 'cpcs_mode', 'off'),
        'CPCS Client': getattr(args, 'cpcs_client', 'mock'),
        'CPCS Storage Mode': getattr(args, 'cpcs_storage_mode', 'file'),
    }

    metrics = {
        'Total Requests': summary.get('total_requests'),
        'Total Tokens': summary.get('total_tokens'),
        'Elapsed Time (s)': summary.get('elapsed_time'),
        'Avg Throughput (tok/s)': summary.get('avg_throughput_tokens_per_sec'),
        'Storage Throughput (tok/s)': summary.get('storage_throughput_tokens_per_sec'),
        'Requests/sec': summary.get('requests_per_second'),
        'Host CPU Avg (%)': first_non_none(summary.get('host_cpu_percent_avg'), get_nested(summary, ['system_metrics', 'host_cpu_percent_avg'])),
        'Fabric RX Bytes': first_non_none(summary.get('fabric_rx_bytes'), get_nested(summary, ['system_metrics', 'fabric_rx_bytes'])),
        'Fabric TX Bytes': first_non_none(summary.get('fabric_tx_bytes'), get_nested(summary, ['system_metrics', 'fabric_tx_bytes'])),

        'E2E Latency Mean (ms)': get_nested(summary, ['end_to_end_latency_ms', 'mean']),
        'E2E Latency P50 (ms)': get_nested(summary, ['end_to_end_latency_ms', 'p50']),
        'E2E Latency P95 (ms)': get_nested(summary, ['end_to_end_latency_ms', 'p95']),
        'E2E Latency P99 (ms)': get_nested(summary, ['end_to_end_latency_ms', 'p99']),
        'E2E Latency P99.9 (ms)': get_nested(summary, ['end_to_end_latency_ms', 'p999']),
        'E2E Latency P99.99 (ms)': get_nested(summary, ['end_to_end_latency_ms', 'p9999']),

        'Storage Latency Mean (ms)': get_nested(summary, ['storage_io_latency_ms', 'mean']),
        'Storage Latency P50 (ms)': get_nested(summary, ['storage_io_latency_ms', 'p50']),
        'Storage Latency P95 (ms)': get_nested(summary, ['storage_io_latency_ms', 'p95']),
        'Storage Latency P99 (ms)': get_nested(summary, ['storage_io_latency_ms', 'p99']),
        'Storage Latency P99.9 (ms)': get_nested(summary, ['storage_io_latency_ms', 'p999']),
        'Storage Latency P99.99 (ms)': get_nested(summary, ['storage_io_latency_ms', 'p9999']),

        'Gen Latency Mean (ms)': get_nested(summary, ['generation_latency_ms', 'mean']),
        'Gen Latency P50 (ms)': get_nested(summary, ['generation_latency_ms', 'p50']),
        'Gen Latency P95 (ms)': get_nested(summary, ['generation_latency_ms', 'p95']),
        'Gen Latency P99 (ms)': get_nested(summary, ['generation_latency_ms', 'p99']),

        'Storage Tier Read Total P50 (ms)': get_nested(summary, ['cache_stats', 'storage_read_p50_ms']),
        'Storage Tier Read Total P95 (ms)': get_nested(summary, ['cache_stats', 'storage_read_p95_ms']),
        'Storage Tier Read Total P99 (ms)': get_nested(summary, ['cache_stats', 'storage_read_p99_ms']),
        'Storage Tier Read Total P99.9 (ms)': get_nested(summary, ['cache_stats', 'storage_read_p999_ms']),
        'Storage Tier Read Total P99.99 (ms)': get_nested(summary, ['cache_stats', 'storage_read_p9999_ms']),
        'Storage Tier Write Total P50 (ms)': get_nested(summary, ['cache_stats', 'storage_write_p50_ms']),
        'Storage Tier Write Total P95 (ms)': get_nested(summary, ['cache_stats', 'storage_write_p95_ms']),
        'Storage Tier Write Total P99 (ms)': get_nested(summary, ['cache_stats', 'storage_write_p99_ms']),
        'Storage Tier Write Total P99.9 (ms)': get_nested(summary, ['cache_stats', 'storage_write_p999_ms']),
        'Storage Tier Write Total P99.99 (ms)': get_nested(summary, ['cache_stats', 'storage_write_p9999_ms']),

        'Storage Tier Read Device P50 (ms)': get_nested(summary, ['cache_stats', 'storage_read_device_p50_ms']),
        'Storage Tier Read Device P95 (ms)': get_nested(summary, ['cache_stats', 'storage_read_device_p95_ms']),
        'Storage Tier Read Device P99 (ms)': get_nested(summary, ['cache_stats', 'storage_read_device_p99_ms']),
        'Storage Tier Read Device P99.9 (ms)': get_nested(summary, ['cache_stats', 'storage_read_device_p999_ms']),
        'Storage Tier Read Device P99.99 (ms)': get_nested(summary, ['cache_stats', 'storage_read_device_p9999_ms']),
        'Storage Tier Write Device P50 (ms)': get_nested(summary, ['cache_stats', 'storage_write_device_p50_ms']),
        'Storage Tier Write Device P95 (ms)': get_nested(summary, ['cache_stats', 'storage_write_device_p95_ms']),
        'Storage Tier Write Device P99 (ms)': get_nested(summary, ['cache_stats', 'storage_write_device_p99_ms']),
        'Storage Tier Write Device P99.9 (ms)': get_nested(summary, ['cache_stats', 'storage_write_device_p999_ms']),
        'Storage Tier Write Device P99.99 (ms)': get_nested(summary, ['cache_stats', 'storage_write_device_p9999_ms']),

        'Storage Tier Read Host P50 (ms)': get_nested(summary, ['cache_stats', 'storage_read_host_p50_ms']),
        'Storage Tier Read Host P95 (ms)': get_nested(summary, ['cache_stats', 'storage_read_host_p95_ms']),
        'Storage Tier Read Host P99 (ms)': get_nested(summary, ['cache_stats', 'storage_read_host_p99_ms']),
        'Storage Tier Read Host P99.9 (ms)': get_nested(summary, ['cache_stats', 'storage_read_host_p999_ms']),
        'Storage Tier Read Host P99.99 (ms)': get_nested(summary, ['cache_stats', 'storage_read_host_p9999_ms']),
        'Storage Tier Write Host P50 (ms)': get_nested(summary, ['cache_stats', 'storage_write_host_p50_ms']),
        'Storage Tier Write Host P95 (ms)': get_nested(summary, ['cache_stats', 'storage_write_host_p95_ms']),
        'Storage Tier Write Host P99 (ms)': get_nested(summary, ['cache_stats', 'storage_write_host_p99_ms']),
        'Storage Tier Write Host P99.9 (ms)': get_nested(summary, ['cache_stats', 'storage_write_host_p999_ms']),
        'Storage Tier Write Host P99.99 (ms)': get_nested(summary, ['cache_stats', 'storage_write_host_p9999_ms']),

        'Cache Hit Rate': get_nested(summary, ['cache_stats', 'cache_hit_rate']),
        'Read/Write Ratio': get_nested(summary, ['cache_stats', 'read_write_ratio']),
        'Total Read (GiB)': get_nested(summary, ['cache_stats', 'total_read_gb']),
        'Total Write (GiB)': get_nested(summary, ['cache_stats', 'total_write_gb']),

        'Tier GPU KV Bytes Written (GiB)': get_nested(summary, ['cache_stats', 'tier_gpu_kv_bytes_written_gb']),
        'Tier CPU KV Bytes Written (GiB)': get_nested(summary, ['cache_stats', 'tier_cpu_kv_bytes_written_gb']),
        'Tier Storage KV Bytes Written (GiB)': get_nested(summary, ['cache_stats', 'tier_storage_kv_bytes_written_gb']),

        'Tier GPU KV Bytes Read (GiB)': get_nested(summary, ['cache_stats', 'tier_gpu_kv_bytes_read_gb']),
        'Tier CPU KV Bytes Read (GiB)': get_nested(summary, ['cache_stats', 'tier_cpu_kv_bytes_read_gb']),
        'Tier Storage KV Bytes Read (GiB)': get_nested(summary, ['cache_stats', 'tier_storage_kv_bytes_read_gb']),

        'Tier GPU Read Bandwidth (GiB/s)': get_nested(summary, ['cache_stats', 'tier_gpu_read_bandwidth_gbps']),
        'Tier GPU Write Bandwidth (GiB/s)': get_nested(summary, ['cache_stats', 'tier_gpu_write_bandwidth_gbps']),
        'Tier CPU Read Bandwidth (GiB/s)': get_nested(summary, ['cache_stats', 'tier_cpu_read_bandwidth_gbps']),
        'Tier CPU Write Bandwidth (GiB/s)': get_nested(summary, ['cache_stats', 'tier_cpu_write_bandwidth_gbps']),
        'Tier Storage Read Bandwidth (GiB/s)': get_nested(summary, ['cache_stats', 'tier_storage_read_bandwidth_gbps']),
        'Tier Storage Write Bandwidth (GiB/s)': get_nested(summary, ['cache_stats', 'tier_storage_write_bandwidth_gbps']),

        'GPU Entries': get_nested(summary, ['cache_stats', 'gpu_entries']),
        'CPU Entries': get_nested(summary, ['cache_stats', 'cpu_entries']),
        'Storage Entries': get_nested(summary, ['cache_stats', 'storage_entries']),

        'Multi-turn Hit Rate': get_nested(summary, ['multi_turn_stats', 'hit_rate']),
    }

    combined_row = {**run_params, **metrics}

    df = pd.DataFrame([combined_row])

    use_excel = OPENPYXL_AVAILABLE and output_path.endswith('.xlsx')

    try:
        if use_excel:
            with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='Summary', index=False)

                params_df = pd.DataFrame(list(run_params.items()), columns=['Parameter', 'Value'])
                params_df.to_excel(writer, sheet_name='Run Parameters', index=False)

                metrics_df = pd.DataFrame(list(metrics.items()), columns=['Metric', 'Value'])
                metrics_df.to_excel(writer, sheet_name='Performance Metrics', index=False)

                qos_metrics = summary.get('qos_metrics', {})
                if qos_metrics:
                    is_throughput = args.performance_profile == 'throughput'
                    qos_rows = []
                    for level, data in qos_metrics.items():
                        if isinstance(data, dict) and not data.get('no_data'):
                            qos_rows.append({
                                'QoS Level': level,
                                'Total Requests': data.get('total_requests'),
                                'Latency P95 (ms)': get_nested(data, ['latency_ms', 'p95']),
                                'Latency P99 (ms)': get_nested(data, ['latency_ms', 'p99']),
                                'SLA Met': 'N/A (throughput mode)' if is_throughput else get_nested(data, ['sla', 'met']),
                                'SLA Compliance': 'N/A (throughput mode)' if is_throughput else get_nested(data, ['sla', 'compliance']),
                            })
                    if qos_rows:
                        qos_df = pd.DataFrame(qos_rows)
                        qos_df.to_excel(writer, sheet_name='QoS Metrics', index=False)

                # Device tracing sheet (when --enable-latency-tracing is used)
                trace_data = results.get('device_latency_tracing', {})
                if trace_data:
                    trace_rows = []
                    display_order = [
                        ('d2c_read_us', 'D2C Read (us)', 'Device hardware time'),
                        ('d2c_write_us', 'D2C Write (us)', 'Device hardware time'),
                        ('q2d_read_us', 'Q2D Read (us)', 'I/O scheduler queue'),
                        ('q2d_write_us', 'Q2D Write (us)', 'I/O scheduler queue'),
                        ('vfs_read_us', 'VFS Read (us)', 'Application-visible'),
                        ('vfs_write_us', 'VFS Write (us)', 'Application-visible'),
                        ('fsync_us', 'fsync (us)', 'Device flush'),
                        ('write_to_fsync_us', 'Write-to-fsync (us)', 'CPU serialization gap'),
                        ('fadvise_to_read_us', 'fadvise-to-read (us)', 'Cache drop overhead'),
                        ('bssplit_read_kb', 'Block Size Read (KiB)', 'I/O size distribution'),
                        ('bssplit_write_kb', 'Block Size Write (KiB)', 'I/O size distribution'),
                        ('qd_read', 'Queue Depth Read', 'Instantaneous QD at dispatch'),
                        ('qd_write', 'Queue Depth Write', 'Instantaneous QD at dispatch'),
                        ('lba_read_gb', 'LBA Heatmap Read (GiB)', 'Spatial I/O distribution'),
                        ('lba_write_gb', 'LBA Heatmap Write (GiB)', 'Spatial I/O distribution'),
                    ]

                    def hist_pct(buckets, pct):
                        total = sum(b['count'] for b in buckets)
                        if total == 0:
                            return 0
                        target = total * pct / 100.0
                        cum = 0
                        for b in buckets:
                            cum += b['count']
                            if cum >= target:
                                return b['range_us'][0]
                        return buckets[-1]['range_us'][0]

                    for key, label, description in display_order:
                        if key not in trace_data or not trace_data[key].get('buckets'):
                            continue
                        buckets = trace_data[key]['buckets']
                        total_count = sum(b['count'] for b in buckets)
                        if total_count == 0:
                            continue
                        trace_rows.append({
                            'Metric': label,
                            'Description': description,
                            'Samples': total_count,
                            'P50': hist_pct(buckets, 50),
                            'P95': hist_pct(buckets, 95),
                            'P99': hist_pct(buckets, 99),
                            'Min Bucket': buckets[0]['range_us'][0],
                            'Max Bucket': buckets[-1]['range_us'][1],
                        })

                    if trace_rows:
                        trace_df = pd.DataFrame(trace_rows)
                        trace_df.to_excel(writer, sheet_name='Device Tracing', index=False)

                        # Raw histograms sheet
                        raw_rows = []
                        for key, label, _ in display_order:
                            if key not in trace_data or not trace_data[key].get('buckets'):
                                continue
                            for b in trace_data[key]['buckets']:
                                raw_rows.append({
                                    'Histogram': label,
                                    'Bucket Low': b['range_us'][0],
                                    'Bucket High': b['range_us'][1],
                                    'Count': b['count'],
                                })
                        if raw_rows:
                            raw_df = pd.DataFrame(raw_rows)
                            raw_df.to_excel(writer, sheet_name='Trace Histograms', index=False)

            logger.info(f"XLSX results saved to {output_path}")
        else:
            csv_path = output_path.replace('.xlsx', '.csv') if output_path.endswith('.xlsx') else output_path
            if not csv_path.endswith('.csv'):
                csv_path += '.csv'
            df.to_csv(csv_path, index=False)
            logger.info(f"CSV results saved to {csv_path} (openpyxl not available for XLSX)")

    except Exception as e:
        logger.error(f"Error saving XLSX/CSV: {e}")
        try:
            csv_path = output_path.replace('.xlsx', '.csv')
            df.to_csv(csv_path, index=False)
            logger.info(f"Fallback CSV saved to {csv_path}")
        except Exception as e2:
            logger.error(f"Failed to save results: {e2}")


def main():
    """Main entry point for running the benchmark from the command line."""
    parser = argparse.ArgumentParser(description="Integrated Multi-User KV Cache Benchmark")
    parser.add_argument('--log-level', type=str, default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        help='Set the logging level (default: INFO)')
    parser.add_argument('--model', type=str, default='llama3.1-8b',
                        help='The model configuration to use. Models are loaded from config.yaml.')
    parser.add_argument('--num-users', type=int, default=100,
                        help='The number of concurrent users to simulate.')
    parser.add_argument('--duration', type=int, default=60,
                        help='The duration of the benchmark in seconds.')
    parser.add_argument('--gpu-mem-gb', type=float, default=16,
                        help='Per-GPU VRAM to allocate for the KV cache tier in GiB. '
                             'When --num-gpus > 1 the effective GPU pool = num_gpus × gpu-mem-gb.')
    parser.add_argument('--num-gpus', type=int, default=1,
                        help='Number of GPUs in the tensor-parallel group. '
                             'Sets total GPU tier = num_gpus × gpu-mem-gb. '
                             'Example: --num-gpus 8 --gpu-mem-gb 141 models 8×H200.')
    parser.add_argument('--tensor-parallel', type=int, default=1,
                        help='Tensor-parallel degree (TP). '
                             'Each GPU rank stores 1/TP of each KV cache entry, '
                             'so per-rank I/O object sizes are divided by TP. '
                             'Must be >= 1 and <= --num-gpus. '
                             'Example: --tensor-parallel 8 models TP=8 for Llama 70B on 8×H200.')
    parser.add_argument('--cpu-mem-gb', type=float, default=32,
                        help='Total CPU DRAM to allocate for the KV cache spill tier in GiB.')
    parser.add_argument('--cache-dir', type=str, default=None,
                        help='The directory to use for the NVMe cache tier.')
    parser.add_argument('--nvme-backend', type=str, default='file', choices=['file', 'cpcs'],
                        help='NVMe backend implementation. "file" preserves original behavior.')
    parser.add_argument('--cpcs-mode', type=str, default='off',
                        choices=['off', 'noop', 'lossless_compress', 'int8_quantize', 'layout', 'block_select', 'prefix_index'],
                        help='CPCS operation mode (used when --nvme-backend cpcs).')
    parser.add_argument('--cpcs-client', type=str, default='mock', choices=['mock', 'spdk_passthru'],
                        help='CPCS client transport.')
    parser.add_argument('--cpcs-storage-mode', type=str, default='file', choices=['file', 'arena'],
                        help='CPCS storage layout mode for persisted payloads.')
    parser.add_argument('--spdk-inventory', type=str, default='',
                        help='Optional SPDK inventory YAML for passthru defaults.')
    parser.add_argument('--spdk-rpc-script', type=str, default='scripts/rpc.py',
                        help='SPDK rpc.py path (reserved for runtime/bootstrap integration).')
    parser.add_argument('--spdk-rpc-python', type=str, default='python3',
                        help='Python interpreter used to execute rpc.py bootstrap calls.')
    parser.add_argument('--spdk-rpc-socket', type=str, default='',
                        help='Optional SPDK RPC socket path for bootstrap calls.')
    parser.add_argument('--bootstrap-subsystem-nqn', type=str, default='',
                        help='Optional subsystem NQN override for CPCS bootstrap RPC calls.')
    parser.add_argument('--cpcs-bootstrap-check', action='store_true',
                        help='When enabled, verify SPDK rpc.py method availability during CPCS backend init.')
    parser.add_argument('--cpcs-bootstrap-install-builtins', action='store_true',
                        help='Install built-in CPCS programs through rpc.py during backend init.')
    parser.add_argument('--cpcs-bootstrap-list-programs', action='store_true',
                        help='Collect cpcs_program_list output through rpc.py during backend init.')
    parser.add_argument('--cpcs-bootstrap-list-mrs', action='store_true',
                        help='Collect cpcs_mrs_list output through rpc.py during backend init.')
    parser.add_argument('--cpcs-required-rpc-methods', type=str, default='',
                        help='Comma-separated list of rpc.py methods required when bootstrap check is enabled.')
    parser.add_argument('--spdk-nvme-passthru', type=str, default='',
                        help='Path to spdk_nvme_passthru binary.')
    parser.add_argument('--trtype', type=str, default='TCP', help='NVMe-oF transport type.')
    parser.add_argument('--traddr', type=str, default='', help='NVMe-oF transport address.')
    parser.add_argument('--trsvcid', type=str, default='', help='NVMe-oF transport service ID.')
    parser.add_argument('--subnqn', type=str, default='', help='NVMe-oF subsystem NQN.')
    parser.add_argument('--hostnqn', type=str, default='', help='NVMe host NQN.')
    parser.add_argument('--src-addr', type=str, default='', help='Optional source IP for NVMe-oF connection.')
    parser.add_argument('--src-svcid', type=str, default='', help='Optional source service id for NVMe-oF connection.')
    parser.add_argument('--passthru-lcores', type=str, default='1', help='lcores string for spdk_nvme_passthru.')
    parser.add_argument('--dataset-nsid', type=int, default=0, help='Dataset namespace NSID.')
    parser.add_argument('--slm-nsid', type=int, default=0, help='SLM namespace NSID.')
    parser.add_argument('--cpcs-nsid', type=int, default=0, help='CPCS namespace NSID.')
    parser.add_argument('--cpcs-program-pind', type=int, default=0, help='Program index for CPCS execute.')
    parser.add_argument('--cpcs-program-pind-pack-store', type=int, default=-1,
                        help='Override PIND for pack_store writes (-1 inherits --cpcs-program-pind).')
    parser.add_argument('--cpcs-program-pind-unpack-load', type=int, default=-1,
                        help='Override PIND for unpack_load reads (-1 inherits --cpcs-program-pind).')
    parser.add_argument('--cpcs-program-pind-layout-repack', type=int, default=-1,
                        help='Override PIND for layout_repack mode (-1 inherits --cpcs-program-pind).')
    parser.add_argument('--cpcs-program-pind-block-select', type=int, default=-1,
                        help='Override PIND for block_select mode (-1 inherits --cpcs-program-pind).')
    parser.add_argument('--cpcs-program-pind-prefix-lookup', type=int, default=-1,
                        help='Override PIND for prefix_lookup mode (-1 inherits --cpcs-program-pind).')
    parser.add_argument('--cpcs-program-pind-batch-read', type=int, default=-1,
                        help='Override PIND for batch_read descriptors (-1 inherits --cpcs-program-pind).')
    parser.add_argument('--cpcs-rsid', type=int, default=1, help='Memory range set ID for CPCS execute.')
    parser.add_argument('--cpcs-rsid-pack-store', type=int, default=-1,
                        help='Override RSID for pack_store writes (-1 inherits --cpcs-rsid).')
    parser.add_argument('--cpcs-rsid-unpack-load', type=int, default=-1,
                        help='Override RSID for unpack_load reads (-1 inherits --cpcs-rsid).')
    parser.add_argument('--cpcs-rsid-layout-repack', type=int, default=-1,
                        help='Override RSID for layout_repack mode (-1 inherits --cpcs-rsid).')
    parser.add_argument('--cpcs-rsid-block-select', type=int, default=-1,
                        help='Override RSID for block_select mode (-1 inherits --cpcs-rsid).')
    parser.add_argument('--cpcs-rsid-prefix-lookup', type=int, default=-1,
                        help='Override RSID for prefix_lookup mode (-1 inherits --cpcs-rsid).')
    parser.add_argument('--cpcs-rsid-batch-read', type=int, default=-1,
                        help='Override RSID for batch_read descriptors (-1 inherits --cpcs-rsid).')
    parser.add_argument('--cpcs-auto-create-mrs', action='store_true',
                        help='Auto-create a CPCS MRS during backend initialization.')
    parser.add_argument('--cpcs-mrs-ranges', type=str, default='',
                        help='MRS ranges spec: "offset:length,..." or JSON list with offset/length fields.')
    parser.add_argument('--cpcs-mrs-default-length-bytes', type=int, default=65536,
                        help='Default MRS length used when auto-create is enabled and ranges are omitted.')
    parser.add_argument('--cpcs-mrs-align-bytes', type=int, default=0,
                        help='Alignment size for MRS ranges (0 uses direct-probe LBA bytes).')
    parser.add_argument('--cpcs-mrs-align-mode', type=str, default='round', choices=['none', 'strict', 'round'],
                        help='MRS alignment policy: none/strict/round.')
    parser.add_argument('--cpcs-load-program-path', type=str, default='',
                        help='Optional CPCS program binary path to load via admin opcode 0x85.')
    parser.add_argument('--cpcs-load-program-pind', type=int, default=-1,
                        help='Program index used for --cpcs-load-program-path (-1 uses --cpcs-program-pind).')
    parser.add_argument('--cpcs-load-program-set-default-pind', action='store_true',
                        help='Use --cpcs-load-program-pind as the default execute PIND for CPCS ops.')
    parser.add_argument('--cpcs-load-program-chunk-bytes', type=int, default=0,
                        help='Chunk size for admin opcode 0x85 program load (0 = single transfer).')
    parser.add_argument('--cpcs-load-program-ptype', type=lambda x: int(x, 0), default=0xC0,
                        help='Program type (PTYPE) for program load; accepts decimal or hex (e.g. 0xC0).')
    parser.add_argument('--cpcs-load-program-pit', type=lambda x: int(x, 0), default=0x01,
                        help='Program implementation type (PIT) for program load; accepts decimal or hex.')
    parser.add_argument('--cpcs-load-program-puid', type=lambda x: int(x, 0), default=0xEBF00001,
                        help='Program UID (PUID) for program load; accepts decimal or hex.')
    parser.add_argument('--cpcs-activate-loaded-program', action='store_true',
                        help='Activate the loaded CPCS program via admin opcode 0x88 after loading.')
    parser.add_argument('--direct-probe-offset', type=int, default=0, help='Probe read offset in bytes.')
    parser.add_argument('--direct-probe-length', type=int, default=4096, help='Probe read length in bytes.')
    parser.add_argument('--direct-probe-lba-bytes', type=int, default=0, help='LBA size used for probe alignment.')
    parser.add_argument('--cpcs-slm-rw-lba-bytes', type=int, default=0,
                        help='LBA size used for SLM raw read/write in lba mode (0 uses --direct-probe-lba-bytes).')
    parser.add_argument('--cpcs-slm-read-address-mode', type=str, default='byte', choices=['byte', 'lba'],
                        help='Addressing mode for SLM reads (byte follows CPCS vector scripts).')
    parser.add_argument('--cpcs-slm-write-address-mode', type=str, default='lba', choices=['byte', 'lba'],
                        help='Addressing mode for SLM writes in arena mode.')
    parser.add_argument('--cpcs-arena-path', type=str, default='', help='Arena path for CPCS arena mode.')
    parser.add_argument('--cpcs-index-path', type=str, default='', help='Index path for CPCS metadata.')
    parser.add_argument('--cpcs-metrics-output', type=str, default='', help='Optional path for CPCS metrics JSON.')
    parser.add_argument('--cpcs-verify-every-n', type=int, default=0, help='Periodic verification interval (0=off).')
    parser.add_argument('--cpcs-lossy-tolerance', type=float, default=0.0, help='Tolerance for lossy CPCS modes.')
    parser.add_argument('--cpcs-block-size-kb', type=int, default=1024, help='Logical CPCS block size in KiB.')
    parser.add_argument('--cpcs-batch-size', type=int, default=1, help='Batch size hint for CPCS layout mode.')
    parser.add_argument('--cpcs-fallback-on-error', action='store_true',
                        help='Fallback to raw storage behavior if CPCS command fails.')
    parser.add_argument('--generation-mode', type=str, default='realistic', choices=[g.value for g in GenerationMode],
                        help='The token generation speed simulation mode.')
    parser.add_argument('--performance-profile', type=str, default='latency', choices=['latency', 'throughput'],
                        help='The performance profile to use for pass/fail criteria.')
    parser.add_argument('--disable-multi-turn', action='store_true',
                        help='Disable multi-turn conversation caching.')
    parser.add_argument('--disable-prefix-caching', action='store_true',
                        help='Disable prefix caching.')
    parser.add_argument('--enable-rag', action='store_true',
                        help='Enable the RAG workload simulation.')
    parser.add_argument('--rag-num-docs', type=int, default=10, help='Number of RAG documents to ingest')
    parser.add_argument('--enable-autoscaling', action='store_true',
                        help='Enable workload autoscaling.')
    parser.add_argument('--autoscaler-mode', type=str, default='qos', choices=['qos', 'capacity'],
                        help='The autoscaling strategy.')
    parser.add_argument('--target-saturation', type=float, default=0.8, help='Target storage saturation (0.0-1.0)')
    parser.add_argument('--use-burst-trace', action='store_true',
                        help='Use BurstGPT trace for workload generation.')
    parser.add_argument('--burst-trace-path', type=str, default='BurstGPT/data/BurstGPT_1.csv',
                        help='Path to the BurstGPT trace file.')
    parser.add_argument('--validation-trace', type=str, default=None,
                        help='Path to a real-world trace file for validation.')
    parser.add_argument('--dataset-path', type=str, default=None,
                        help='Path to ShareGPT dataset JSON file.')
    parser.add_argument('--max-conversations', type=int, default=500,
                        help='Maximum number of conversations from ShareGPT dataset.')
    parser.add_argument('--output', type=str, default=f"benchmark_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", help='Output file for results')
    parser.add_argument('--seed', type=int, default=None,
                        help='Seed for random number generators.')
    parser.add_argument('--max-concurrent-allocs', type=int, default=0,
                        help='Limit concurrent allocations. 0 = unlimited.')
    parser.add_argument('--request-rate', type=float, default=0,
                        help='Target request arrival rate (requests/sec). 0 = unlimited.')
    parser.add_argument('--arrival', type=str, default='fixed',
                        choices=['fixed', 'poisson', 'gamma'],
                        help='Arrival process for --request-rate pacing: fixed cadence '
                             '(legacy), open-loop Poisson (exponential inter-arrivals), '
                             'or gamma (burstier-than-Poisson at the same mean rate; '
                             'see --arrival-cv).')
    parser.add_argument('--arrival-cv', type=float, default=2.0,
                        help='Coefficient of variation for --arrival gamma '
                             '(CV=1 equals Poisson; higher = burstier).')
    parser.add_argument('--slo-ms', type=float, default=0,
                        help='End-to-end latency SLO in ms; summary gains slo_attainment '
                             '(fraction of requests within the SLO). 0 = no SLO.')
    parser.add_argument('--latency-dump', type=str, default=None,
                        help='Optional JSONL path for per-request latency records '
                             '(request_id, qos, phase, e2e_ms, storage_s, tokens).')
    parser.add_argument('--max-requests', type=int, default=0,
                        help='Stop after completing N requests (0 = use duration instead).')
    parser.add_argument('--xlsx-output', type=str, default=None,
                        help='Optional: Output Excel file path.')
    parser.add_argument('--config', type=str, default=None,
                        help='Path to YAML configuration file.')
    parser.add_argument('--storage-capacity-gb', type=float, default=0,
                        help='NVMe/storage tier capacity in GiB. 0 = auto-detect.')
    parser.add_argument('--precondition', action='store_true',
                        help='Enable SSD preconditioning phase before benchmark.')
    parser.add_argument('--precondition-size-gb', type=float, default=0,
                        help='Preconditioning data volume in GiB. 0 = 2x NVMe capacity.')
    parser.add_argument('--precondition-threads', type=int, default=0,
                        help='Number of threads for preconditioning writes. 0 = os.cpu_count().')
    parser.add_argument('--trace-speedup', type=float, default=1.0,
                        help='Speedup factor for BurstGPT trace replay timestamps.')
    parser.add_argument('--replay-cycles', type=int, default=0,
                        help='Number of complete passes through the trace dataset. 0 = infinite.')
    parser.add_argument('--prefill-only', action='store_true',
                        help='Simulate disaggregated prefill node (write-heavy, no decode reads).')
    parser.add_argument('--decode-only', action='store_true',
                        help='Simulate disaggregated decode node (read-heavy, assumes KV cache exists).')
    parser.add_argument('--io-trace-log', type=str, default=None,
                        help=(
                            'Path for the I/O trace CSV output file. '
                            'When set, activates trace mode: no real GPU/CPU/NVMe I/O is performed. '
                            'Instead every KV cache operation is logged as a row: '
                            'Timestamp,Operation,Object_Size_Bytes,Tier (Tier-0=GPU, Tier-1=CPU, Tier-2=NVMe). '
                            'The resulting trace can be replayed by an external storage benchmark tool.'
                        ))
    parser.add_argument('--enable-latency-tracing', action='store_true',
                        help='Enable bpftrace device latency tracing (requires sudo, bpftrace).')

    args = parser.parse_args()

    # Validate mutually exclusive flags
    if args.prefill_only and args.decode_only:
        parser.error("--prefill-only and --decode-only are mutually exclusive")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    args = validate_args(args)

    if args.io_trace_log:
        logger.info(f"Trace mode active: I/O operations will be logged to {args.io_trace_log} (no real hardware I/O)")

    # Optional passthru defaults from SPDK inventory YAML.
    if args.nvme_backend == 'cpcs' and args.spdk_inventory:
        inventory_defaults = load_spdk_inventory_defaults(args.spdk_inventory)

        def _apply_if_empty(name, empty_values):
            cur = getattr(args, name)
            if cur in empty_values and name in inventory_defaults:
                setattr(args, name, inventory_defaults[name])

        _apply_if_empty('trtype', {'', None})
        _apply_if_empty('traddr', {'', None})
        _apply_if_empty('trsvcid', {'', None})
        _apply_if_empty('subnqn', {'', None})
        _apply_if_empty('hostnqn', {'', None})
        _apply_if_empty('bootstrap_subsystem_nqn', {'', None})
        _apply_if_empty('src_addr', {'', None})
        _apply_if_empty('src_svcid', {'', None})
        _apply_if_empty('passthru_lcores', {'', None})
        _apply_if_empty('cpcs_bootstrap_install_builtins', {False, None})
        _apply_if_empty('cpcs_bootstrap_list_programs', {False, None})
        _apply_if_empty('cpcs_bootstrap_list_mrs', {False, None})
        _apply_if_empty('dataset_nsid', {0, None})
        _apply_if_empty('slm_nsid', {0, None})
        _apply_if_empty('cpcs_nsid', {0, None})
        _apply_if_empty('cpcs_program_pind', {0, None})
        _apply_if_empty('cpcs_rsid', {1, 0, None})
        _apply_if_empty('cpcs_program_pind_pack_store', {-1, None})
        _apply_if_empty('cpcs_program_pind_unpack_load', {-1, None})
        _apply_if_empty('cpcs_program_pind_layout_repack', {-1, None})
        _apply_if_empty('cpcs_program_pind_block_select', {-1, None})
        _apply_if_empty('cpcs_program_pind_prefix_lookup', {-1, None})
        _apply_if_empty('cpcs_program_pind_batch_read', {-1, None})
        _apply_if_empty('cpcs_rsid_pack_store', {-1, None})
        _apply_if_empty('cpcs_rsid_unpack_load', {-1, None})
        _apply_if_empty('cpcs_rsid_layout_repack', {-1, None})
        _apply_if_empty('cpcs_rsid_block_select', {-1, None})
        _apply_if_empty('cpcs_rsid_prefix_lookup', {-1, None})
        _apply_if_empty('cpcs_rsid_batch_read', {-1, None})
        _apply_if_empty('cpcs_auto_create_mrs', {False, None})
        _apply_if_empty('cpcs_mrs_ranges', {'', None})
        _apply_if_empty('cpcs_mrs_default_length_bytes', {65536, 0, None})
        _apply_if_empty('cpcs_mrs_align_bytes', {0, None})
        _apply_if_empty('cpcs_mrs_align_mode', {'round', '', None})
        _apply_if_empty('cpcs_load_program_path', {'', None})
        _apply_if_empty('cpcs_load_program_pind', {-1, None})
        _apply_if_empty('cpcs_load_program_set_default_pind', {False, None})
        _apply_if_empty('cpcs_load_program_chunk_bytes', {0, None})
        _apply_if_empty('cpcs_load_program_ptype', {0xC0, None})
        _apply_if_empty('cpcs_load_program_pit', {0x01, None})
        _apply_if_empty('cpcs_load_program_puid', {0xEBF00001, None})
        _apply_if_empty('cpcs_activate_loaded_program', {False, None})
        _apply_if_empty('direct_probe_offset', {0, None})
        _apply_if_empty('direct_probe_length', {0, None})
        _apply_if_empty('direct_probe_lba_bytes', {0, None})
        _apply_if_empty('cpcs_slm_rw_lba_bytes', {0, None})
        _apply_if_empty('cpcs_slm_read_address_mode', {'byte', '', None})
        _apply_if_empty('cpcs_slm_write_address_mode', {'lba', '', None})

    if args.config:
        config = ConfigLoader(args.config)
        set_config(config)
        logger.info(f"Loaded configuration from {args.config}")

        # Refresh MODEL_CONFIGS and QOS_PROFILES with config values
        import kv_cache.models as _models
        _models.MODEL_CONFIGS = _models.get_model_configs()
        _models.QOS_PROFILES = get_qos_profiles()

    # Re-import MODEL_CONFIGS after potential config reload
    from kv_cache.models import MODEL_CONFIGS as CURRENT_MODEL_CONFIGS

    # Validate model choice
    if args.model not in CURRENT_MODEL_CONFIGS:
        available = ', '.join(sorted(CURRENT_MODEL_CONFIGS.keys()))
        logger.error(f"Unknown model '{args.model}'. Available models: {available}")
        sys.exit(1)

    if args.seed is not None:
        logger.info(f"Using random seed: {args.seed}")
        random.seed(args.seed)
        np.random.seed(args.seed)
        if TORCH_AVAILABLE:
            torch.manual_seed(args.seed)
        if CUPY_AVAILABLE:
            cp.random.seed(args.seed)

    model_config = CURRENT_MODEL_CONFIGS[args.model]
    gen_mode = GenerationMode(args.generation_mode)
    cpcs_config = {
        'mode': args.cpcs_mode,
        'client': args.cpcs_client,
        'storage_mode': args.cpcs_storage_mode,
        'spdk_rpc_script': args.spdk_rpc_script,
        'spdk_rpc_python': args.spdk_rpc_python,
        'spdk_rpc_socket': args.spdk_rpc_socket,
        'bootstrap_subsystem_nqn': args.bootstrap_subsystem_nqn,
        'bootstrap_check': args.cpcs_bootstrap_check,
        'bootstrap_install_builtins': args.cpcs_bootstrap_install_builtins,
        'bootstrap_list_programs': args.cpcs_bootstrap_list_programs,
        'bootstrap_list_mrs': args.cpcs_bootstrap_list_mrs,
        'required_rpc_methods': args.cpcs_required_rpc_methods,
        'spdk_nvme_passthru': args.spdk_nvme_passthru,
        'trtype': args.trtype,
        'traddr': args.traddr,
        'trsvcid': args.trsvcid,
        'subnqn': args.subnqn,
        'hostnqn': args.hostnqn,
        'src_addr': args.src_addr,
        'src_svcid': args.src_svcid,
        'passthru_lcores': args.passthru_lcores,
        'dataset_nsid': args.dataset_nsid,
        'slm_nsid': args.slm_nsid,
        'cpcs_nsid': args.cpcs_nsid,
        'cpcs_program_pind': args.cpcs_program_pind,
        'cpcs_program_pind_pack_store': args.cpcs_program_pind_pack_store,
        'cpcs_program_pind_unpack_load': args.cpcs_program_pind_unpack_load,
        'cpcs_program_pind_layout_repack': args.cpcs_program_pind_layout_repack,
        'cpcs_program_pind_block_select': args.cpcs_program_pind_block_select,
        'cpcs_program_pind_prefix_lookup': args.cpcs_program_pind_prefix_lookup,
        'cpcs_program_pind_batch_read': args.cpcs_program_pind_batch_read,
        'cpcs_rsid': args.cpcs_rsid,
        'cpcs_rsid_pack_store': args.cpcs_rsid_pack_store,
        'cpcs_rsid_unpack_load': args.cpcs_rsid_unpack_load,
        'cpcs_rsid_layout_repack': args.cpcs_rsid_layout_repack,
        'cpcs_rsid_block_select': args.cpcs_rsid_block_select,
        'cpcs_rsid_prefix_lookup': args.cpcs_rsid_prefix_lookup,
        'cpcs_rsid_batch_read': args.cpcs_rsid_batch_read,
        'auto_create_mrs': args.cpcs_auto_create_mrs,
        'mrs_ranges': args.cpcs_mrs_ranges,
        'mrs_default_length_bytes': args.cpcs_mrs_default_length_bytes,
        'mrs_align_bytes': args.cpcs_mrs_align_bytes,
        'mrs_align_mode': args.cpcs_mrs_align_mode,
        'load_program_path': args.cpcs_load_program_path,
        'load_program_pind': args.cpcs_load_program_pind,
        'load_program_set_default_pind': args.cpcs_load_program_set_default_pind,
        'load_program_chunk_bytes': args.cpcs_load_program_chunk_bytes,
        'load_program_ptype': args.cpcs_load_program_ptype,
        'load_program_pit': args.cpcs_load_program_pit,
        'load_program_puid': args.cpcs_load_program_puid,
        'activate_loaded_program': args.cpcs_activate_loaded_program,
        'direct_probe_offset': args.direct_probe_offset,
        'direct_probe_length': args.direct_probe_length,
        'direct_probe_lba_bytes': args.direct_probe_lba_bytes if args.direct_probe_lba_bytes > 0 else 4096,
        'slm_rw_lba_bytes': args.cpcs_slm_rw_lba_bytes if args.cpcs_slm_rw_lba_bytes > 0 else (
            args.direct_probe_lba_bytes if args.direct_probe_lba_bytes > 0 else 4096
        ),
        'slm_read_address_mode': args.cpcs_slm_read_address_mode,
        'slm_write_address_mode': args.cpcs_slm_write_address_mode,
        'arena_path': args.cpcs_arena_path,
        'index_path': args.cpcs_index_path,
        'metrics_output': args.cpcs_metrics_output,
        'verify_every_n': args.cpcs_verify_every_n,
        'lossy_tolerance': args.cpcs_lossy_tolerance,
        'block_size_kb': args.cpcs_block_size_kb,
        'batch_size': args.cpcs_batch_size,
        'fallback_on_error': args.cpcs_fallback_on_error,
    }

    benchmark = IntegratedBenchmark(
        model_config=model_config,
        num_users=args.num_users,
        gpu_memory_gb=args.gpu_mem_gb,
        num_gpus=args.num_gpus,
        tensor_parallel=args.tensor_parallel,
        cpu_memory_gb=args.cpu_mem_gb,
        duration_seconds=args.duration,
        cache_dir=args.cache_dir,
        nvme_backend=args.nvme_backend,
        cpcs_config=cpcs_config,
        enable_autoscaling=args.enable_autoscaling,
        autoscaler_mode=args.autoscaler_mode,
        target_saturation=args.target_saturation,
        enable_multi_turn=not args.disable_multi_turn,
        enable_prefix_caching=not args.disable_prefix_caching,
        enable_rag=args.enable_rag,
        rag_num_docs=args.rag_num_docs,
        validation_trace=args.validation_trace,
        generation_mode=gen_mode,
        performance_profile=args.performance_profile,
        use_burst_trace=args.use_burst_trace,
        burst_trace_path=args.burst_trace_path,
        dataset_path=args.dataset_path,
        max_conversations=args.max_conversations,
        seed=args.seed,
        max_concurrent_allocs=args.max_concurrent_allocs,
        request_rate=args.request_rate,
        arrival=args.arrival,
        arrival_cv=args.arrival_cv,
        slo_ms=args.slo_ms,
        latency_dump=args.latency_dump,
        max_requests=args.max_requests,
        storage_capacity_gb=args.storage_capacity_gb,
        precondition=args.precondition,
        precondition_size_gb=args.precondition_size_gb,
        precondition_threads=args.precondition_threads,
        trace_speedup=args.trace_speedup,
        replay_cycles=args.replay_cycles,
        prefill_only=args.prefill_only,
        decode_only=args.decode_only,
        io_trace_log=args.io_trace_log,
        enable_latency_tracing=args.enable_latency_tracing
    )

    results = benchmark.run()
    results.setdefault('metadata', {})
    results['metadata']['nvme_backend'] = args.nvme_backend
    results['metadata']['cpcs'] = {
        'enabled': args.nvme_backend == 'cpcs',
        'mode': args.cpcs_mode,
        'client': args.cpcs_client,
        'storage_mode': args.cpcs_storage_mode,
        'spdk_inventory': args.spdk_inventory,
        'bootstrap': {
            'enabled': (
                args.cpcs_bootstrap_check
                or args.cpcs_bootstrap_install_builtins
                or args.cpcs_bootstrap_list_programs
                or args.cpcs_bootstrap_list_mrs
            ),
            'rpc_script': args.spdk_rpc_script,
            'rpc_python': args.spdk_rpc_python,
            'rpc_socket': args.spdk_rpc_socket,
            'subsystem_nqn': args.bootstrap_subsystem_nqn or args.subnqn,
            'install_builtins': args.cpcs_bootstrap_install_builtins,
            'list_programs': args.cpcs_bootstrap_list_programs,
            'list_mrs': args.cpcs_bootstrap_list_mrs,
            'required_methods': args.cpcs_required_rpc_methods,
        },
        'transport': {
            'trtype': args.trtype,
            'traddr': args.traddr,
            'trsvcid': args.trsvcid,
            'subnqn': args.subnqn,
            'hostnqn': args.hostnqn,
            'src_addr': args.src_addr,
            'src_svcid': args.src_svcid,
        },
        'namespaces': {
            'dataset_nsid': args.dataset_nsid,
            'slm_nsid': args.slm_nsid,
            'cpcs_nsid': args.cpcs_nsid,
        },
        'slm': {
            'rw_lba_bytes': args.cpcs_slm_rw_lba_bytes if args.cpcs_slm_rw_lba_bytes > 0 else (
                args.direct_probe_lba_bytes if args.direct_probe_lba_bytes > 0 else 4096
            ),
            'read_address_mode': args.cpcs_slm_read_address_mode,
            'write_address_mode': args.cpcs_slm_write_address_mode,
        },
        'program': {
            'default_pind': args.cpcs_program_pind,
            'pack_store_pind': args.cpcs_program_pind_pack_store,
            'unpack_load_pind': args.cpcs_program_pind_unpack_load,
            'layout_repack_pind': args.cpcs_program_pind_layout_repack,
            'block_select_pind': args.cpcs_program_pind_block_select,
            'prefix_lookup_pind': args.cpcs_program_pind_prefix_lookup,
            'batch_read_pind': args.cpcs_program_pind_batch_read,
            'rsid': args.cpcs_rsid,
            'pack_store_rsid': args.cpcs_rsid_pack_store,
            'unpack_load_rsid': args.cpcs_rsid_unpack_load,
            'layout_repack_rsid': args.cpcs_rsid_layout_repack,
            'block_select_rsid': args.cpcs_rsid_block_select,
            'prefix_lookup_rsid': args.cpcs_rsid_prefix_lookup,
            'batch_read_rsid': args.cpcs_rsid_batch_read,
            'load_program_path': args.cpcs_load_program_path,
            'load_program_pind': args.cpcs_load_program_pind,
            'load_program_set_default_pind': args.cpcs_load_program_set_default_pind,
            'load_program_chunk_bytes': args.cpcs_load_program_chunk_bytes,
            'load_program_ptype': args.cpcs_load_program_ptype,
            'load_program_pit': args.cpcs_load_program_pit,
            'load_program_puid': args.cpcs_load_program_puid,
            'activate_loaded_program': args.cpcs_activate_loaded_program,
        },
        'mrs': {
            'auto_create': args.cpcs_auto_create_mrs,
            'ranges': args.cpcs_mrs_ranges,
            'default_length_bytes': args.cpcs_mrs_default_length_bytes,
            'align_bytes': args.cpcs_mrs_align_bytes,
            'align_mode': args.cpcs_mrs_align_mode,
        },
        'layout': {
            'arena_path': args.cpcs_arena_path,
            'index_path': args.cpcs_index_path,
            'metrics_output': args.cpcs_metrics_output,
        },
    }

    def convert_numpy(obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, datetime):
            return obj.isoformat()
        if is_dataclass(obj):
            return asdict(obj)
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    with open(args.output, 'w') as f:
        json.dump(results, f, indent=4, default=convert_numpy)

    logger.info(f"Results saved to {args.output}")

    if args.xlsx_output:
        export_results_to_xlsx(results, args, args.xlsx_output)

    # Save fio workload file when latency tracing produced one
    fio_config = results.get('fio_workload')
    if fio_config:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        fio_filename = f"fio_kv_cache_workload_{timestamp}.ini"
        with open(fio_filename, 'w') as f:
            f.write(fio_config)
        logger.info(f"fio workload saved to {fio_filename}")


if __name__ == "__main__":
    main()
