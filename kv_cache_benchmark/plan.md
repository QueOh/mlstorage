# CPCS KV-Cache Offload Demo Plan

Last updated: 2026-05-29
Owner: Codex agent, on behalf of Kyuho Son
Repository target: `mlcommons/storage`, subdirectory `kv_cache_benchmark`

## 1. Goal

Implement a demo path in the MLPerf Storage KV Cache Benchmark that compares:

1. Baseline NVMe-oF JBOF target used as normal storage.
2. NVMe-oF JBOF target with CPCS-based computational offload for selected KV-cache operations.

The goal is not to create a closed, official MLPerf submission. The goal is a reproducible research/demo harness that shows whether storage-side computation improves one or more of these metrics:

- Host CPU utilization.
- NVMe-oF fabric bytes transferred.
- Media bytes written/read on the target.
- Storage I/O latency, especially P95/P99.
- Effective KV-cache throughput.
- Requests/sec or storage throughput tokens/sec reported by the benchmark.
- Target-side compute utilization and CPCS command latency.

## 2. Current benchmark facts to preserve

The current benchmark is suitable as a harness because it simulates LLM KV-cache offloading, has CPU-only mode, and uses a modular backend layer.

Important current behavior:

- The benchmark models a GPU -> CPU -> NVMe memory hierarchy, but it can run with `--gpu-mem-gb 0` and `--cpu-mem-gb 0` or `--cpu-mem-gb 4` for storage-only testing.
- The README says no GPU is required for CPU-only mode.
- The benchmark accepts any filesystem/block-device mount through `--cache-dir`.
- `kv_cache/backends.py` defines `StorageBackend` with `write`, `read`, `delete`, and `clear`.
- `NVMeBackend` currently stores each KV object as a `.npy` file and returns `IOTiming(total, device, host)`.
- `kv_cache/cache.py` creates the NVMe tier with `NVMeBackend(base_path=cache_dir)`.
- The default output already includes throughput, storage latency, end-to-end latency, cache hit rate, and related metrics.

Keep the default benchmark behavior unchanged unless CPCS flags are explicitly enabled.

## 3. Key design decision

Do not modify the workload generator first. Implement CPCS as an alternate NVMe backend, then add deeper benchmark hooks only where required.

Use this staged design:

1. Backend-only CPCS path: compatible with the existing `StorageBackend` API.
2. Arena/block-addressed CPCS path: real CPCS commands use namespace offsets/LBAs, not host filesystem paths.
3. Optional workload extensions: batch read, block selection, and prefix-index lookup.

This keeps the benchmark usable early and avoids invasive changes before the CPCS path is proven.

## 4. Demo targets

### Target A: Storage-side KV compression or quantization

Purpose: show reduced media bytes, reduced target storage footprint, and lower host CPU overhead compared with host-side transformation.

Implementation idea:

- On write, host sends raw KV bytes to the target-side staging area or arena.
- Host invokes CPCS program `kv_pack_store`.
- Target compresses or quantizes and stores the transformed object.
- On read, host invokes CPCS program `kv_unpack_load`.
- Target reconstructs a NumPy-compatible payload or returns packed bytes depending on mode.

Modes:

- `lossless_compress`: output must match exactly by `np.array_equal`.
- `int8_quantize`: output is lossy; record `max_abs_error`, `mean_abs_error`, and `mse`. Do not claim model quality impact from this benchmark.
- `mock`: fake CPCS implementation for tests and CI only.

Important risk:

- The benchmark KV generator intentionally creates noisy data, so generic compression may not help. Quantization or an optional controlled compressibility workload may be required to show a benefit. If adding a controlled data mode, label it clearly as a synthetic CPCS demonstration mode.

### Target B: Storage-side layout transform and coalescing

Purpose: show reduced small-I/O overhead by storing fragmented KV objects but returning a packed/coalesced buffer.

Implementation idea:

- Maintain an arena index: `key -> offset, length, shape, dtype, transform, checksum`.
- Add optional `read_many(keys)` or `cpcs_batch_read(keys)` path.
- CPCS program `kv_repack` reads multiple extents and writes a contiguous output buffer.
- Host reads one contiguous result instead of many small files/reads.

Initial benchmark integration:

- Add a backend capability method such as `supports_batch_read()`.
- Add fallback to individual `read()` calls.
- Only enable through a flag such as `--cpcs-mode layout`.

### Target C: Storage-side block filtering or top-k block selection

Purpose: show reduced NVMe-oF read bytes by returning only selected KV blocks.

Implementation idea:

- Split each KV cache object into fixed-size logical blocks.
- On decode/read, host sends compact selection metadata or query vector summary to CPCS.
- CPCS program `kv_select_blocks` returns selected block IDs or a compact packed payload.
- Benchmark records `selected_blocks / total_blocks` and `bytes_saved`.

Important limitation:

- The existing benchmark expects full NumPy arrays from `StorageBackend.read()`. To demonstrate real read-byte reduction, this target needs an extension to the benchmark semantics. Do not hide this. Label this mode as `experimental_partial_kv`.

### Target D: Storage-side prefix/index lookup

Purpose: show reduced host metadata work and lower prefix-cache lookup latency.

Implementation idea:

- Store a small prefix index or hash table on the target.
- CPCS program `kv_prefix_lookup` returns matching object metadata or arena offsets.
- Integrate later with `kv_cache/prefix_cache.py` or a new metadata path.

This is lower priority than Targets A and B because it may not stress the storage data path enough for a strong JBOF offload demo.

## 5. Architecture to implement

### 5.1 New files

Create these files unless the repository layout changes:

- `kv_cache/cpcs_client.py`
  - Defines the CPCS client interface.
  - Provides `MockCPCSClient` for CI and development.
  - Provides `SpdkPassthruCPCSClient` aligned to SPDK `spdk_nvme_passthru`.
  - Optionally provides a small `SpdkRpcBootstrap` helper for setup/probe via `scripts/rpc.py`.
  - Do not hard-code private target details; take transport/NQN/NSID/program IDs from CLI/config.

- `kv_cache/cpcs_backend.py`
  - Defines `CPCSNVMeBackend(StorageBackend)`.
  - Supports `file` mode for early proof and `arena` mode for real block-addressed CPCS.
  - Exposes normal `write`, `read`, `delete`, `clear`.
  - Optionally exposes `read_many` and `stats`.

- `kv_cache/cpcs_spdk_inventory.py`
  - Parses SPDK inventory YAML fields used by current CPCS automation.
  - Auto-fills passthru defaults (`trtype`, `traddr`, `trsvcid`, `subnqn`, `hostnqn`, `direct_probe_nsid`, `direct_probe_lba_bytes`) when CLI omits them.

- `kv_cache/cpcs_metrics.py`
  - Aggregates CPCS-specific counters and latencies.
  - Emits JSON-serializable summaries.

- `scripts/run_cpcs_demo_matrix.sh`
  - Runs baseline and CPCS trials with identical seeds.

- `scripts/compare_cpcs_results.py`
  - Compares benchmark JSON outputs and CPCS metrics.
  - Emits a Markdown or CSV summary.

- `scripts/prepare_cpcs_validation_pack.sh`
  - Generates deferred validation command packs (execution scripts + reporting checklist) without running benchmarks.
  - Includes real-target template flow artifacts (`15_run_real_target_matrix.sh`, `50_real_target_env.template`) and milestone command queue mappings (`60_milestone3_validation_queue.md`, `70_milestone4_validation_queue.md`, `80_milestone5_demo_report_queue.md`).

- `docs/cpcs_demo.md`
  - Documents setup, flags, commands, and limitations.

### 5.2 Existing files to modify

- `kv_cache/backends.py`
  - Keep existing `NVMeBackend` unchanged if possible.
  - Either import `CPCSNVMeBackend` here or keep it in `cpcs_backend.py` and import from `cache.py`.

- `kv_cache/cache.py`
  - Add backend selection logic.
  - Default must remain `NVMeBackend(base_path=cache_dir)`.
  - If `--nvme-backend cpcs`, instantiate `CPCSNVMeBackend`.

- `kv_cache/cli.py`
  - Add CPCS flags.
  - Include CPCS config in result metadata.
  - Add CPCS summary metrics to JSON and Excel output where possible.

- `kv_cache/monitoring.py`
  - Add optional CPCS metrics collection or merge CPCS metrics into existing summary.

- `config.yaml`
  - Add a `cpcs:` section with safe defaults.

- Tests under the existing test directory
  - Add unit tests for mock CPCS backend.
  - Add tests proving baseline behavior is unchanged when CPCS is disabled.

## 6. CPCS command interface (SPDK-aligned)

Use the same interface already validated in the real-SPDK cutover path.

Decision:

- Control plane (setup/provisioning): SPDK RPC (`scripts/rpc.py`) to configure `spdk_tgt`.
- Data and execute plane: SPDK passthru binary (`build/bin/spdk_nvme_passthru`) to send SLM/CPCS commands over NVMe-oF.
- Do not introduce `nvme-cli`, ioctl, or custom one-off transports for Milestone 3/4 unless SPDK path is proven broken.

This keeps the benchmark path consistent with:

- `spdk/test/cpcs/scenarios/realapp_bridge.py`
- `experiments/real_apps/common/run_real_app_hybrid_runner.py`
- `experiments/real_apps/common/run_real_app_cpcs_direct_passthru.py`
- `spdk/test/cpcs/cpcs_vector_eval_compare.py`
- `spdk/test/cpcs/vslm_pslm_perf_compare.py`

### 6.1 Provisioning interface to `spdk_tgt`

Use RPC calls already used in SPDK CPCS scenarios:

- `nvmf_create_subsystem`
- `bdev_nvme_attach_controller` or `bdev_malloc_create` as needed
- `bdev_slm_create` or `bdev_vslm_create`
- `nvmf_subsystem_add_ns`
- `cpcs_ns_create`
- `cpcs_program_install_builtins`

The KV-cache benchmark does not have to own full lifecycle orchestration, but must support a setup mode that can verify these resources exist.

### 6.2 Runtime command interface (passthru)

Use `spdk_nvme_passthru` as the only real command transport for CPCS/SLM in this plan:

- SLM staging/copy command: NVMe I/O opcode `0x01` to SLM namespace with descriptor payload.
- CPCS execute command: NVMe I/O opcode `0x01` to CPCS namespace with request payload (`cdw2 = (rsid << 16) | pind`).
  - Support both global defaults and per-operation overrides for `(rsid, pind)` bindings.
  - Expected operation keys: `pack_store`, `unpack_load`, `layout_repack`, `block_select`, `prefix_lookup`, `batch_read`.
- CPCS MRS management: NVMe admin opcode `0x89` to CPCS namespace.
- Optional downloadable-program path, if needed later:
  - load program: admin opcode `0x85`
  - activate program: admin opcode `0x88`

For dataset read probes, follow the direct runner convention that converts byte offset/length to LBA fields (`cdw10/cdw11/cdw12`) using namespace logical block size.
For SLM data reads, follow SPDK CPCS vector scripts (`cpcs_vector_eval_compare.py`, `cpcs_vector_device_eval.py`) where opcode `0x02` uses byte offset/byte length in `cdw10/cdw11/cdw12`.

Suggested Python interface:

```python
from dataclasses import dataclass
from typing import Any, Dict, Optional

@dataclass
class CPCSResult:
    status: str
    output_offset: Optional[int]
    output_length: int
    bytes_in: int
    bytes_out: int
    device_compute_us: int
    command_latency_s: float
    extra: Dict[str, Any]

class CPCSClient:
    def probe(self) -> Dict[str, Any]:
        raise NotImplementedError

    def ensure_runtime_ready(self) -> Dict[str, Any]:
        raise NotImplementedError

    def create_mrs(self, slm_nsid: int, ranges: list[dict]) -> int:
        raise NotImplementedError

    def execute(self, *, cpcs_nsid: int, rsid: int, pind: int, payload: bytes) -> CPCSResult:
        raise NotImplementedError

    def slm_write(self, *, slm_nsid: int, offset_bytes: int, payload: bytes) -> CPCSResult:
        raise NotImplementedError

    def slm_copy(self, *, slm_nsid: int, payload_desc: bytes, dest_offset: int, byte_len: int) -> CPCSResult:
        raise NotImplementedError

    def slm_read(self, *, slm_nsid: int, offset_bytes: int, length_bytes: int) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass
```

Concrete implementation classes:

- `MockCPCSClient`: deterministic local mock for CI.
- `SpdkPassthruCPCSClient`: subprocess wrapper around `spdk_nvme_passthru`.
- `SpdkRpcBootstrap`: optional helper to validate/setup required namespaces/programs via `rpc.py`.

Keep transport-specific code isolated in `cpcs_client.py` or a small submodule, not scattered through the benchmark.

## 7. Storage format

### 7.1 Quick file mode

Use only for early development and mock CPCS:

- Keep `.npy` files under `--cache-dir`.
- CPCS mock reads/writes those files or sidecar files.
- This mode is useful for unit tests but may not represent real NVMe CPCS, because the target normally sees LBAs, not host file paths.

### 7.2 Real arena mode

Use for actual CPCS demo on NVMe-oF JBOF:

- Create an arena file or raw block-device region under the target namespace.
- Maintain a host-side index file, for example `cpcs_index.sqlite` or `cpcs_index.jsonl`.
- Map every key to aligned extents:
  - `key`
  - `offset_bytes`
  - `length_bytes_raw`
  - `length_bytes_stored`
  - `shape`
  - `dtype`
  - `transform`
  - `checksum_raw` or `checksum_stored`
  - `created_at`
- Align extents to the namespace logical block size, preferably 4 KiB or larger.
- For large objects, prefer append-only allocation first; add free-list reuse later.

Do not rely on the target understanding the host filesystem unless your CPCS implementation explicitly supports that.

## 8. CLI flags

Add these flags with safe defaults:

```text
--nvme-backend {file,cpcs}              default: file
--cpcs-mode {off,noop,lossless_compress,int8_quantize,layout,block_select,prefix_index}
--cpcs-client {mock,spdk_passthru}      default: mock for dev, spdk_passthru for real runs
--spdk-inventory PATH                   optional; auto-fill transport/nsid defaults from inventory YAML
--spdk-rpc-script PATH                  default: scripts/rpc.py
--bootstrap-subsystem-nqn STR           optional override for bootstrap RPC subsystem NQN
--cpcs-bootstrap-install-builtins       optional RPC install of built-in CPCS programs
--cpcs-bootstrap-list-programs          optional RPC snapshot of cpcs_program_list
--cpcs-bootstrap-list-mrs               optional RPC snapshot of cpcs_mrs_list
--spdk-nvme-passthru PATH               default: build/bin/spdk_nvme_passthru
--trtype STR                            default: TCP
--traddr STR
--trsvcid STR
--subnqn STR
--hostnqn STR
--src-addr STR                          optional
--src-svcid STR                         optional
--passthru-lcores STR                   default: 1
--dataset-nsid INT                      dataset namespace for read probes
--slm-nsid INT                          SLM namespace ID
--cpcs-nsid INT                         CPCS namespace ID
--cpcs-program-pind INT                 optional explicit program index
--cpcs-program-pind-pack-store INT      optional per-op override (-1 inherit default)
--cpcs-program-pind-unpack-load INT     optional per-op override (-1 inherit default)
--cpcs-program-pind-layout-repack INT   optional per-op override (-1 inherit default)
--cpcs-program-pind-block-select INT    optional per-op override (-1 inherit default)
--cpcs-program-pind-prefix-lookup INT   optional per-op override (-1 inherit default)
--cpcs-program-pind-batch-read INT      optional per-op override (-1 inherit default)
--cpcs-rsid INT                         optional explicit MRS ID (if not auto-created)
--cpcs-rsid-pack-store INT              optional per-op override (-1 inherit default)
--cpcs-rsid-unpack-load INT             optional per-op override (-1 inherit default)
--cpcs-rsid-layout-repack INT           optional per-op override (-1 inherit default)
--cpcs-rsid-block-select INT            optional per-op override (-1 inherit default)
--cpcs-rsid-prefix-lookup INT           optional per-op override (-1 inherit default)
--cpcs-rsid-batch-read INT              optional per-op override (-1 inherit default)
--cpcs-auto-create-mrs                  optional auto-create MRS at backend init
--cpcs-mrs-ranges STR                   optional MRS descriptor ranges ("offset:length,..." or JSON list)
--cpcs-mrs-default-length-bytes INT     default length used by auto-create when ranges omitted
--cpcs-mrs-align-bytes INT              optional range alignment bytes (0 inherits probe LBA bytes)
--cpcs-mrs-align-mode STR               one of none|strict|round for MRS alignment behavior
--cpcs-load-program-path PATH           optional program blob for admin opcode 0x85 load
--cpcs-load-program-pind INT            program index for loaded program (-1 inherit default PIND)
--cpcs-load-program-set-default-pind    optional apply loaded program PIND as default execute PIND
--cpcs-load-program-chunk-bytes INT     optional chunk size for load-program transfer loop
--cpcs-load-program-ptype INT           program type for load command
--cpcs-load-program-pit INT             program implementation type for load command
--cpcs-load-program-puid INT            program identifier for load command
--cpcs-activate-loaded-program          optional activate command (admin opcode 0x88) after load
--direct-probe-offset INT               default: 0
--direct-probe-length INT               default: 4096
--direct-probe-lba-bytes INT            default from inventory or 4096
--cpcs-slm-rw-lba-bytes INT             optional SLM LBA size when SLM read/write mode is lba
--cpcs-slm-read-address-mode STR        one of byte|lba (byte matches SPDK CPCS vector scripts)
--cpcs-slm-write-address-mode STR       one of byte|lba (lba default for raw arena writes)
--cpcs-arena-path PATH                  file or block-device region for arena mode
--cpcs-index-path PATH
--cpcs-metrics-output PATH              JSONL or JSON summary
--cpcs-verify-every-n INT               default: 0, disabled; use 1 for early testing
--cpcs-lossy-tolerance FLOAT            default: 0.0 for lossless, mode-specific for quantize
--cpcs-block-size-kb INT                default: 1024 or 2048 for KV object chunks
--cpcs-batch-size INT                   default: 1; used by layout/read_many
--cpcs-fallback-on-error                default: false for benchmarks, true may be useful in dev
```

Rules:

- `--nvme-backend file` must preserve current behavior exactly.
- `--cpcs-client mock` must never be used for final performance claims.
- Real CPCS runs must use `--cpcs-client spdk_passthru` with explicit transport/NQN/NSID values or `--spdk-inventory`.
- `--spdk-inventory` numeric runtime fields should accept decimal and `0x`-prefixed hex forms.
- If CPCS fails and fallback is disabled, fail fast with a clear error.
- Include all CPCS flags in output metadata.

## 9. Metrics

### 9.1 Benchmark-native metrics

Preserve and compare:

- `Avg Throughput (tok/s)`.
- `Storage Throughput (tok/s)`.
- Requests/sec.
- End-to-end latency P50/P95/P99.
- Storage I/O latency P50/P95/P99.
- Storage read/write latency distributions.
- Cache hit rate.
- Total requests and total tokens.

### 9.2 CPCS-specific metrics

Add:

- `cpcs_commands_total`.
- `cpcs_commands_failed`.
- `cpcs_command_latency_ms_p50/p95/p99`.
- `cpcs_device_compute_us_p50/p95/p99`.
- `cpcs_bytes_in_total`.
- `cpcs_bytes_out_total`.
- `cpcs_media_bytes_read_est`.
- `cpcs_media_bytes_written_est`.
- `cpcs_compression_ratio` or `cpcs_pack_ratio`.
- `cpcs_selected_blocks_ratio` for block selection.
- `cpcs_host_cpu_seconds_saved_est`, if measurable.
- `cpcs_target_cpu_percent`, if target telemetry is available.

Implementation note:
- `cpcs_media_bytes_read_est` / `cpcs_media_bytes_written_est` may be populated from command-transfer estimates first (SLM read/write/copy payload sizes), with target-side counters preferred during deferred validation.

### 9.3 System metrics

Benchmark-native summary now includes lightweight runtime counters:

- `summary.system_metrics.host_cpu_percent_avg`
- `summary.system_metrics.process_cpu_percent_avg`
- `summary.system_metrics.process_cpu_time_s`
- `summary.system_metrics.process_max_rss_bytes`
- `summary.system_metrics.fabric_rx_bytes` / `fabric_tx_bytes` (Linux `/proc/net/dev`, non-loopback aggregate)
- `summary.system_metrics.process_io_read_bytes` / `process_io_write_bytes` (Linux `/proc/self/io`)

For higher-fidelity host and target attribution, still collect external evidence per run:

- `uname -a`.
- `lscpu`.
- `lsblk -o NAME,MODEL,SIZE,ROTA,TYPE,MOUNTPOINTS`.
- `nvme list`.
- NVMe-oF transport details.
- `iostat -x 1` log for the client device.
- `pidstat -dur -p <benchmark_pid> 1` or equivalent.
- NIC byte counters before and after each run.
- Target-side CPU, disk, and NIC counters.
- CPCS program logs from the target.

Implementation note:
- `scripts/run_cpcs_demo_matrix.sh` now supports optional system-metrics artifact capture (`--collect-system-metrics`) and sampler toggles (`--system-metrics-pidstat`, `--system-metrics-iostat`) so run-paired telemetry files can be generated when validation execution is enabled.

## 10. Correctness validation

Add correctness checks before performance testing.

Required tests:

1. Write/read one small array, lossless mode, exact equality.
2. Write/read one large array, lossless mode, exact equality.
3. Write/read many keys concurrently, exact equality.
4. Delete and clear behavior matches `NVMeBackend`.
5. Quantized mode records error metrics and stays within configured tolerance.
6. Mock client produces deterministic metrics.
7. CPCS-disabled path produces the same output structure as the original benchmark.

For quantized or partial modes, do not claim model quality. This benchmark does not run a real LLM accuracy test.

## 11. Experiment matrix

Use identical workload parameters, seeds, and target setup for baseline and CPCS runs.

### 11.1 Environment setup

```bash
git clone https://github.com/mlcommons/storage.git
cd storage/kv_cache_benchmark
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e ".[yaml,reporting,dev]"
python kv-cache.py --help
pytest -q
```

If editable install fails because extras changed, use the repository README and install the listed requirements. Record the exact command in `progress.md`.

### 11.2 Baseline storage-only run

```bash
python3 kv-cache.py \
  --config config.yaml \
  --model llama3.1-8b \
  --num-users 50 \
  --duration 180 \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 4 \
  --generation-mode realistic \
  --cache-dir /mnt/kv-nvmeof-baseline \
  --seed 42 \
  --output results/baseline_storage_only.json
```

### 11.3 Maximum NVMe stress baseline

```bash
python3 kv-cache.py \
  --config config.yaml \
  --model llama3.1-8b \
  --num-users 200 \
  --duration 300 \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 0 \
  --max-concurrent-allocs 16 \
  --disable-multi-turn \
  --disable-prefix-caching \
  --generation-mode none \
  --cache-dir /mnt/kv-nvmeof-baseline \
  --seed 42 \
  --output results/baseline_stress_8b.json
```

### 11.4 Prefill-only write-heavy run

```bash
python3 kv-cache.py \
  --config config.yaml \
  --model llama3.1-70b-instruct \
  --prefill-only \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 0 \
  --num-users 100 \
  --duration 120 \
  --cache-dir /mnt/kv-nvmeof-baseline \
  --seed 42 \
  --output results/baseline_prefill_only_70b.json
```

### 11.5 Decode-only read-heavy run

```bash
python3 kv-cache.py \
  --config config.yaml \
  --model llama3.1-70b-instruct \
  --decode-only \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 0 \
  --num-users 100 \
  --duration 120 \
  --cache-dir /mnt/kv-nvmeof-baseline \
  --seed 42 \
  --output results/baseline_decode_only_70b.json
```

### 11.6 CPCS runs

Run the same commands, changing only:

```text
--nvme-backend cpcs
--cpcs-mode <mode>
--cpcs-client spdk_passthru
--spdk-nvme-passthru <path>
--trtype <trtype>
--traddr <traddr>
--trsvcid <trsvcid>
--subnqn <subnqn>
--hostnqn <hostnqn>
--dataset-nsid <nsid>
--slm-nsid <nsid>
--cpcs-nsid <nsid>
--cpcs-arena-path <path>
--cpcs-index-path <path>
--cpcs-metrics-output <path>
--cache-dir /mnt/kv-nvmeof-cpcs
```

Example:

```bash
python3 kv-cache.py \
  --config config.yaml \
  --model llama3.1-8b \
  --num-users 50 \
  --duration 180 \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 4 \
  --generation-mode realistic \
  --cache-dir /mnt/kv-nvmeof-cpcs \
  --seed 42 \
  --nvme-backend cpcs \
  --cpcs-mode int8_quantize \
  --cpcs-client spdk_passthru \
  --spdk-nvme-passthru /path/to/spdk/build/bin/spdk_nvme_passthru \
  --trtype TCP \
  --traddr 10.0.0.22 \
  --trsvcid 4420 \
  --subnqn nqn.2026-03.io.spdk:cpcs-exp \
  --hostnqn nqn.2026-03.io.spdk:cpcs-exp-host \
  --dataset-nsid 1 \
  --slm-nsid 101 \
  --cpcs-nsid 200 \
  --passthru-lcores 1 \
  --direct-probe-lba-bytes 512 \
  --cpcs-arena-path /mnt/kv-nvmeof-cpcs/cpcs_arena.bin \
  --cpcs-index-path /mnt/kv-nvmeof-cpcs/cpcs_index.sqlite \
  --cpcs-metrics-output results/cpcs_storage_only_metrics.json \
  --output results/cpcs_storage_only.json
```

## 12. Comparison report

Implement `scripts/compare_cpcs_results.py` with this output:

```text
run_name, baseline_json, cpcs_json
baseline_system_metrics_dir
cpcs_system_metrics_dir
throughput_delta_percent
storage_throughput_delta_percent
storage_p95_delta_percent
storage_p99_delta_percent
host_cpu_delta_percent
fabric_rx_bytes_delta_percent
fabric_tx_bytes_delta_percent
media_read_bytes_delta_percent
media_write_bytes_delta_percent
cpcs_command_p95_ms
cpcs_compute_p95_us
pack_ratio_or_selectivity
demo_claim_met
demo_claim_reason
correctness_status
notes
```

The script should also generate `results/cpcs_comparison.md`.

## 13. Acceptance criteria

### Milestone 1: Baseline reproducibility

- Benchmark installs and tests pass.
- Baseline storage-only run completes on NVMe-oF mount.
- Baseline stress, prefill-only, and decode-only runs complete or documented if hardware capacity is insufficient.
- Results are saved under `results/`.

### Milestone 2: Mock CPCS backend

- `--nvme-backend cpcs --cpcs-client mock --cpcs-mode noop` completes.
- Mock lossless compression mode passes correctness tests.
- CPCS metrics appear in output JSON.
- Default file backend remains unchanged.

### Milestone 3: Real CPCS no-op path

- Real CPCS client can probe device/target.
- Real no-op CPCS program executes and metrics are recorded.
- No-op overhead is measured against baseline.

### Milestone 4: Real CPCS transform path

- At least one transform mode works end-to-end.
- Correctness checks pass or quantization error is recorded within configured tolerance.
- Repeated trials show median results.

### Milestone 5: Demo claim

A valid demo claim must satisfy at least one of these with the same workload and seed:

- Host CPU utilization improves by at least 10 percent.
- Fabric bytes fall by at least 10 percent.
- Media bytes written/read fall by at least 10 percent.
- Storage I/O P95 improves by at least 10 percent.
- Storage throughput tokens/sec improves by at least 10 percent.

Also verify:

- CPCS command failures are zero.
- Correctness is documented.
- Target-side resource cost is shown.
- Results include at least 3 trials; report median, not only best run.

## 14. Agent workflow rules

The Codex agent must:

1. Read `plan.md` and `progress.md` before making changes.
2. Update `progress.md` before starting a new milestone.
3. Update `progress.md` after every meaningful code change, test run, benchmark run, or blocker.
4. Record exact commands, working directory, branch name, commit hash, and key outputs.
5. Never overwrite benchmark results without moving old results to an archive directory.
6. Keep CPCS disabled by default.
7. Keep mock results separate from real CPCS results.
8. Mark uncertain findings as uncertain.
9. Avoid claiming MLPerf-compliant results for modified CPCS modes unless MLPerf rules are separately verified.

### 14.1 Task-finished handoff

When a single implementation task is finished, do this immediately:

1. Append a new entry in `progress.md` with files changed, exact commands, summary, blockers, and uncertainties.
2. Update `Current resume state` in `progress.md`, especially `last_successful_command` and `next_action`.
3. If local kickoff mirror files are maintained, sync updated `plan.md` and `progress.md` to the kickoff timeline copy.
4. Select the next unchecked implementation item and begin it without running deferred validation commands.
5. If no implementation items remain, prepare the validation command queue and stop before execution until user approval.

## 15. First tasks for Codex

Start here:

1. Open `progress.md` and set status to `In Progress`.
2. Record environment details.
3. Clone or locate the repository.
4. Inspect current `kv_cache/backends.py`, `kv_cache/cache.py`, and `kv_cache/cli.py`.
5. Run the current unit tests.
6. Run one short baseline smoke test with `--gpu-mem-gb 0` and small duration.
7. Create a branch named `cpcs-kv-cache-demo`.
8. Implement only the mock CPCS interface first.

Suggested smoke test:

```bash
python3 kv-cache.py \
  --config config.yaml \
  --model tiny-1b \
  --num-users 2 \
  --duration 10 \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 0 \
  --generation-mode none \
  --cache-dir /tmp/kv-smoke \
  --seed 42 \
  --output results/smoke_baseline.json
```

## 16. Source references checked while creating this plan

- MLPerf Storage KV Cache Benchmark README: https://github.com/mlcommons/storage/tree/main/kv_cache_benchmark
- Backend implementation: https://github.com/mlcommons/storage/blob/main/kv_cache_benchmark/kv_cache/backends.py
- Cache integration: https://github.com/mlcommons/storage/blob/main/kv_cache_benchmark/kv_cache/cache.py
- CLI flags and output metadata: https://github.com/mlcommons/storage/blob/main/kv_cache_benchmark/kv_cache/cli.py
- NVM Express Computational Programs Command Set page: https://nvmexpress.org/specification/computational-programs-command-set/
- SPDK unified CPCS platform: `spdk/test/cpcs/cpcs_experiments.py`
- SPDK inventory/runtime contract: `spdk/test/cpcs/experiment_platform.py`
- Realapp CPCS bridge defaults and passthru wiring: `spdk/test/cpcs/scenarios/realapp_bridge.py`
- Direct CPCS passthru runner: `experiments/real_apps/common/run_real_app_cpcs_direct_passthru.py`
- Hybrid-by-mode dispatcher: `experiments/real_apps/common/run_real_app_hybrid_runner.py`
- SLM/CPCS command examples and opcode usage: `spdk/test/cpcs/cpcs_vector_eval_compare.py`, `spdk/test/cpcs/vslm_pslm_perf_compare.py`
- CPCS command structures: `spdk/include/spdk/nvme_cpcs_spec.h`
