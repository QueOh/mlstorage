#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
KV_BENCH_BIN="${KV_BENCH_BIN:-${ROOT_DIR}/kv-cache.py}"

CONFIG_PATH="${CONFIG_PATH:-${ROOT_DIR}/config.yaml}"
RESULTS_DIR="${RESULTS_DIR:-${ROOT_DIR}/results}"
BASELINE_DIR="${BASELINE_DIR:-${RESULTS_DIR}/baseline}"
CPCS_DIR="${CPCS_DIR:-${RESULTS_DIR}/cpcs}"
MANIFEST_PATH="${MANIFEST_PATH:-${RESULTS_DIR}/cpcs/run_manifest.csv}"

RUN_PREFIX="${RUN_PREFIX:-storage_only}"
TRIALS="${TRIALS:-3}"
MODEL="${MODEL:-llama3.1-8b}"
NUM_USERS="${NUM_USERS:-50}"
DURATION="${DURATION:-180}"
GPU_MEM_GB="${GPU_MEM_GB:-0}"
CPU_MEM_GB="${CPU_MEM_GB:-4}"
GENERATION_MODE="${GENERATION_MODE:-realistic}"
SEED="${SEED:-42}"
MAX_REQUESTS="${MAX_REQUESTS:-0}"

CACHE_DIR_BASELINE="${CACHE_DIR_BASELINE:-/mnt/kv-nvmeof-baseline}"
CACHE_DIR_CPCS="${CACHE_DIR_CPCS:-/mnt/kv-nvmeof-cpcs}"

CPCS_MODE="${CPCS_MODE:-noop}"
CPCS_CLIENT="${CPCS_CLIENT:-spdk_passthru}"
CPCS_STORAGE_MODE="${CPCS_STORAGE_MODE:-file}"
CPCS_ARENA_PATH="${CPCS_ARENA_PATH:-}"
CPCS_INDEX_PATH="${CPCS_INDEX_PATH:-}"
CPCS_METRICS_DIR="${CPCS_METRICS_DIR:-${CPCS_DIR}/metrics}"

SPDK_INVENTORY="${SPDK_INVENTORY:-}"
SPDK_RPC_SCRIPT="${SPDK_RPC_SCRIPT:-}"
SPDK_RPC_PYTHON="${SPDK_RPC_PYTHON:-}"
SPDK_RPC_SOCKET="${SPDK_RPC_SOCKET:-}"
BOOTSTRAP_SUBSYSTEM_NQN="${BOOTSTRAP_SUBSYSTEM_NQN:-}"
CPCS_BOOTSTRAP_CHECK="${CPCS_BOOTSTRAP_CHECK:-0}"
CPCS_BOOTSTRAP_INSTALL_BUILTINS="${CPCS_BOOTSTRAP_INSTALL_BUILTINS:-0}"
CPCS_BOOTSTRAP_LIST_PROGRAMS="${CPCS_BOOTSTRAP_LIST_PROGRAMS:-0}"
CPCS_BOOTSTRAP_LIST_MRS="${CPCS_BOOTSTRAP_LIST_MRS:-0}"
CPCS_REQUIRED_RPC_METHODS="${CPCS_REQUIRED_RPC_METHODS:-}"
SPDK_NVME_PASSTHRU="${SPDK_NVME_PASSTHRU:-}"
TRTYPE="${TRTYPE:-}"
TRADDR="${TRADDR:-}"
TRSVCID="${TRSVCID:-}"
SUBNQN="${SUBNQN:-}"
HOSTNQN="${HOSTNQN:-}"
SRC_ADDR="${SRC_ADDR:-}"
SRC_SVCID="${SRC_SVCID:-}"
PASSTHRU_LCORES="${PASSTHRU_LCORES:-}"
DATASET_NSID="${DATASET_NSID:-}"
SLM_NSID="${SLM_NSID:-}"
CPCS_NSID="${CPCS_NSID:-}"
CPCS_PROGRAM_PIND="${CPCS_PROGRAM_PIND:-}"
CPCS_PROGRAM_PIND_PACK_STORE="${CPCS_PROGRAM_PIND_PACK_STORE:-}"
CPCS_PROGRAM_PIND_UNPACK_LOAD="${CPCS_PROGRAM_PIND_UNPACK_LOAD:-}"
CPCS_PROGRAM_PIND_LAYOUT_REPACK="${CPCS_PROGRAM_PIND_LAYOUT_REPACK:-}"
CPCS_PROGRAM_PIND_BLOCK_SELECT="${CPCS_PROGRAM_PIND_BLOCK_SELECT:-}"
CPCS_PROGRAM_PIND_PREFIX_LOOKUP="${CPCS_PROGRAM_PIND_PREFIX_LOOKUP:-}"
CPCS_PROGRAM_PIND_BATCH_READ="${CPCS_PROGRAM_PIND_BATCH_READ:-}"
CPCS_RSID="${CPCS_RSID:-}"
CPCS_RSID_PACK_STORE="${CPCS_RSID_PACK_STORE:-}"
CPCS_RSID_UNPACK_LOAD="${CPCS_RSID_UNPACK_LOAD:-}"
CPCS_RSID_LAYOUT_REPACK="${CPCS_RSID_LAYOUT_REPACK:-}"
CPCS_RSID_BLOCK_SELECT="${CPCS_RSID_BLOCK_SELECT:-}"
CPCS_RSID_PREFIX_LOOKUP="${CPCS_RSID_PREFIX_LOOKUP:-}"
CPCS_RSID_BATCH_READ="${CPCS_RSID_BATCH_READ:-}"
CPCS_AUTO_CREATE_MRS="${CPCS_AUTO_CREATE_MRS:-0}"
CPCS_MRS_RANGES="${CPCS_MRS_RANGES:-}"
CPCS_MRS_DEFAULT_LENGTH_BYTES="${CPCS_MRS_DEFAULT_LENGTH_BYTES:-}"
CPCS_MRS_ALIGN_BYTES="${CPCS_MRS_ALIGN_BYTES:-}"
CPCS_MRS_ALIGN_MODE="${CPCS_MRS_ALIGN_MODE:-}"
CPCS_LOAD_PROGRAM_PATH="${CPCS_LOAD_PROGRAM_PATH:-}"
CPCS_LOAD_PROGRAM_PIND="${CPCS_LOAD_PROGRAM_PIND:-}"
CPCS_LOAD_PROGRAM_SET_DEFAULT_PIND="${CPCS_LOAD_PROGRAM_SET_DEFAULT_PIND:-0}"
CPCS_LOAD_PROGRAM_CHUNK_BYTES="${CPCS_LOAD_PROGRAM_CHUNK_BYTES:-}"
CPCS_LOAD_PROGRAM_PTYPE="${CPCS_LOAD_PROGRAM_PTYPE:-}"
CPCS_LOAD_PROGRAM_PIT="${CPCS_LOAD_PROGRAM_PIT:-}"
CPCS_LOAD_PROGRAM_PUID="${CPCS_LOAD_PROGRAM_PUID:-}"
CPCS_ACTIVATE_LOADED_PROGRAM="${CPCS_ACTIVATE_LOADED_PROGRAM:-0}"
DIRECT_PROBE_OFFSET="${DIRECT_PROBE_OFFSET:-}"
DIRECT_PROBE_LENGTH="${DIRECT_PROBE_LENGTH:-}"
DIRECT_PROBE_LBA_BYTES="${DIRECT_PROBE_LBA_BYTES:-}"
CPCS_SLM_RW_LBA_BYTES="${CPCS_SLM_RW_LBA_BYTES:-}"
CPCS_SLM_READ_ADDRESS_MODE="${CPCS_SLM_READ_ADDRESS_MODE:-}"
CPCS_SLM_WRITE_ADDRESS_MODE="${CPCS_SLM_WRITE_ADDRESS_MODE:-}"

SYSTEM_METRICS_COLLECT="${SYSTEM_METRICS_COLLECT:-0}"
SYSTEM_METRICS_DIR="${SYSTEM_METRICS_DIR:-${RESULTS_DIR}/system_metrics}"
SYSTEM_METRICS_NET_IFACE="${SYSTEM_METRICS_NET_IFACE:-}"
SYSTEM_METRICS_NVME_DEVICE="${SYSTEM_METRICS_NVME_DEVICE:-}"
SYSTEM_METRICS_PIDSTAT_ENABLE="${SYSTEM_METRICS_PIDSTAT_ENABLE:-0}"
SYSTEM_METRICS_IOSTAT_ENABLE="${SYSTEM_METRICS_IOSTAT_ENABLE:-0}"
SYSTEM_METRICS_SAMPLE_SEC="${SYSTEM_METRICS_SAMPLE_SEC:-1}"

EXECUTE=0

usage() {
  cat <<'USAGE'
Usage:
  run_cpcs_demo_matrix.sh [options]

Options:
  --execute                    Actually run commands (default is dry-run).
  --trials N                   Number of baseline/CPCS trial pairs (default: 3).
  --run-prefix NAME            Prefix for run names.
  --model NAME                 Benchmark model (default: llama3.1-8b).
  --num-users N                Number of users.
  --duration SEC               Run duration in seconds.
  --gpu-mem-gb GB              GPU KV tier capacity.
  --cpu-mem-gb GB              CPU KV tier capacity.
  --generation-mode MODE       none|fast|realistic.
  --seed N                     Random seed.
  --max-requests N             Stop after N completed requests (0 disables).
  --cache-dir-baseline PATH    Cache directory for baseline backend.
  --cache-dir-cpcs PATH        Cache directory for CPCS backend.
  --cpcs-mode MODE             CPCS mode (noop/lossless_compress/int8_quantize/...).
  --cpcs-client NAME           mock|spdk_passthru.
  --cpcs-storage-mode MODE     file|arena.
  --cpcs-arena-path PATH       Arena file path for CPCS arena mode.
  --cpcs-index-path PATH       Arena index path for CPCS arena mode.
  --spdk-inventory PATH        SPDK inventory YAML path.
  --spdk-rpc-script PATH       rpc.py path for optional bootstrap checks.
  --spdk-rpc-python PATH       Python interpreter used for rpc.py.
  --spdk-rpc-socket PATH       Optional SPDK RPC socket.
  --bootstrap-subsystem-nqn STR  Optional NQN override for bootstrap RPC calls.
  --cpcs-bootstrap-check       Enable rpc bootstrap checks.
  --cpcs-bootstrap-install-builtins  Install built-in CPCS programs via rpc.py.
  --cpcs-bootstrap-list-programs     Query cpcs_program_list via rpc.py.
  --cpcs-bootstrap-list-mrs          Query cpcs_mrs_list via rpc.py.
  --cpcs-required-rpc-methods  Comma-separated required rpc methods.
  --spdk-nvme-passthru PATH    Path to spdk_nvme_passthru binary.
  --trtype STR                 NVMe-oF transport type.
  --traddr STR                 NVMe-oF target address.
  --trsvcid STR                NVMe-oF target service id.
  --subnqn STR                 NVMe-oF subsystem NQN.
  --hostnqn STR                Host NQN.
  --src-addr STR               Optional source address.
  --src-svcid STR              Optional source service id.
  --passthru-lcores STR        CPU cores for passthru tool.
  --dataset-nsid N             Dataset namespace id.
  --slm-nsid N                 SLM namespace id.
  --cpcs-nsid N                CPCS namespace id.
  --cpcs-program-pind N        CPCS program index.
  --cpcs-program-pind-pack-store N      Override pack_store program index.
  --cpcs-program-pind-unpack-load N     Override unpack_load program index.
  --cpcs-program-pind-layout-repack N   Override layout_repack program index.
  --cpcs-program-pind-block-select N    Override block_select program index.
  --cpcs-program-pind-prefix-lookup N   Override prefix_lookup program index.
  --cpcs-program-pind-batch-read N      Override batch_read program index.
  --cpcs-rsid N                CPCS memory range set id.
  --cpcs-rsid-pack-store N             Override pack_store RSID.
  --cpcs-rsid-unpack-load N            Override unpack_load RSID.
  --cpcs-rsid-layout-repack N          Override layout_repack RSID.
  --cpcs-rsid-block-select N           Override block_select RSID.
  --cpcs-rsid-prefix-lookup N          Override prefix_lookup RSID.
  --cpcs-rsid-batch-read N             Override batch_read RSID.
  --cpcs-auto-create-mrs        Auto-create CPCS MRS during backend init.
  --cpcs-mrs-ranges STR         MRS ranges spec ("offset:length,..." or JSON list).
  --cpcs-mrs-default-length-bytes N  Default auto-created MRS length.
  --cpcs-mrs-align-bytes N      MRS range alignment size (0 uses probe LBA bytes).
  --cpcs-mrs-align-mode MODE    none|strict|round.
  --cpcs-load-program-path PATH Optional CPCS program binary for opcode 0x85 load.
  --cpcs-load-program-pind N    Program index for loaded program.
  --cpcs-load-program-set-default-pind  Set loaded-program PIND as default CPCS execute PIND.
  --cpcs-load-program-chunk-bytes N  Optional chunk size for program load transfers.
  --cpcs-load-program-ptype N   Program type (PTYPE) for load.
  --cpcs-load-program-pit N     Program implementation type (PIT) for load.
  --cpcs-load-program-puid N    Program UID (PUID) for load.
  --cpcs-activate-loaded-program Activate loaded program via opcode 0x88.
  --direct-probe-offset N      Probe offset in bytes.
  --direct-probe-length N      Probe length in bytes.
  --direct-probe-lba-bytes N   Probe LBA size in bytes.
  --cpcs-slm-rw-lba-bytes N    LBA size for SLM read/write in lba mode.
  --cpcs-slm-read-address-mode MODE   byte|lba.
  --cpcs-slm-write-address-mode MODE  byte|lba.
  --collect-system-metrics      Capture before/after host snapshots per run.
  --system-metrics-dir PATH     Output directory for per-run system metrics artifacts.
  --system-metrics-net-iface IFACE  Optional network interface for detailed link stats.
  --system-metrics-nvme-device DEV  Optional device selector passed to iostat.
  --system-metrics-pidstat      Enable per-run `pidstat -dur` sampler (if available).
  --system-metrics-iostat       Enable per-run `iostat` sampler (if available).
  --system-metrics-sample-sec N Sampling interval (seconds) for pidstat/iostat.
  -h, --help                   Show this help.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) EXECUTE=1 ;;
    --trials) TRIALS="$2"; shift ;;
    --run-prefix) RUN_PREFIX="$2"; shift ;;
    --model) MODEL="$2"; shift ;;
    --num-users) NUM_USERS="$2"; shift ;;
    --duration) DURATION="$2"; shift ;;
    --gpu-mem-gb) GPU_MEM_GB="$2"; shift ;;
    --cpu-mem-gb) CPU_MEM_GB="$2"; shift ;;
    --generation-mode) GENERATION_MODE="$2"; shift ;;
    --seed) SEED="$2"; shift ;;
    --max-requests) MAX_REQUESTS="$2"; shift ;;
    --cache-dir-baseline) CACHE_DIR_BASELINE="$2"; shift ;;
    --cache-dir-cpcs) CACHE_DIR_CPCS="$2"; shift ;;
    --cpcs-mode) CPCS_MODE="$2"; shift ;;
    --cpcs-client) CPCS_CLIENT="$2"; shift ;;
    --cpcs-storage-mode) CPCS_STORAGE_MODE="$2"; shift ;;
    --cpcs-arena-path) CPCS_ARENA_PATH="$2"; shift ;;
    --cpcs-index-path) CPCS_INDEX_PATH="$2"; shift ;;
    --spdk-inventory) SPDK_INVENTORY="$2"; shift ;;
    --spdk-rpc-script) SPDK_RPC_SCRIPT="$2"; shift ;;
    --spdk-rpc-python) SPDK_RPC_PYTHON="$2"; shift ;;
    --spdk-rpc-socket) SPDK_RPC_SOCKET="$2"; shift ;;
    --bootstrap-subsystem-nqn) BOOTSTRAP_SUBSYSTEM_NQN="$2"; shift ;;
    --cpcs-bootstrap-check) CPCS_BOOTSTRAP_CHECK=1 ;;
    --cpcs-bootstrap-install-builtins) CPCS_BOOTSTRAP_INSTALL_BUILTINS=1 ;;
    --cpcs-bootstrap-list-programs) CPCS_BOOTSTRAP_LIST_PROGRAMS=1 ;;
    --cpcs-bootstrap-list-mrs) CPCS_BOOTSTRAP_LIST_MRS=1 ;;
    --cpcs-required-rpc-methods) CPCS_REQUIRED_RPC_METHODS="$2"; shift ;;
    --spdk-nvme-passthru) SPDK_NVME_PASSTHRU="$2"; shift ;;
    --trtype) TRTYPE="$2"; shift ;;
    --traddr) TRADDR="$2"; shift ;;
    --trsvcid) TRSVCID="$2"; shift ;;
    --subnqn) SUBNQN="$2"; shift ;;
    --hostnqn) HOSTNQN="$2"; shift ;;
    --src-addr) SRC_ADDR="$2"; shift ;;
    --src-svcid) SRC_SVCID="$2"; shift ;;
    --passthru-lcores) PASSTHRU_LCORES="$2"; shift ;;
    --dataset-nsid) DATASET_NSID="$2"; shift ;;
    --slm-nsid) SLM_NSID="$2"; shift ;;
    --cpcs-nsid) CPCS_NSID="$2"; shift ;;
    --cpcs-program-pind) CPCS_PROGRAM_PIND="$2"; shift ;;
    --cpcs-program-pind-pack-store) CPCS_PROGRAM_PIND_PACK_STORE="$2"; shift ;;
    --cpcs-program-pind-unpack-load) CPCS_PROGRAM_PIND_UNPACK_LOAD="$2"; shift ;;
    --cpcs-program-pind-layout-repack) CPCS_PROGRAM_PIND_LAYOUT_REPACK="$2"; shift ;;
    --cpcs-program-pind-block-select) CPCS_PROGRAM_PIND_BLOCK_SELECT="$2"; shift ;;
    --cpcs-program-pind-prefix-lookup) CPCS_PROGRAM_PIND_PREFIX_LOOKUP="$2"; shift ;;
    --cpcs-program-pind-batch-read) CPCS_PROGRAM_PIND_BATCH_READ="$2"; shift ;;
    --cpcs-rsid) CPCS_RSID="$2"; shift ;;
    --cpcs-rsid-pack-store) CPCS_RSID_PACK_STORE="$2"; shift ;;
    --cpcs-rsid-unpack-load) CPCS_RSID_UNPACK_LOAD="$2"; shift ;;
    --cpcs-rsid-layout-repack) CPCS_RSID_LAYOUT_REPACK="$2"; shift ;;
    --cpcs-rsid-block-select) CPCS_RSID_BLOCK_SELECT="$2"; shift ;;
    --cpcs-rsid-prefix-lookup) CPCS_RSID_PREFIX_LOOKUP="$2"; shift ;;
    --cpcs-rsid-batch-read) CPCS_RSID_BATCH_READ="$2"; shift ;;
    --cpcs-auto-create-mrs) CPCS_AUTO_CREATE_MRS=1 ;;
    --cpcs-mrs-ranges) CPCS_MRS_RANGES="$2"; shift ;;
    --cpcs-mrs-default-length-bytes) CPCS_MRS_DEFAULT_LENGTH_BYTES="$2"; shift ;;
    --cpcs-mrs-align-bytes) CPCS_MRS_ALIGN_BYTES="$2"; shift ;;
    --cpcs-mrs-align-mode) CPCS_MRS_ALIGN_MODE="$2"; shift ;;
    --cpcs-load-program-path) CPCS_LOAD_PROGRAM_PATH="$2"; shift ;;
    --cpcs-load-program-pind) CPCS_LOAD_PROGRAM_PIND="$2"; shift ;;
    --cpcs-load-program-set-default-pind) CPCS_LOAD_PROGRAM_SET_DEFAULT_PIND=1 ;;
    --cpcs-load-program-chunk-bytes) CPCS_LOAD_PROGRAM_CHUNK_BYTES="$2"; shift ;;
    --cpcs-load-program-ptype) CPCS_LOAD_PROGRAM_PTYPE="$2"; shift ;;
    --cpcs-load-program-pit) CPCS_LOAD_PROGRAM_PIT="$2"; shift ;;
    --cpcs-load-program-puid) CPCS_LOAD_PROGRAM_PUID="$2"; shift ;;
    --cpcs-activate-loaded-program) CPCS_ACTIVATE_LOADED_PROGRAM=1 ;;
    --direct-probe-offset) DIRECT_PROBE_OFFSET="$2"; shift ;;
    --direct-probe-length) DIRECT_PROBE_LENGTH="$2"; shift ;;
    --direct-probe-lba-bytes) DIRECT_PROBE_LBA_BYTES="$2"; shift ;;
    --cpcs-slm-rw-lba-bytes) CPCS_SLM_RW_LBA_BYTES="$2"; shift ;;
    --cpcs-slm-read-address-mode) CPCS_SLM_READ_ADDRESS_MODE="$2"; shift ;;
    --cpcs-slm-write-address-mode) CPCS_SLM_WRITE_ADDRESS_MODE="$2"; shift ;;
    --collect-system-metrics) SYSTEM_METRICS_COLLECT=1 ;;
    --system-metrics-dir) SYSTEM_METRICS_DIR="$2"; shift ;;
    --system-metrics-net-iface) SYSTEM_METRICS_NET_IFACE="$2"; shift ;;
    --system-metrics-nvme-device) SYSTEM_METRICS_NVME_DEVICE="$2"; shift ;;
    --system-metrics-pidstat) SYSTEM_METRICS_PIDSTAT_ENABLE=1 ;;
    --system-metrics-iostat) SYSTEM_METRICS_IOSTAT_ENABLE=1 ;;
    --system-metrics-sample-sec) SYSTEM_METRICS_SAMPLE_SEC="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

mkdir -p "${BASELINE_DIR}" "${CPCS_DIR}" "${CPCS_METRICS_DIR}" "$(dirname "${MANIFEST_PATH}")"
if [[ "${SYSTEM_METRICS_COLLECT}" == "1" ]]; then
  mkdir -p "${SYSTEM_METRICS_DIR}"
fi

COMMON_ARGS=(
  "--config" "${CONFIG_PATH}"
  "--model" "${MODEL}"
  "--num-users" "${NUM_USERS}"
  "--duration" "${DURATION}"
  "--gpu-mem-gb" "${GPU_MEM_GB}"
  "--cpu-mem-gb" "${CPU_MEM_GB}"
  "--generation-mode" "${GENERATION_MODE}"
  "--seed" "${SEED}"
)
if [[ "${MAX_REQUESTS}" != "0" ]]; then
  COMMON_ARGS+=("--max-requests" "${MAX_REQUESTS}")
fi

append_if_set() {
  local -n arr_ref="$1"
  local flag="$2"
  local value="$3"
  if [[ -n "${value}" ]]; then
    arr_ref+=("${flag}" "${value}")
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

metrics_dir_for_label() {
  local label="$1"
  local safe="${label//:/_}"
  safe="${safe//\//_}"
  safe="${safe// /_}"
  echo "${SYSTEM_METRICS_DIR}/${safe}"
}

capture_system_snapshot() {
  local out_file="$1"
  local label="$2"
  local phase="$3"
  mkdir -p "$(dirname "${out_file}")"
  {
    echo "timestamp: $(date '+%Y-%m-%dT%H:%M:%S%z')"
    echo "label: ${label}"
    echo "phase: ${phase}"
    echo "hostname: $(hostname 2>/dev/null || echo unknown)"
    echo "pwd: $(pwd)"
    echo
    echo "## uname -a"
    uname -a 2>&1 || true
    echo

    if command_exists lscpu; then
      echo "## lscpu"
      lscpu 2>&1 || true
      echo
    fi

    if command_exists lsblk; then
      echo "## lsblk -o NAME,MODEL,SIZE,ROTA,TYPE,MOUNTPOINTS"
      lsblk -o NAME,MODEL,SIZE,ROTA,TYPE,MOUNTPOINTS 2>&1 || true
      echo
    fi

    if command_exists nvme; then
      echo "## nvme list"
      nvme list 2>&1 || true
      echo
    fi

    if [[ -r "/proc/net/dev" ]]; then
      echo "## /proc/net/dev"
      cat /proc/net/dev 2>&1 || true
      echo
    fi

    if [[ -n "${SYSTEM_METRICS_NET_IFACE}" ]]; then
      if command_exists ip; then
        echo "## ip -s link show dev ${SYSTEM_METRICS_NET_IFACE}"
        ip -s link show dev "${SYSTEM_METRICS_NET_IFACE}" 2>&1 || true
        echo
      elif command_exists ifconfig; then
        echo "## ifconfig ${SYSTEM_METRICS_NET_IFACE}"
        ifconfig "${SYSTEM_METRICS_NET_IFACE}" 2>&1 || true
        echo
      fi
    fi
  } > "${out_file}"
}

stop_sampler() {
  local pid="$1"
  if [[ -z "${pid}" ]]; then
    return
  fi
  if kill -0 "${pid}" >/dev/null 2>&1; then
    kill "${pid}" >/dev/null 2>&1 || true
  fi
  wait "${pid}" >/dev/null 2>&1 || true
}

run_cmd() {
  local label="$1"
  shift
  echo "[${label}] $*"
  if [[ "${EXECUTE}" -eq 1 ]]; then
    if [[ "${SYSTEM_METRICS_COLLECT}" != "1" ]]; then
      "$@"
      return
    fi

    local run_metrics_dir
    run_metrics_dir="$(metrics_dir_for_label "${label}")"
    mkdir -p "${run_metrics_dir}"

    capture_system_snapshot "${run_metrics_dir}/before.txt" "${label}" "before"

    local pidstat_pid=""
    local iostat_pid=""
    local cmd_pid=""
    local run_start_iso
    local run_end_iso
    local run_exit=0

    run_start_iso="$(date '+%Y-%m-%dT%H:%M:%S%z')"

    "$@" &
    cmd_pid="$!"

    if [[ "${SYSTEM_METRICS_PIDSTAT_ENABLE}" == "1" ]]; then
      if command_exists pidstat; then
        pidstat -dur -h -p "${cmd_pid}" "${SYSTEM_METRICS_SAMPLE_SEC}" > "${run_metrics_dir}/pidstat.log" 2>&1 &
        pidstat_pid="$!"
      else
        echo "pidstat not found; skipping pidstat sampler for ${label}" >> "${run_metrics_dir}/warnings.log"
      fi
    fi

    if [[ "${SYSTEM_METRICS_IOSTAT_ENABLE}" == "1" ]]; then
      if command_exists iostat; then
        if [[ -n "${SYSTEM_METRICS_NVME_DEVICE}" ]]; then
          iostat -x "${SYSTEM_METRICS_NVME_DEVICE}" "${SYSTEM_METRICS_SAMPLE_SEC}" > "${run_metrics_dir}/iostat.log" 2>&1 &
        else
          iostat -x "${SYSTEM_METRICS_SAMPLE_SEC}" > "${run_metrics_dir}/iostat.log" 2>&1 &
        fi
        iostat_pid="$!"
      else
        echo "iostat not found; skipping iostat sampler for ${label}" >> "${run_metrics_dir}/warnings.log"
      fi
    fi

    set +e
    wait "${cmd_pid}"
    run_exit=$?
    set -e

    stop_sampler "${pidstat_pid}"
    stop_sampler "${iostat_pid}"

    run_end_iso="$(date '+%Y-%m-%dT%H:%M:%S%z')"
    capture_system_snapshot "${run_metrics_dir}/after.txt" "${label}" "after"

    {
      echo "label: ${label}"
      echo "start: ${run_start_iso}"
      echo "end: ${run_end_iso}"
      echo "exit_code: ${run_exit}"
      echo "pidstat_enabled: ${SYSTEM_METRICS_PIDSTAT_ENABLE}"
      echo "iostat_enabled: ${SYSTEM_METRICS_IOSTAT_ENABLE}"
      echo "sample_sec: ${SYSTEM_METRICS_SAMPLE_SEC}"
      if [[ -n "${SYSTEM_METRICS_NET_IFACE}" ]]; then
        echo "net_iface: ${SYSTEM_METRICS_NET_IFACE}"
      fi
      if [[ -n "${SYSTEM_METRICS_NVME_DEVICE}" ]]; then
        echo "nvme_device: ${SYSTEM_METRICS_NVME_DEVICE}"
      fi
    } > "${run_metrics_dir}/run_meta.txt"

    if [[ "${run_exit}" -ne 0 ]]; then
      return "${run_exit}"
    fi
  fi
}

printf "run_name,baseline_json,cpcs_json,baseline_system_metrics_dir,cpcs_system_metrics_dir\n" > "${MANIFEST_PATH}"

for trial in $(seq 1 "${TRIALS}"); do
  run_id="$(printf "%s_t%02d" "${RUN_PREFIX}" "${trial}")"
  baseline_json="${BASELINE_DIR}/${run_id}_baseline.json"
  cpcs_json="${CPCS_DIR}/${run_id}_cpcs.json"
  cpcs_metrics_json="${CPCS_METRICS_DIR}/${run_id}_cpcs_metrics.json"
  baseline_label="baseline:${run_id}"
  cpcs_label="cpcs:${run_id}"
  baseline_system_metrics_dir=""
  cpcs_system_metrics_dir=""
  if [[ "${SYSTEM_METRICS_COLLECT}" == "1" ]]; then
    baseline_system_metrics_dir="$(metrics_dir_for_label "${baseline_label}")"
    cpcs_system_metrics_dir="$(metrics_dir_for_label "${cpcs_label}")"
  fi

  baseline_cmd=(
    "${PYTHON_BIN}" "${KV_BENCH_BIN}"
    "${COMMON_ARGS[@]}"
    "--cache-dir" "${CACHE_DIR_BASELINE}"
    "--nvme-backend" "file"
    "--output" "${baseline_json}"
  )

  cpcs_cmd=(
    "${PYTHON_BIN}" "${KV_BENCH_BIN}"
    "${COMMON_ARGS[@]}"
    "--cache-dir" "${CACHE_DIR_CPCS}"
    "--nvme-backend" "cpcs"
    "--cpcs-mode" "${CPCS_MODE}"
    "--cpcs-client" "${CPCS_CLIENT}"
    "--cpcs-storage-mode" "${CPCS_STORAGE_MODE}"
    "--cpcs-metrics-output" "${cpcs_metrics_json}"
    "--output" "${cpcs_json}"
  )

  append_if_set cpcs_cmd "--cpcs-arena-path" "${CPCS_ARENA_PATH}"
  append_if_set cpcs_cmd "--cpcs-index-path" "${CPCS_INDEX_PATH}"
  append_if_set cpcs_cmd "--spdk-inventory" "${SPDK_INVENTORY}"
  append_if_set cpcs_cmd "--spdk-rpc-script" "${SPDK_RPC_SCRIPT}"
  append_if_set cpcs_cmd "--spdk-rpc-python" "${SPDK_RPC_PYTHON}"
  append_if_set cpcs_cmd "--spdk-rpc-socket" "${SPDK_RPC_SOCKET}"
  append_if_set cpcs_cmd "--bootstrap-subsystem-nqn" "${BOOTSTRAP_SUBSYSTEM_NQN}"
  if [[ "${CPCS_BOOTSTRAP_CHECK}" == "1" ]]; then
    cpcs_cmd+=("--cpcs-bootstrap-check")
  fi
  if [[ "${CPCS_BOOTSTRAP_INSTALL_BUILTINS}" == "1" ]]; then
    cpcs_cmd+=("--cpcs-bootstrap-install-builtins")
  fi
  if [[ "${CPCS_BOOTSTRAP_LIST_PROGRAMS}" == "1" ]]; then
    cpcs_cmd+=("--cpcs-bootstrap-list-programs")
  fi
  if [[ "${CPCS_BOOTSTRAP_LIST_MRS}" == "1" ]]; then
    cpcs_cmd+=("--cpcs-bootstrap-list-mrs")
  fi
  append_if_set cpcs_cmd "--cpcs-required-rpc-methods" "${CPCS_REQUIRED_RPC_METHODS}"
  append_if_set cpcs_cmd "--spdk-nvme-passthru" "${SPDK_NVME_PASSTHRU}"
  append_if_set cpcs_cmd "--trtype" "${TRTYPE}"
  append_if_set cpcs_cmd "--traddr" "${TRADDR}"
  append_if_set cpcs_cmd "--trsvcid" "${TRSVCID}"
  append_if_set cpcs_cmd "--subnqn" "${SUBNQN}"
  append_if_set cpcs_cmd "--hostnqn" "${HOSTNQN}"
  append_if_set cpcs_cmd "--src-addr" "${SRC_ADDR}"
  append_if_set cpcs_cmd "--src-svcid" "${SRC_SVCID}"
  append_if_set cpcs_cmd "--passthru-lcores" "${PASSTHRU_LCORES}"
  append_if_set cpcs_cmd "--dataset-nsid" "${DATASET_NSID}"
  append_if_set cpcs_cmd "--slm-nsid" "${SLM_NSID}"
  append_if_set cpcs_cmd "--cpcs-nsid" "${CPCS_NSID}"
  append_if_set cpcs_cmd "--cpcs-program-pind" "${CPCS_PROGRAM_PIND}"
  append_if_set cpcs_cmd "--cpcs-program-pind-pack-store" "${CPCS_PROGRAM_PIND_PACK_STORE}"
  append_if_set cpcs_cmd "--cpcs-program-pind-unpack-load" "${CPCS_PROGRAM_PIND_UNPACK_LOAD}"
  append_if_set cpcs_cmd "--cpcs-program-pind-layout-repack" "${CPCS_PROGRAM_PIND_LAYOUT_REPACK}"
  append_if_set cpcs_cmd "--cpcs-program-pind-block-select" "${CPCS_PROGRAM_PIND_BLOCK_SELECT}"
  append_if_set cpcs_cmd "--cpcs-program-pind-prefix-lookup" "${CPCS_PROGRAM_PIND_PREFIX_LOOKUP}"
  append_if_set cpcs_cmd "--cpcs-program-pind-batch-read" "${CPCS_PROGRAM_PIND_BATCH_READ}"
  append_if_set cpcs_cmd "--cpcs-rsid" "${CPCS_RSID}"
  append_if_set cpcs_cmd "--cpcs-rsid-pack-store" "${CPCS_RSID_PACK_STORE}"
  append_if_set cpcs_cmd "--cpcs-rsid-unpack-load" "${CPCS_RSID_UNPACK_LOAD}"
  append_if_set cpcs_cmd "--cpcs-rsid-layout-repack" "${CPCS_RSID_LAYOUT_REPACK}"
  append_if_set cpcs_cmd "--cpcs-rsid-block-select" "${CPCS_RSID_BLOCK_SELECT}"
  append_if_set cpcs_cmd "--cpcs-rsid-prefix-lookup" "${CPCS_RSID_PREFIX_LOOKUP}"
  append_if_set cpcs_cmd "--cpcs-rsid-batch-read" "${CPCS_RSID_BATCH_READ}"
  if [[ "${CPCS_AUTO_CREATE_MRS}" == "1" ]]; then
    cpcs_cmd+=("--cpcs-auto-create-mrs")
  fi
  append_if_set cpcs_cmd "--cpcs-mrs-ranges" "${CPCS_MRS_RANGES}"
  append_if_set cpcs_cmd "--cpcs-mrs-default-length-bytes" "${CPCS_MRS_DEFAULT_LENGTH_BYTES}"
  append_if_set cpcs_cmd "--cpcs-mrs-align-bytes" "${CPCS_MRS_ALIGN_BYTES}"
  append_if_set cpcs_cmd "--cpcs-mrs-align-mode" "${CPCS_MRS_ALIGN_MODE}"
  append_if_set cpcs_cmd "--cpcs-load-program-path" "${CPCS_LOAD_PROGRAM_PATH}"
  append_if_set cpcs_cmd "--cpcs-load-program-pind" "${CPCS_LOAD_PROGRAM_PIND}"
  if [[ "${CPCS_LOAD_PROGRAM_SET_DEFAULT_PIND}" == "1" ]]; then
    cpcs_cmd+=("--cpcs-load-program-set-default-pind")
  fi
  append_if_set cpcs_cmd "--cpcs-load-program-chunk-bytes" "${CPCS_LOAD_PROGRAM_CHUNK_BYTES}"
  append_if_set cpcs_cmd "--cpcs-load-program-ptype" "${CPCS_LOAD_PROGRAM_PTYPE}"
  append_if_set cpcs_cmd "--cpcs-load-program-pit" "${CPCS_LOAD_PROGRAM_PIT}"
  append_if_set cpcs_cmd "--cpcs-load-program-puid" "${CPCS_LOAD_PROGRAM_PUID}"
  if [[ "${CPCS_ACTIVATE_LOADED_PROGRAM}" == "1" ]]; then
    cpcs_cmd+=("--cpcs-activate-loaded-program")
  fi
  append_if_set cpcs_cmd "--direct-probe-offset" "${DIRECT_PROBE_OFFSET}"
  append_if_set cpcs_cmd "--direct-probe-length" "${DIRECT_PROBE_LENGTH}"
  append_if_set cpcs_cmd "--direct-probe-lba-bytes" "${DIRECT_PROBE_LBA_BYTES}"
  append_if_set cpcs_cmd "--cpcs-slm-rw-lba-bytes" "${CPCS_SLM_RW_LBA_BYTES}"
  append_if_set cpcs_cmd "--cpcs-slm-read-address-mode" "${CPCS_SLM_READ_ADDRESS_MODE}"
  append_if_set cpcs_cmd "--cpcs-slm-write-address-mode" "${CPCS_SLM_WRITE_ADDRESS_MODE}"

  run_cmd "${baseline_label}" "${baseline_cmd[@]}"
  run_cmd "${cpcs_label}" "${cpcs_cmd[@]}"

  printf "%s,%s,%s,%s,%s\n" "${run_id}" "${baseline_json}" "${cpcs_json}" "${baseline_system_metrics_dir}" "${cpcs_system_metrics_dir}" >> "${MANIFEST_PATH}"
done

if [[ "${EXECUTE}" -eq 1 ]]; then
  echo "Execution finished."
else
  echo "Dry-run finished (no benchmark commands executed)."
fi
echo "Manifest: ${MANIFEST_PATH}"
