#!/usr/bin/env bash
# 同时启动身份服务和 Streamlit 前端。
# 只起前端会一直停在登录页（身份服务连不上），所以入口统一走这里。
set -euo pipefail

cd "$(dirname "$0")"
source venv312/bin/activate

API_PORT="${AUTH_API_PORT:-8000}"
WEB_PORT="${STREAMLIT_SERVER_PORT:-8501}"
export AUTH_API_URL="${AUTH_API_URL:-http://127.0.0.1:${API_PORT}}"
export AUTH_CORS_ORIGINS="${AUTH_CORS_ORIGINS:-http://localhost:${WEB_PORT},http://127.0.0.1:${WEB_PORT}}"

uvicorn app.api.server:create_server --factory \
  --host 127.0.0.1 --port "$API_PORT" --log-level warning &
API_PID=$!

cleanup() {
  kill "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 50); do
  if curl -fsS "${AUTH_API_URL}/v1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

if ! curl -fsS "${AUTH_API_URL}/v1/health" >/dev/null 2>&1; then
  echo "身份服务启动失败：${AUTH_API_URL}" >&2
  exit 1
fi

streamlit run app/main.py
