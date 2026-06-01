# CPCS KV-Cache Offload Demo

## Scope
This benchmark now supports an implementation-first CPCS path with two runtime layers:

- Control/setup path: optional SPDK RPC bootstrap helper (`SpdkRpcBootstrap`).
- Data/execute path: `spdk_nvme_passthru` client (`SpdkPassthruCPCSClient`).

The default benchmark behavior remains unchanged (`--nvme-backend file`).

## Implemented Components

- CPCS client layer:
  - `MockCPCSClient` for local/offline development.
  - `SpdkPassthruCPCSClient` for real passthru commands.
  - `SpdkRpcBootstrap` helper for rpc.py method/runtime checks.
  - SPDK-aligned program-management helpers:
    - optional built-in install via RPC (`cpcs_program_install_builtins`)
    - optional user program load (`admin opcode 0x85`) + activate (`admin opcode 0x88`)
    - MRS create (`admin opcode 0x89`) bound to CPCS namespace with SLM range descriptors.
- CPCS backend layer (`CPCSNVMeBackend`):
  - Transform modes: `noop`, `lossless_compress`, `int8_quantize`.
  - Mode-specific command routing:
    - `layout` -> `layout_repack`/`batch_read` profile.
    - `block_select` -> `block_select` read profile (+ batch descriptor path).
    - `prefix_index` -> `prefix_lookup` read profile (+ batch descriptor path).
  - Storage layout modes:
    - `file`: per-key `*.cpcs` payload files.
    - `arena`: append-only arena file + JSON index.
    - In `arena` + `spdk_passthru`, staging reads/writes use `slm_write` / `slm_read` paths.
      `slm_copy` remains available for descriptor-based SLM copy flows.
  - Optional batch API: `supports_batch_read()` and `read_many(keys)`.
  - CPCS metrics summary + optional `--cpcs-metrics-output` JSON persistence.
  - Estimated media-byte counters are populated from CPCS/SLM command paths:
    - `cpcs_media_bytes_read_est`
    - `cpcs_media_bytes_written_est`
  - Block-select mode summary exposes:
    - `cpcs_selected_blocks_ratio`
- Benchmark summary/runtime metrics:
  - `summary.system_metrics` now includes host/process runtime counters without external tooling.
  - Exposed compatibility fields in summary root:
    - `host_cpu_percent_avg`
    - `fabric_rx_bytes`
    - `fabric_tx_bytes`
- Inventory mapping:
  - `--spdk-inventory` support with passthru defaults (`traddr`, `trsvcid`, `subnqn`, NSIDs, probe geometry).
- Automation scripts:
  - `scripts/run_cpcs_demo_matrix.sh`
  - `scripts/compare_cpcs_results.py`
  - `scripts/prepare_cpcs_validation_pack.sh`

## CLI Flags

Core CPCS flags:

- `--nvme-backend cpcs`
- `--cpcs-mode {noop,lossless_compress,int8_quantize,layout,block_select,prefix_index}`
- `--cpcs-client {mock,spdk_passthru}`
- `--cpcs-storage-mode {file,arena}`
- `--cpcs-arena-path PATH`
- `--cpcs-index-path PATH`
- `--cpcs-metrics-output PATH`
- `--spdk-inventory PATH`
- `--spdk-nvme-passthru PATH`
- `--spdk-rpc-script PATH`
- `--spdk-rpc-python PATH`
- `--spdk-rpc-socket PATH`
- `--bootstrap-subsystem-nqn STR`
- `--cpcs-bootstrap-check`
- `--cpcs-bootstrap-install-builtins`
- `--cpcs-bootstrap-list-programs`
- `--cpcs-bootstrap-list-mrs`
- `--cpcs-required-rpc-methods method_a,method_b`
- `--trtype/--traddr/--trsvcid/--subnqn/--hostnqn`
- `--dataset-nsid/--slm-nsid/--cpcs-nsid`
- `--cpcs-program-pind` (default program index)
- `--cpcs-program-pind-pack-store` / `--cpcs-program-pind-unpack-load`
- `--cpcs-program-pind-layout-repack` / `--cpcs-program-pind-block-select`
- `--cpcs-program-pind-prefix-lookup` / `--cpcs-program-pind-batch-read`
- `--cpcs-rsid` (default memory-range-set id)
- `--cpcs-rsid-pack-store` / `--cpcs-rsid-unpack-load`
- `--cpcs-rsid-layout-repack` / `--cpcs-rsid-block-select`
- `--cpcs-rsid-prefix-lookup` / `--cpcs-rsid-batch-read`
- `--cpcs-auto-create-mrs`
- `--cpcs-mrs-ranges "0:65536,65536:65536"` (or JSON list)
- `--cpcs-mrs-default-length-bytes 65536`
- `--cpcs-mrs-align-bytes 4096`
- `--cpcs-mrs-align-mode {none,strict,round}`
- `--cpcs-load-program-path /path/to/program.bin`
- `--cpcs-load-program-pind` / `--cpcs-load-program-ptype`
- `--cpcs-load-program-set-default-pind`
- `--cpcs-load-program-chunk-bytes`
- `--cpcs-load-program-pit` / `--cpcs-load-program-puid`
- `--cpcs-activate-loaded-program`
- `--direct-probe-offset/--direct-probe-length/--direct-probe-lba-bytes`
- `--cpcs-slm-rw-lba-bytes`
- `--cpcs-slm-read-address-mode {byte,lba}`
- `--cpcs-slm-write-address-mode {byte,lba}`
- `--collect-system-metrics`
- `--system-metrics-dir PATH`
- `--system-metrics-net-iface IFACE`
- `--system-metrics-nvme-device DEV`
- `--system-metrics-pidstat`
- `--system-metrics-iostat`
- `--system-metrics-sample-sec N`

Inventory parsing note:
- Numeric runtime values in `--spdk-inventory` can be decimal or `0x`-prefixed hex strings (base-auto parsing).

## Matrix Runner

Dry-run command generation (no benchmark execution):

```bash
./scripts/run_cpcs_demo_matrix.sh \
  --run-prefix storage_only \
  --trials 3
```

Deferred validation pack generation (build commands/checklist, no execution):

```bash
./scripts/prepare_cpcs_validation_pack.sh \
  --run-prefix storage_only \
  --trials 3 \
  --collect-system-metrics \
  --system-metrics-pidstat \
  --system-metrics-iostat \
  -- \
  --model llama3.1-8b \
  --num-users 50 \
  --duration 180 \
  --cpcs-mode noop
```

This creates a timestamped pack under `results/cpcs_validation_pack/` with:
- `10_run_matrix.sh`
- `15_run_real_target_matrix.sh`
- `20_compare_results.sh`
- `30_run_all.sh`
- `40_reporting_checklist.md`
- `50_real_target_env.template`
- `60_milestone3_validation_queue.md`
- `70_milestone4_validation_queue.md`
- `80_milestone5_demo_report_queue.md`

Real-target template flow:

1. `cp 50_real_target_env.template 50_real_target_env.sh`
2. Fill SPDK transport/NQN/NSID/cache-dir fields in `50_real_target_env.sh`.
3. Run `./15_run_real_target_matrix.sh` (validates required env fields, then runs matrix).
4. Run `./20_compare_results.sh`.
5. Track Milestone 3 completion gates in `60_milestone3_validation_queue.md`.
6. Run Milestone 4 deferred transform queue from `70_milestone4_validation_queue.md`.
7. Assemble Milestone 5 demo report gates from `80_milestone5_demo_report_queue.md`.

Real execution mode:

```bash
./scripts/run_cpcs_demo_matrix.sh \
  --execute \
  --run-prefix storage_only \
  --trials 3 \
  --model llama3.1-8b \
  --num-users 50 \
  --duration 180 \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 4 \
  --generation-mode realistic \
  --cpcs-mode noop \
  --cpcs-client spdk_passthru \
  --cpcs-storage-mode arena \
  --cpcs-arena-path /mnt/kv-nvmeof-cpcs/cpcs_arena.bin \
  --cpcs-index-path /mnt/kv-nvmeof-cpcs/cpcs_index.json \
  --spdk-nvme-passthru /path/to/spdk/build/bin/spdk_nvme_passthru \
  --trtype TCP \
  --traddr 192.168.0.20 \
  --trsvcid 4420 \
  --subnqn nqn.2026-03.io.spdk:cpcs-exp \
  --hostnqn nqn.2026-03.io.spdk:cpcs-exp-host \
  --dataset-nsid 1 \
  --slm-nsid 100 \
  --cpcs-nsid 200
```

The script writes a manifest CSV:

- `results/cpcs/run_manifest.csv`

Manifest columns now include per-run system-metrics artifact directories:

- `baseline_system_metrics_dir`
- `cpcs_system_metrics_dir`

Benchmark integration note:

- Multi-turn previous-turn reload and RAG chunk reload now use batch cache access (`access_cache_many`) and leverage backend `read_many` where available.
- In CPCS special modes (`layout`, `block_select`, `prefix_index`), `read_many` issues a batch CPCS descriptor command before per-key reads.

Runtime provisioning notes:

- To install built-ins during init, add `--cpcs-bootstrap-install-builtins`.
- To auto-create RSID, add `--cpcs-auto-create-mrs` with optional `--cpcs-mrs-ranges`.
- `--cpcs-mrs-align-mode round` can auto-expand ranges to alignment boundaries; `strict` enforces exact alignment and fails on mismatch.
- To load an eBPF/custom program via passthru, pass `--cpcs-load-program-path` (plus optional load/activate flags).
- If the loaded program should become the runtime default for CPCS execute paths, also pass `--cpcs-load-program-set-default-pind` with `--cpcs-load-program-pind`.
- For larger program blobs, `--cpcs-load-program-chunk-bytes` enables chunked admin `0x85` transfers (`cdw15=LOFF`, `cdw14=NUMB`).
- `--cpcs-slm-read-address-mode byte` follows SPDK CPCS vector scripts (`opcode 0x02` byte offsets/length).
- `--cpcs-slm-write-address-mode lba` uses standard NVMe write addressing for raw arena persistence.
- `--collect-system-metrics` captures host snapshots before/after each run (`uname`, optional `lscpu`, `lsblk`, `nvme list`, `/proc/net/dev`).
- `--system-metrics-pidstat` and `--system-metrics-iostat` enable optional background samplers while each benchmark command runs.

## Result Comparison

From manifest:

```bash
./scripts/compare_cpcs_results.py \
  --manifest results/cpcs/run_manifest.csv \
  --output-csv results/cpcs_comparison.csv \
  --output-md results/cpcs_comparison.md
```

`results/cpcs_comparison.md` includes per-run rows and group-level medians
(for run names formatted like `group_t01`, `group_t02`, ...).
CSV/Markdown rows also include CPCS bootstrap/program binding fields
(`cpcs_bootstrap_ok`, `cpcs_program_rsid`, `cpcs_program_pind_default`,
`cpcs_slm_read_mode`, `cpcs_slm_write_mode`, `cpcs_slm_rw_lba_bytes`).
When present, host/fabric deltas are read from benchmark summary
(`host_cpu_percent_avg`, `fabric_rx_bytes`, `fabric_tx_bytes`) or
`summary.system_metrics`.
Comparison output now also includes:
- manifest-linked telemetry directories (`baseline_system_metrics_dir`, `cpcs_system_metrics_dir`)
- per-row demo-claim evaluation (`demo_claim_met`, `demo_claim_reason`) using the plan threshold rules.

Direct pair input:

```bash
./scripts/compare_cpcs_results.py \
  --pair run_a=results/baseline/run_a_baseline.json,results/cpcs/run_a_cpcs.json
```

## Notes

- Arena mode is append-only and does not reclaim space on `delete`; `clear` resets arena/index.
- `spdk_passthru` validation and performance characterization should be run separately after implementation lock.
- This workflow is for research/demo measurements, not an official closed MLPerf submission path.
