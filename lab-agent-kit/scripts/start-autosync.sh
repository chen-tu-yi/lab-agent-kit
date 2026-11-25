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

export AUTOSYNC_EVENTS_LOG="${DATA_DIR}/harnessmith/autosync_events.jsonl"

exec srun --jobid="$JOBID" --overlap \
  --nodes=1 --ntasks=1 --cpus-per-task=1 --mem=256M --gres=none \
  python3 "$KIT_DIR/openwebui-adapter/autosync_server.py"
