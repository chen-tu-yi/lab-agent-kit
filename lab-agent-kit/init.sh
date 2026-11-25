#!/bin/bash
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$KIT_DIR"

HARNESS_REPO="https://github.com/surenjanath/harnessmith.git"
HARNESS_COMMIT="4ace72f"

echo "=== [1/5] 產生個人專屬 .env（自動錯開 Port 避免同節點衝突） ==="
mkdir -p "$KIT_DIR/images" "$KIT_DIR/logs"

if [ -f "$KIT_DIR/.env" ]; then
  echo "  [SKIP] .env 已存在，保留你現有的設定。"
else
  # 依 UID 自動計算專屬 Port 偏移量（確保同節點多人同時運行不撞 Port）
  OFFSET=$(( ( $(id -u) % 90 + 1 ) * 10 ))
  OLLAMA_PORT=$((11400 + OFFSET))
  WEBUI_PORT=$((13000 + OFFSET))
  HARNESS_PORT=$((18000 + OFFSET))
  AUTOSYNC_PORT=$((18001 + OFFSET))

  HOOK_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  SYNC_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  DEFAULT_DATA_DIR="${DATA_DIR:-$HOME/local-agent-data}"

  umask 077
  cat > "$KIT_DIR/.env" <<EOF
DATA_DIR=${DEFAULT_DATA_DIR}

OLLAMA_PORT=${OLLAMA_PORT}
WEBUI_PORT=${WEBUI_PORT}
HARNESS_PORT=${HARNESS_PORT}
AUTOSYNC_PORT=${AUTOSYNC_PORT}

OPENWEBUI_URL=http://127.0.0.1:${WEBUI_PORT}
HARNESS_URL=http://127.0.0.1:${HARNESS_PORT}
HARNESSMITH_URL=http://127.0.0.1:${HARNESS_PORT}
AUTOSYNC_HOST=127.0.0.1

OLLAMA_IMAGE=${KIT_DIR}/images/ollama.sqsh
WEBUI_IMAGE=${KIT_DIR}/images/open-webui.sqsh

OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0

HARNESS2_RUNS_DIR=${DEFAULT_DATA_DIR}/harnessmith/runs
HARNESS2_CONNECTIONS=${DEFAULT_DATA_DIR}/harnessmith/connections.json
HARNESSMITH_PROJECT=agent-harness-lab

HARNESS2_HOOK_TOKEN=${HOOK_TOKEN}
HARNESS2_AUTOSYNC_TOKEN=${SYNC_TOKEN}
OPENWEBUI_API_KEY=
EOF
  chmod 600 "$KIT_DIR/.env"
  echo "  [OK] 已建立 .env（你的專屬 Ports: Ollama=${OLLAMA_PORT}, WebUI=${WEBUI_PORT}, Harness=${HARNESS_PORT}, AutoSync=${AUTOSYNC_PORT}）"
fi

set -a
source "$KIT_DIR/.env"
set +a

echo "=== [2/5] 建立個人隔離資料目錄（權限 700） ==="
umask 077
mkdir -p \
  "${DATA_DIR}/ollama/models" \
  "${DATA_DIR}/open-webui" \
  "${DATA_DIR}/harnessmith/runs"
chmod 700 "${DATA_DIR}" "${DATA_DIR}/open-webui" "${DATA_DIR}/harnessmith"
echo "  [OK] 資料目錄就緒: ${DATA_DIR}"

echo "=== [3/5] 下載 Ollama Enroot 映像檔 (.sqsh) ==="
if [ -f "${OLLAMA_IMAGE}" ]; then
  echo "  [SKIP] 已存在: ${OLLAMA_IMAGE}"
else
  echo "  [INFO] 開始從 Docker Hub 匯入 ollama:latest（申請 50G 記憶體避免解包 OOM）..."
  mkdir -p "$(dirname "${OLLAMA_IMAGE}")"
  srun --cpus-per-task=4 --mem=50G --gres=none \
    enroot import -o "${OLLAMA_IMAGE}" docker://ollama/ollama:latest
  echo "  [OK] 已建立 ${OLLAMA_IMAGE}"
fi

echo "=== [4/5] 下載 Open WebUI Enroot 映像檔 (.sqsh) ==="
if [[ "${WEBUI_IMAGE}" == docker://* ]]; then
  echo "  [SKIP] 使用遠端映像 URI: ${WEBUI_IMAGE}"
elif [ -f "${WEBUI_IMAGE}" ]; then
  echo "  [SKIP] 已存在: ${WEBUI_IMAGE}"
else
  echo "  [INFO] 開始從 GHCR 匯入 open-webui:slim（申請 50G 記憶體避免首次啟動 OOM）..."
  mkdir -p "$(dirname "${WEBUI_IMAGE}")"
  srun --cpus-per-task=4 --mem=50G --gres=none \
    enroot import -o "${WEBUI_IMAGE}" docker://ghcr.io#open-webui/open-webui:slim
  echo "  [OK] 已建立 ${WEBUI_IMAGE}"
fi

echo "=== [5/5] 下載並編譯 Harnessmith（含 CJK 繁中修復 Patch） ==="
if [ -x "$KIT_DIR/harnessmith/target/release/harness2" ]; then
  echo "  [SKIP] harness2 執行檔已存在。"
else
  if [ ! -d "$KIT_DIR/harnessmith/.git" ]; then
    git clone "$HARNESS_REPO" "$KIT_DIR/harnessmith"
    git -C "$KIT_DIR/harnessmith" checkout "$HARNESS_COMMIT"
  fi
  if [ -f "$KIT_DIR/patches/harnessmith-cjk-review.patch" ]; then
    if git -C "$KIT_DIR/harnessmith" apply --check "$KIT_DIR/patches/harnessmith-cjk-review.patch" 2>/dev/null; then
      git -C "$KIT_DIR/harnessmith" apply "$KIT_DIR/patches/harnessmith-cjk-review.patch"
      echo "  [OK] 已套用 CJK review patch"
    else
      echo "  [INFO] CJK patch 已套用或略過"
    fi
  fi

  # 載入 rustup/cargo
  [ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
  if ! command -v cargo >/dev/null 2>&1; then
    echo "  [INFO] 未偵測到 cargo，正在安裝 rustup（僅裝在你個人家目錄）..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
  fi

  echo "  [INFO] 編譯 harness2 release（首次約需 1~3 分鐘）..."
  (cd "$KIT_DIR/harnessmith" && cargo build --release)
  echo "  [OK] 編譯完成: $KIT_DIR/harnessmith/target/release/harness2"
fi

echo ""
echo "[DONE] 初始化完成！接下來請依照 README.md："
echo "  1. 申請 GPU 資源:   sbatch alloc.sbatch"
echo "  2. 啟動全部服務:    ./start-all.sh <JOBID>"
echo "  3. 下載 LLM 模型:   見 README.md（獨立下載到 ${DATA_DIR}/ollama）"
