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
mkdir -p "${DATA_DIR}/open-webui"

exec srun --jobid="$JOBID" --overlap \
  --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=4G --gres=none \
  --container-image="${WEBUI_IMAGE}" \
  --container-mounts="${DATA_DIR}/open-webui:/app/backend/data" \
  --container-workdir=/app/backend \
  bash -c "HOST=127.0.0.1 PORT=${WEBUI_PORT} OLLAMA_BASE_URL=http://127.0.0.1:${OLLAMA_PORT} WEBUI_SECRET_KEY_FILE=/app/backend/data/.webui_secret_key exec bash start.sh"
