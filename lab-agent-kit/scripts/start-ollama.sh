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
mkdir -p "${DATA_DIR}/ollama/models"

exec srun --jobid="$JOBID" --overlap \
  --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=10G \
  --gres=gpu:1 \
  --container-image="${OLLAMA_IMAGE}" \
  --container-mounts="${DATA_DIR}/ollama:/ollama" \
  --container-workdir=/tmp \
  --no-container-entrypoint \
  bash -c "export HOME=/ollama OLLAMA_MODELS=/ollama/models OLLAMA_HOST=127.0.0.1:${OLLAMA_PORT} OLLAMA_MAX_LOADED_MODELS=${OLLAMA_MAX_LOADED_MODELS:-1} OLLAMA_FLASH_ATTENTION=${OLLAMA_FLASH_ATTENTION:-1} OLLAMA_KV_CACHE_TYPE=${OLLAMA_KV_CACHE_TYPE:-q8_0}; exec ollama serve"
