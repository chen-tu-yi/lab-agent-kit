#!/bin/bash
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
source "$KIT_DIR/.env"
set +a

JOBID="${1:-$(squeue -u "$USER" -t R -h -o "%i" | head -n 1)}"
if [ -z "$JOBID" ]; then
  echo "Error: no running Slurm job found." >&2
  exit 1
fi

umask 077
mkdir -p "${HARNESS2_RUNS_DIR}" "$(dirname "${HARNESS2_CONNECTIONS}")"

cd "$KIT_DIR/harnessmith"
exec srun --jobid="$JOBID" --overlap \
  --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=2G --gres=none \
  "$KIT_DIR/harnessmith/target/release/harness2" serve --host 127.0.0.1 --port "${HARNESS_PORT}"
