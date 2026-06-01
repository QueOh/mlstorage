#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

RESULTS_DIR="${RESULTS_DIR:-${ROOT_DIR}/results}"
PACK_DIR="${PACK_DIR:-}"

RUN_PREFIX="${RUN_PREFIX:-storage_only}"
TRIALS="${TRIALS:-3}"

BASELINE_DIR="${BASELINE_DIR:-}"
CPCS_DIR="${CPCS_DIR:-}"
MANIFEST_PATH="${MANIFEST_PATH:-}"
COMPARISON_CSV="${COMPARISON_CSV:-}"
COMPARISON_MD="${COMPARISON_MD:-}"

MATRIX_SCRIPT="${MATRIX_SCRIPT:-${ROOT_DIR}/scripts/run_cpcs_demo_matrix.sh}"
COMPARE_SCRIPT="${COMPARE_SCRIPT:-${ROOT_DIR}/scripts/compare_cpcs_results.py}"

STRICT_COMPARE=0

COLLECT_SYSTEM_METRICS=0
SYSTEM_METRICS_DIR="${SYSTEM_METRICS_DIR:-}"
SYSTEM_METRICS_NET_IFACE="${SYSTEM_METRICS_NET_IFACE:-}"
SYSTEM_METRICS_NVME_DEVICE="${SYSTEM_METRICS_NVME_DEVICE:-}"
SYSTEM_METRICS_PIDSTAT=0
SYSTEM_METRICS_IOSTAT=0
SYSTEM_METRICS_SAMPLE_SEC="${SYSTEM_METRICS_SAMPLE_SEC:-1}"

FORWARD_ARGS=()

usage() {
  cat <<'USAGE'
Usage:
  prepare_cpcs_validation_pack.sh [options] [-- matrix-args...]

Description:
  Generates a deferred validation command pack (scripts + checklist) without
  executing benchmark runs. The generated scripts can be reviewed and run later.

Options:
  --pack-dir PATH                Output directory for generated pack.
  --results-dir PATH             Base results directory.
  --baseline-dir PATH            Baseline results directory.
  --cpcs-dir PATH                CPCS results directory.
  --manifest-path PATH           Matrix manifest CSV path.
  --comparison-csv PATH          Comparison CSV output path.
  --comparison-md PATH           Comparison Markdown output path.
  --run-prefix NAME              Matrix run prefix (default: storage_only).
  --trials N                     Matrix trial count (default: 3).
  --matrix-script PATH           Matrix runner script path.
  --compare-script PATH          Comparison script path.
  --strict-compare               Add --strict to comparison command.
  --collect-system-metrics       Add system-metrics flags to generated matrix command.
  --system-metrics-dir PATH      System metrics artifact directory.
  --system-metrics-net-iface IFACE
                                 Network interface name for detailed snapshots.
  --system-metrics-nvme-device DEV
                                 Device selector passed to iostat sampler.
  --system-metrics-pidstat       Enable pidstat sampler in generated matrix command.
  --system-metrics-iostat        Enable iostat sampler in generated matrix command.
  --system-metrics-sample-sec N  Sampler interval for pidstat/iostat.
  --matrix-arg ARG               Append one raw argument to generated matrix command.
  -h, --help                     Show this help.

Notes:
  - Unknown options are forwarded to the generated matrix command.
  - Arguments after '--' are forwarded verbatim to the matrix command.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pack-dir) PACK_DIR="$2"; shift 2 ;;
    --results-dir) RESULTS_DIR="$2"; shift 2 ;;
    --baseline-dir) BASELINE_DIR="$2"; shift 2 ;;
    --cpcs-dir) CPCS_DIR="$2"; shift 2 ;;
    --manifest-path) MANIFEST_PATH="$2"; shift 2 ;;
    --comparison-csv) COMPARISON_CSV="$2"; shift 2 ;;
    --comparison-md) COMPARISON_MD="$2"; shift 2 ;;
    --run-prefix) RUN_PREFIX="$2"; shift 2 ;;
    --trials) TRIALS="$2"; shift 2 ;;
    --matrix-script) MATRIX_SCRIPT="$2"; shift 2 ;;
    --compare-script) COMPARE_SCRIPT="$2"; shift 2 ;;
    --strict-compare) STRICT_COMPARE=1; shift ;;
    --collect-system-metrics) COLLECT_SYSTEM_METRICS=1; shift ;;
    --system-metrics-dir) SYSTEM_METRICS_DIR="$2"; shift 2 ;;
    --system-metrics-net-iface) SYSTEM_METRICS_NET_IFACE="$2"; shift 2 ;;
    --system-metrics-nvme-device) SYSTEM_METRICS_NVME_DEVICE="$2"; shift 2 ;;
    --system-metrics-pidstat) SYSTEM_METRICS_PIDSTAT=1; shift ;;
    --system-metrics-iostat) SYSTEM_METRICS_IOSTAT=1; shift ;;
    --system-metrics-sample-sec) SYSTEM_METRICS_SAMPLE_SEC="$2"; shift 2 ;;
    --matrix-arg) FORWARD_ARGS+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        FORWARD_ARGS+=("$1")
        shift
      done
      break
      ;;
    *)
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "${BASELINE_DIR}" ]]; then
  BASELINE_DIR="${RESULTS_DIR}/baseline"
fi
if [[ -z "${CPCS_DIR}" ]]; then
  CPCS_DIR="${RESULTS_DIR}/cpcs"
fi
if [[ -z "${MANIFEST_PATH}" ]]; then
  MANIFEST_PATH="${RESULTS_DIR}/cpcs/run_manifest.csv"
fi
if [[ -z "${COMPARISON_CSV}" ]]; then
  COMPARISON_CSV="${RESULTS_DIR}/cpcs_comparison.csv"
fi
if [[ -z "${COMPARISON_MD}" ]]; then
  COMPARISON_MD="${RESULTS_DIR}/cpcs_comparison.md"
fi
if [[ -z "${SYSTEM_METRICS_DIR}" ]]; then
  SYSTEM_METRICS_DIR="${RESULTS_DIR}/system_metrics"
fi
if [[ -z "${PACK_DIR}" ]]; then
  PACK_DIR="${RESULTS_DIR}/cpcs_validation_pack/$(date +%Y%m%d_%H%M%S)"
fi

if [[ ! -f "${MATRIX_SCRIPT}" ]]; then
  echo "Matrix script not found: ${MATRIX_SCRIPT}" >&2
  exit 1
fi
if [[ ! -f "${COMPARE_SCRIPT}" ]]; then
  echo "Comparison script not found: ${COMPARE_SCRIPT}" >&2
  exit 1
fi

mkdir -p "${PACK_DIR}"

quote_cmd() {
  local out=""
  local q=""
  for arg in "$@"; do
    printf -v q '%q' "$arg"
    out+="${out:+ }${q}"
  done
  echo "${out}"
}

MATRIX_CMD=(
  "${MATRIX_SCRIPT}"
  "--execute"
  "--run-prefix" "${RUN_PREFIX}"
  "--trials" "${TRIALS}"
)

if [[ "${COLLECT_SYSTEM_METRICS}" == "1" ]]; then
  MATRIX_CMD+=("--collect-system-metrics")
  MATRIX_CMD+=("--system-metrics-dir" "${SYSTEM_METRICS_DIR}")
  MATRIX_CMD+=("--system-metrics-sample-sec" "${SYSTEM_METRICS_SAMPLE_SEC}")
  if [[ -n "${SYSTEM_METRICS_NET_IFACE}" ]]; then
    MATRIX_CMD+=("--system-metrics-net-iface" "${SYSTEM_METRICS_NET_IFACE}")
  fi
  if [[ -n "${SYSTEM_METRICS_NVME_DEVICE}" ]]; then
    MATRIX_CMD+=("--system-metrics-nvme-device" "${SYSTEM_METRICS_NVME_DEVICE}")
  fi
  if [[ "${SYSTEM_METRICS_PIDSTAT}" == "1" ]]; then
    MATRIX_CMD+=("--system-metrics-pidstat")
  fi
  if [[ "${SYSTEM_METRICS_IOSTAT}" == "1" ]]; then
    MATRIX_CMD+=("--system-metrics-iostat")
  fi
fi

if [[ ${#FORWARD_ARGS[@]} -gt 0 ]]; then
  MATRIX_CMD+=("${FORWARD_ARGS[@]}")
fi

COMPARE_CMD=(
  "${COMPARE_SCRIPT}"
  "--manifest" "${MANIFEST_PATH}"
  "--output-csv" "${COMPARISON_CSV}"
  "--output-md" "${COMPARISON_MD}"
)
if [[ "${STRICT_COMPARE}" == "1" ]]; then
  COMPARE_CMD+=("--strict")
fi

MATRIX_CMD_STR="$(quote_cmd "${MATRIX_CMD[@]}")"
COMPARE_CMD_STR="$(quote_cmd "${COMPARE_CMD[@]}")"
FORWARD_ARGS_STR=""
if [[ ${#FORWARD_ARGS[@]} -gt 0 ]]; then
  FORWARD_ARGS_STR="$(quote_cmd "${FORWARD_ARGS[@]}")"
fi

Q_ROOT_DIR="$(printf '%q' "${ROOT_DIR}")"
Q_RESULTS_DIR="$(printf '%q' "${RESULTS_DIR}")"
Q_BASELINE_DIR="$(printf '%q' "${BASELINE_DIR}")"
Q_CPCS_DIR="$(printf '%q' "${CPCS_DIR}")"
Q_MANIFEST_PATH="$(printf '%q' "${MANIFEST_PATH}")"
Q_COMPARISON_CSV="$(printf '%q' "${COMPARISON_CSV}")"
Q_COMPARISON_MD="$(printf '%q' "${COMPARISON_MD}")"
Q_SYSTEM_METRICS_DIR="$(printf '%q' "${SYSTEM_METRICS_DIR}")"

cat > "${PACK_DIR}/10_run_matrix.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail

cd ${Q_ROOT_DIR}

export RESULTS_DIR=${Q_RESULTS_DIR}
export BASELINE_DIR=${Q_BASELINE_DIR}
export CPCS_DIR=${Q_CPCS_DIR}
export MANIFEST_PATH=${Q_MANIFEST_PATH}
export SYSTEM_METRICS_DIR=${Q_SYSTEM_METRICS_DIR}

${MATRIX_CMD_STR}
EOF

cat > "${PACK_DIR}/15_run_real_target_matrix.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-${SCRIPT_DIR}/50_real_target_env.sh}"
TEMPLATE_FILE="${SCRIPT_DIR}/50_real_target_env.template"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing real-target env file: ${ENV_FILE}" >&2
  if [[ -f "${TEMPLATE_FILE}" ]]; then
    echo "Create it from template first:" >&2
    echo "  cp \"${TEMPLATE_FILE}\" \"${SCRIPT_DIR}/50_real_target_env.sh\"" >&2
  fi
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

required_vars=(
  SPDK_NVME_PASSTHRU
  TRTYPE
  TRADDR
  TRSVCID
  SUBNQN
  HOSTNQN
  DATASET_NSID
  SLM_NSID
  CPCS_NSID
  CACHE_DIR_BASELINE
  CACHE_DIR_CPCS
)

missing=()
for key in "${required_vars[@]}"; do
  value="${!key-}"
  if [[ -z "${value}" || "${value}" == REPLACE_ME* ]]; then
    missing+=("${key}")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Real-target env file has unset placeholder fields:" >&2
  for key in "${missing[@]}"; do
    echo "  - ${key}" >&2
  done
  exit 1
fi

"${SCRIPT_DIR}/10_run_matrix.sh"
EOF

cat > "${PACK_DIR}/20_compare_results.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail

cd ${Q_ROOT_DIR}
${COMPARE_CMD_STR}
EOF

cat > "${PACK_DIR}/30_run_all.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${SCRIPT_DIR}/10_run_matrix.sh"
"${SCRIPT_DIR}/20_compare_results.sh"
EOF

cat > "${PACK_DIR}/50_real_target_env.template" <<'EOF'
# Copy this file to `50_real_target_env.sh` and replace placeholder values.
# This file is sourced by `15_run_real_target_matrix.sh`.

# Required transport + namespace coordinates
SPDK_NVME_PASSTHRU=REPLACE_ME_ABS_PATH_TO_SPDK_NVME_PASSTHRU
TRTYPE=TCP
TRADDR=REPLACE_ME_TARGET_IP_OR_HOSTNAME
TRSVCID=4420
SUBNQN=REPLACE_ME_SUBSYSTEM_NQN
HOSTNQN=REPLACE_ME_HOST_NQN
DATASET_NSID=1
SLM_NSID=100
CPCS_NSID=200

# Required cache directories on validation host
CACHE_DIR_BASELINE=/mnt/kv-nvmeof-baseline
CACHE_DIR_CPCS=/mnt/kv-nvmeof-cpcs

# Optional inventory/bootstrap hooks
SPDK_INVENTORY=
SPDK_RPC_SCRIPT=
SPDK_RPC_PYTHON=python3
SPDK_RPC_SOCKET=
BOOTSTRAP_SUBSYSTEM_NQN=
CPCS_BOOTSTRAP_CHECK=0
CPCS_BOOTSTRAP_INSTALL_BUILTINS=0
CPCS_BOOTSTRAP_LIST_PROGRAMS=0
CPCS_BOOTSTRAP_LIST_MRS=0
CPCS_REQUIRED_RPC_METHODS=

# Optional CPCS routing/program defaults
CPCS_MODE=noop
CPCS_CLIENT=spdk_passthru
CPCS_STORAGE_MODE=arena
CPCS_ARENA_PATH=/mnt/kv-nvmeof-cpcs/cpcs_arena.bin
CPCS_INDEX_PATH=/mnt/kv-nvmeof-cpcs/cpcs_index.json
CPCS_PROGRAM_PIND=0
CPCS_RSID=1
CPCS_AUTO_CREATE_MRS=0
CPCS_MRS_RANGES=

# Optional system-metrics hints for matrix runner
SYSTEM_METRICS_NET_IFACE=
SYSTEM_METRICS_NVME_DEVICE=
EOF

cat > "${PACK_DIR}/00_README.md" <<EOF
# CPCS Deferred Validation Pack

Generated: $(date '+%Y-%m-%dT%H:%M:%S%z')

This pack prepares execution commands only. Nothing is executed during pack generation.

## Files

- \`10_run_matrix.sh\`: executes baseline+CPCS matrix runs.
- \`15_run_real_target_matrix.sh\`: runs matrix with required real-target env validation.
- \`20_compare_results.sh\`: builds comparison CSV/Markdown.
- \`30_run_all.sh\`: runs matrix then comparison in sequence.
- \`40_reporting_checklist.md\`: post-run reporting checklist.
- \`50_real_target_env.template\`: template env file for real-target run settings.
- \`60_milestone3_validation_queue.md\`: Milestone 3 command queue with run gates.
- \`70_milestone4_validation_queue.md\`: Milestone 4 transform validation command queue.
- \`80_milestone5_demo_report_queue.md\`: Milestone 5 report assembly command queue.

## Key Paths

- Results dir: \`${RESULTS_DIR}\`
- Baseline dir: \`${BASELINE_DIR}\`
- CPCS dir: \`${CPCS_DIR}\`
- Manifest: \`${MANIFEST_PATH}\`
- Comparison CSV: \`${COMPARISON_CSV}\`
- Comparison MD: \`${COMPARISON_MD}\`
- System metrics dir: \`${SYSTEM_METRICS_DIR}\`

## Generated Commands

\`\`\`bash
${MATRIX_CMD_STR}
${COMPARE_CMD_STR}
\`\`\`

## Forwarded Matrix Args

\`\`\`bash
${FORWARD_ARGS_STR}
\`\`\`

## Real-Target Template Flow

1. Copy and edit env template:
   \`cp 50_real_target_env.template 50_real_target_env.sh\`
2. Run matrix with real-target env validation:
   \`./15_run_real_target_matrix.sh\`
3. Build comparison report:
   \`./20_compare_results.sh\`
EOF

cat > "${PACK_DIR}/40_reporting_checklist.md" <<'EOF'
# Reporting Checklist

- [ ] Baseline and CPCS matrix runs completed without unexpected command failures.
- [ ] `run_manifest.csv` contains all expected trials.
- [ ] Comparison CSV and Markdown generated successfully.
- [ ] `demo_claim_met` / `demo_claim_reason` reviewed per trial.
- [ ] Fabric and host CPU deltas reviewed for metric availability gaps.
- [ ] Media-byte delta fields reviewed for estimate-vs-telemetry interpretation.
- [ ] If system metrics were collected: before/after snapshots and sampler logs archived.
- [ ] Limitations and uncertain findings documented in final summary.
EOF

cat > "${PACK_DIR}/60_milestone3_validation_queue.md" <<EOF
# Milestone 3 Validation Queue (Deferred Execution)

This queue maps Milestone 3 ("Real CPCS no-op path") to executable commands.
Do not run until validation execution is explicitly approved.

## Preconditions

- [ ] Copy and fill real-target env file:
  - \`cp 50_real_target_env.template 50_real_target_env.sh\`
- [ ] Confirm required fields are replaced in \`50_real_target_env.sh\`:
  - \`SPDK_NVME_PASSTHRU\`, \`TRTYPE\`, \`TRADDR\`, \`TRSVCID\`, \`SUBNQN\`, \`HOSTNQN\`
  - \`DATASET_NSID\`, \`SLM_NSID\`, \`CPCS_NSID\`
  - \`CACHE_DIR_BASELINE\`, \`CACHE_DIR_CPCS\`

## Queue

1. [ ] Execute real-target matrix run gate + run:
   \`\`\`bash
   ./15_run_real_target_matrix.sh
   \`\`\`

2. [ ] Generate comparison outputs:
   \`\`\`bash
   ./20_compare_results.sh
   \`\`\`

3. [ ] Confirm required artifacts exist:
   - \`${MANIFEST_PATH}\`
   - \`${COMPARISON_CSV}\`
   - \`${COMPARISON_MD}\`

## Milestone 3 Mapping

- [ ] Real \`noop\` CPCS command executes.
- [ ] CPCS command latency is measured.
- [ ] Real no-op benchmark run completes.
- [ ] No-op overhead compared with baseline.
EOF

cat > "${PACK_DIR}/70_milestone4_validation_queue.md" <<EOF
# Milestone 4 Validation Queue (Deferred Execution)

This queue maps Milestone 4 ("Real CPCS transform path") to staged commands.
Do not run until validation execution is explicitly approved.

## Preconditions

- [ ] Milestone 3 queue completed (\`60_milestone3_validation_queue.md\`).
- [ ] Real-target env file prepared:
  - \`cp 50_real_target_env.template 50_real_target_env.sh\`
  - required fields replaced.

## Queue

1. [ ] Generate lossless transform validation pack:
   \`\`\`bash
   ${ROOT_DIR}/scripts/prepare_cpcs_validation_pack.sh \
     --pack-dir ${RESULTS_DIR}/cpcs_validation_pack/m4_lossless \
     --run-prefix m4_lossless \
     --trials ${TRIALS} \
     --collect-system-metrics \
     --strict-compare \
     -- --cpcs-mode lossless_compress --cpcs-client spdk_passthru --cpcs-storage-mode arena
   \`\`\`

2. [ ] Execute lossless transform run + comparison:
   \`\`\`bash
   cd ${RESULTS_DIR}/cpcs_validation_pack/m4_lossless
   cp 50_real_target_env.template 50_real_target_env.sh
   ./15_run_real_target_matrix.sh
   ./20_compare_results.sh
   \`\`\`

3. [ ] Generate int8 transform validation pack:
   \`\`\`bash
   ${ROOT_DIR}/scripts/prepare_cpcs_validation_pack.sh \
     --pack-dir ${RESULTS_DIR}/cpcs_validation_pack/m4_int8 \
     --run-prefix m4_int8 \
     --trials ${TRIALS} \
     --collect-system-metrics \
     --strict-compare \
     -- --cpcs-mode int8_quantize --cpcs-client spdk_passthru --cpcs-storage-mode arena
   \`\`\`

4. [ ] Execute int8 transform run + comparison:
   \`\`\`bash
   cd ${RESULTS_DIR}/cpcs_validation_pack/m4_int8
   cp 50_real_target_env.template 50_real_target_env.sh
   ./15_run_real_target_matrix.sh
   ./20_compare_results.sh
   \`\`\`

## Milestone 4 Mapping

- [ ] At least one transform mode works end-to-end.
- [ ] Lossless correctness checks pass or quantization error is documented.
- [ ] Repeated trials complete for transform mode(s).
- [ ] CPCS metrics and comparison artifacts generated for transform mode(s).
EOF

cat > "${PACK_DIR}/80_milestone5_demo_report_queue.md" <<EOF
# Milestone 5 Demo Report Queue (Deferred Execution)

This queue maps Milestone 5 ("Demo claim") to final report assembly steps.
Do not run until validation execution is explicitly approved.

## Preconditions

- [ ] Milestone 3 queue completed.
- [ ] Milestone 4 queue completed.
- [ ] Comparison outputs available from no-op and transform runs.

## Queue

1. [ ] Gather comparison artifacts:
   - \`${COMPARISON_CSV}\`
   - \`${COMPARISON_MD}\`
   - transform-mode comparison outputs (for example under \`results/cpcs_validation_pack/m4_*/\`).

2. [ ] Confirm minimum trial evidence:
   - at least 3 baseline trials
   - at least 3 CPCS trials
   - median-based interpretation (not best-run only)

3. [ ] Evaluate claim gates:
   - \`demo_claim_met\` / \`demo_claim_reason\` rows
   - host CPU / fabric / media / latency / throughput deltas
   - command failure counters and correctness status fields

4. [ ] Publish final summary:
   - choose supported claim(s)
   - list limitations and uncertain findings
   - include target/resource context and telemetry caveats

## Milestone 5 Mapping

- [ ] Demo claim selected and supported by metrics.
- [ ] Limitations documented.
- [ ] Final comparison/report artifacts archived.
EOF

chmod +x "${PACK_DIR}/10_run_matrix.sh" "${PACK_DIR}/15_run_real_target_matrix.sh" "${PACK_DIR}/20_compare_results.sh" "${PACK_DIR}/30_run_all.sh"

echo "Generated deferred validation pack:"
echo "  ${PACK_DIR}"
echo "Run order:"
echo "  1) ${PACK_DIR}/10_run_matrix.sh"
echo "  2) ${PACK_DIR}/20_compare_results.sh"
echo "Real-target flow:"
echo "  cp ${PACK_DIR}/50_real_target_env.template ${PACK_DIR}/50_real_target_env.sh"
echo "  ${PACK_DIR}/15_run_real_target_matrix.sh"
echo "Or:"
echo "  ${PACK_DIR}/30_run_all.sh"
