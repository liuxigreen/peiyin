#!/usr/bin/env bash
# GPU节点一条命令接入算力池。用法：
#   CONTROL_URL=https://dubbing.opspilot.me NODE_SHARED_SECRET=<secret> ./join.sh
set -e
docker build -t dubbing-gpunode -f "$(dirname "$0")/Dockerfile.node" "$(dirname "$0")/.."
docker run --rm --gpus all \
  -e CONTROL_URL="${CONTROL_URL:-http://host.docker.internal:8500}" \
  -e NODE_SHARED_SECRET="${NODE_SHARED_SECRET:?must set}" \
  -e GPU_MODEL="$(nvidia-smi --query-gpu=name --format=csv,noheader)" \
  -e GPU_VRAM="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | cut -d' ' -f1 || echo 0)" \
  dubbing-gpunode
