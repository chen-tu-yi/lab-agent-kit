#!/bin/bash
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$KIT_DIR/.env" ]; then
  echo "Error: 找不到 $KIT_DIR/.env，請先執行: bash init.sh" >&2
  exit 1
fi

set -a
source "$KIT_DIR/.env"
set +a

JOBID="${1:-$(squeue -u "$USER" -t R -h -o "%i" | head -n 1)}"
if [ -z "$JOBID" ]; then
  echo "Error: 找不到正在執行的 Slurm Job，請先執行: sbatch alloc.sbatch" >&2
  exit 1
fi

JOB_NODE="$(squeue -j "$JOBID" -h -o "%N" | head -n 1)"
CUR_NODE="$(hostname -s)"
if [ -n "$JOB_NODE" ] && [ "$JOB_NODE" != "$CUR_NODE" ]; then
  echo "[WARN] Job $JOBID 跑在節點 $JOB_NODE，但你目前在 $CUR_NODE。" >&2
  echo "       請先執行 'ssh $JOB_NODE'，再到該節點執行: $KIT_DIR/start-all.sh $JOBID" >&2
  exit 1
fi

mkdir -p "$KIT_DIR/logs"
echo "使用 Slurm Job: $JOBID (節點: $CUR_NODE)"

ensure_service() {
  local name="$1" port="$2" script="$3" log="$4"
  if ss -tln | grep -q "127.0.0.1:${port} "; then
    echo "  [OK]    $name (127.0.0.1:${port}) 已在運行中"
  else
    echo "  [START] 啟動 $name (127.0.0.1:${port})..."
    tmux kill-session -t "$name" 2>/dev/null || true
    tmux new-session -d -s "$name" "bash '$script' '$JOBID' 2>&1 | tee -a '$log'"
  fi
}

ensure_service ollama      "$OLLAMA_PORT"   "$KIT_DIR/scripts/start-ollama.sh"      "$KIT_DIR/logs/ollama-${JOBID}.log"
ensure_service open-webui  "$WEBUI_PORT"    "$KIT_DIR/scripts/start-open-webui.sh"  "$KIT_DIR/logs/open-webui-${JOBID}.log"
ensure_service harnessmith "$HARNESS_PORT"  "$KIT_DIR/scripts/start-harnessmith.sh" "$KIT_DIR/logs/harnessmith-${JOBID}.log"
ensure_service autosync    "$AUTOSYNC_PORT" "$KIT_DIR/scripts/start-autosync.sh"    "$KIT_DIR/logs/autosync-${JOBID}.log"

echo "等待服務就緒..."
sleep 6
for pair in "ollama:$OLLAMA_PORT" "open-webui:$WEBUI_PORT" "harnessmith:$HARNESS_PORT" "autosync:$AUTOSYNC_PORT"; do
  name="${pair%%:*}"
  p="${pair##*:}"
  if ss -tln | grep -q "127.0.0.1:${p} "; then
    echo "  [OK]   $name -> 127.0.0.1:${p} LISTENING"
  else
    echo "  [WAIT] $name -> 127.0.0.1:${p} 啟動中（可用 tmux attach -t $name 或看 logs/ 確認）"
  fi
done

echo ""
echo "在本機電腦開 SSH Tunnel 即可瀏覽你自己的介面："
echo "  ssh -L ${WEBUI_PORT}:127.0.0.1:${WEBUI_PORT} -L ${HARNESS_PORT}:127.0.0.1:${HARNESS_PORT} <你的帳號>@<Server IP> -p <SSH Port>"
echo "  Open WebUI:  http://127.0.0.1:${WEBUI_PORT}"
echo "  Harnessmith: http://127.0.0.1:${HARNESS_PORT}"
