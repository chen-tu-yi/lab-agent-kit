#!/bin/bash
#SBATCH --job-name=local-agent
#SBATCH --nodes=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=10
#SBATCH --mem=32G
#SBATCH --time=24:00:00
#SBATCH --output=logs/slurm-%j.out

# 此 Job 負責向 Slurm 保留 GPU / CPU / RAM 資源，
# 啟動後請在該節點執行 `./start-all.sh <JOBID>` 以 srun --overlap 啟動各服務。
# （若想指定顯卡型號，可改 --gres=gpu:rtx_pro_4000:1 或 --gres=gpu:rtx_5080:1）

set -e
mkdir -p logs
echo "[$(date)] Local Agent allocation started on node: $(hostname) (JOBID=${SLURM_JOB_ID})"
echo "請在 $(hostname) 上執行: ./start-all.sh ${SLURM_JOB_ID}"
sleep infinity
