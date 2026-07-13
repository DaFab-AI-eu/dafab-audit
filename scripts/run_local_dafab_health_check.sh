#!/usr/bin/env bash
set -euo pipefail

# Default: fast read-only audit. Use --mode sample for one real download per
# scope, or --mode full for exhaustive hashes/downloads.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${REPO_DIR}/.venv/bin/python"
[[ -x "${PYTHON}" ]] || PYTHON="python3"

CONFIG_DIR="${DAFAB_CLIENT_CONFIG_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/dafab-rucio-client}"
SECRET_DIR="${CONFIG_DIR}/audit"
HEALTH_ENV="${DAFAB_HEALTH_ENV:-${SECRET_DIR}/rucio_health_audit.env}"
DEFAULT_RSE_ACCOUNT="${SECRET_DIR}/rucio_health_rse_account.json"
RSE_ACCOUNT_FILE="${DAFAB_RSE_ACCOUNT_FILE:-${DEFAULT_RSE_ACCOUNT}}"
POSIX_ROOT="${DAFAB_RUCIO_POSIX_ROOT:-/mnt/tier2/project/p200528/rucio-data}"
OUTPUT="${DAFAB_HEALTH_OUTPUT:-${REPO_DIR}/untracked/reports/dafab_health_check_$(date -u +%Y%m%dT%H%M%SZ).json}"
MODE="${DAFAB_HEALTH_MODE:-fast}"
TIMEOUT="${DAFAB_HEALTH_TIMEOUT:-20}"

if [[ -r "${HEALTH_ENV}" ]]; then
  set -a
  source "${HEALTH_ENV}"
  set +a
fi

[[ -n "${DAFAB_AUDIT_DB_DSN:-}" ]] || {
  echo "Set DAFAB_AUDIT_DB_DSN, or create ${HEALTH_ENV} with that variable." >&2
  exit 2
}
[[ -r "${RSE_ACCOUNT_FILE}" ]] || {
  echo "Missing RSE account file: ${RSE_ACCOUNT_FILE}" >&2
  exit 2
}
[[ -d "${POSIX_ROOT}/dafab" ]] || {
  echo "POSIX storage is not mounted at ${POSIX_ROOT}." >&2
  echo "Mount the Rucio POSIX storage before running this check." >&2
  exit 2
}

"${PYTHON}" -m dafab_audit.health \
  --mode "${MODE}" \
  --bucket-scan full-bucket \
  --lan-policy require \
  --rse-account-file "${RSE_ACCOUNT_FILE}" \
  --profile-dir "${CONFIG_DIR}/profiles" \
  --timeout "${TIMEOUT}" \
  --output "${OUTPUT}" \
  "$@"

echo "Health report: ${OUTPUT}"
