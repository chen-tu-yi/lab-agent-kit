# Lab Local Agent Kit（Ollama + Open WebUI + Harnessmith）

實驗室 Slurm + Enroot 環境用的 Local Agent 部署範本。每個人的 Port、模型、Open WebUI 資料庫與 Harness 記憶完全獨立，不共享運行資料。

---

## 一、已經幫你包好的部分（照順序執行即可）

以下環境建置與服務啟動都已寫成腳本，不用自己從頭架：

1. **初始化環境（只需跑一次）**
   ```bash
   git clone <你的-repo-URL> ~/lab-agent-kit
   cd ~/lab-agent-kit
   bash init.sh
   ```
   `init.sh` 會自動完成：產生不撞 Port 的 `.env` 與隨機 Token、建立個人隔離資料夾（`~/local-agent-data`，若常跨節點可在 `.env` 改到 `/mnt/nas_home/$USER/local-agent-data`）、下載 Ollama 與 Open WebUI 容器映像（`.sqsh`）、下載並編譯含中文修復的 `harness2`。

2. **申請 GPU 並啟動四個服務**
   ```bash
   sbatch alloc.sbatch
   squeue -u "$USER"
   ```
   等狀態變 `R` 後，確認該 Job 跑在哪個節點（`host` 或 `host2`；若不在該節點請先 `ssh <節點>`），執行：
   ```bash
   ./start-all.sh <JOBID>
   ```
   `start-all.sh` 會自動在背景啟動 `ollama`、`open-webui`、`harnessmith`、`autosync`，並印出你專屬的 Port。
   （若要關閉並釋放 GPU，執行 `scancel <JOBID>`。）

---

## 二、需要你自己下載與設定的部分

以下項目因屬於個人模型、帳號與金鑰，無法預先包好，請在服務啟動後依序完成：

### 1. 下載你要用的 LLM 模型（Ollama）

每個人自己下載想要的模型（例如 `qwen2.5:14b`），模型只會存到你的 `DATA_DIR/ollama`：

```bash
source .env
srun --jobid=<JOBID> --overlap --gres=none --cpus-per-task=1 --mem=2G \
  --container-image="${OLLAMA_IMAGE}" --container-mounts="${DATA_DIR}/ollama:/ollama" \
  --no-container-entrypoint \
  env HOME=/ollama OLLAMA_MODELS=/ollama/models OLLAMA_HOST="127.0.0.1:${OLLAMA_PORT}" \
  ollama pull qwen2.5:14b
```

想確認已下載哪些模型，把最後的 `ollama pull qwen2.5:14b` 換成 `ollama list` 即可。

### 2. 從自己筆電開 SSH Tunnel 連上網頁

把 Port 換成 `start-all.sh` 印出的個人 `WEBUI_PORT` 與 `HARNESS_PORT`（也在 `.env` 裡看得到）：

```bash
ssh -L <WEBUI_PORT>:127.0.0.1:<WEBUI_PORT> -L <HARNESS_PORT>:127.0.0.1:<HARNESS_PORT> <你的帳號>@<Server IP> -p <SSH Port>
```

開啟瀏覽器：
- Open WebUI：`http://127.0.0.1:<WEBUI_PORT>`
- Harnessmith：`http://127.0.0.1:<HARNESS_PORT>`

### 3. 在 Open WebUI 建帳號並把 API Key 填回 `.env`

1. 第一次打開 Open WebUI，註冊你自己的管理員帳號。
2. 左下角頭像 → **Settings → Account → API Keys**，產生一組 `sk-...` Key。
3. 編輯 `~/lab-agent-kit/.env`，把 Key 貼進 `OPENWEBUI_API_KEY=` 後面。
4. 讓 `autosync` 重新讀取 `.env`：
   ```bash
   tmux kill-session -t autosync 2>/dev/null || true
   ./start-all.sh <JOBID>
   ```

### 4. 在 Harnessmith 綁定你的 Ollama 與模型

1. 打開 Harnessmith 網頁 → **Connections** → 新增連線：
   - Name：`ollama`
   - Provider：`ollama`
   - Base URL：`http://127.0.0.1:<OLLAMA_PORT>`（看 `.env` 的 `OLLAMA_PORT`）
2. 進你的 Project（預設名稱 `agent-harness-lab`）→ **Settings**，把 Solver / Improver / Selector / Judge 指定為你剛下載的模型（格式 `ollama/<模型名>`）。

### 5. 在 Open WebUI 建立 Workspace 模型並匯入串接外掛

1. **建立兩個 Workspace 模型**：
   到 **Workspace → Models**，基底都選你下載的 Ollama 模型，分別建立：
   - `testworkspace-lab`（驗證與自動回寫用）
   - `testworkspace`（日常使用）

2. **匯入 `webui-functions/` 裡附好的三個外掛**：
   到 **Admin Panel → Functions → + New Function**，分別貼入以下檔案的內容並啟用：
   - `webui-functions/auto_submit_filter.py`（對話後自動送 Harnessmith 並於驗證通過後自動回寫）
   - `webui-functions/submit_to_harnessmith_action.py`（對話下方手動提交給 Harnessmith 的按鈕）
   - `webui-functions/folder_shared_memory_filter.py`（同資料夾跨對話共享上下文記憶）

3. **填寫前兩支外掛的 Valves**：
   點 `auto_submit_filter` 與 `submit_to_harnessmith_action` 右側齒輪（**Valves**），把 `.env` 裡你自己的值填進去：

   | Valve 欄位 | 填入 `.env` 對應值 |
   | --- | --- |
   | `harness_url` | `http://127.0.0.1:<HARNESS_PORT>` |
   | `hook_token` | `HARNESS2_HOOK_TOKEN` |
   | `autosync_url` | `http://127.0.0.1:<AUTOSYNC_PORT>/callback` |
   | `autosync_token` | `HARNESS2_AUTOSYNC_TOKEN` |
