#!/usr/bin/env bash
# Run Pocket locally, reachable from Discord/Telegram/Twilio through ngrok.
#
#   ./scripts/dev.sh            (or: make dev)
#
# 1. starts an ngrok tunnel to :8080 (reuses one that's already running)
# 2. writes its https URL into .env as POCKET_PUBLIC_URL
# 3. starts `pocket serve` on :8080
# 4. points your Discord app's Interactions Endpoint at the tunnel (if Discord is configured)
# 5. registers the Telegram webhook (if Telegram is configured)
# Ctrl-C stops the server and the tunnel it started.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8080}"

started_ngrok=""
cleanup() {
  [[ -n "${server_pid:-}" ]] && kill "$server_pid" 2>/dev/null || true
  [[ -n "$started_ngrok" ]] && kill "$started_ngrok" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

tunnel_url() {
  curl -s localhost:4040/api/tunnels 2>/dev/null |
    python3 -c "import sys,json; t=json.load(sys.stdin)['tunnels']; print(next((x['public_url'] for x in t if x['public_url'].startswith('https')), ''))" 2>/dev/null || true
}

# --- 1. tunnel
URL="$(tunnel_url)"
if [[ -z "$URL" ]]; then
  echo "→ starting ngrok on :$PORT"
  ngrok http "$PORT" --log stdout > data/ngrok.log 2>&1 &
  started_ngrok=$!
  for _ in $(seq 1 20); do URL="$(tunnel_url)"; [[ -n "$URL" ]] && break; sleep 1; done
fi
[[ -z "$URL" ]] && { echo "✗ ngrok didn't come up (see data/ngrok.log)"; exit 1; }
echo "→ public URL: $URL"

# --- 2. .env
python3 - "$URL" <<'EOF'
import re, sys, pathlib
p = pathlib.Path(".env"); s = p.read_text() if p.exists() else ""
line = f"POCKET_PUBLIC_URL={sys.argv[1]}"
s = re.sub(r"^POCKET_PUBLIC_URL=.*$", line, s, flags=re.M) if re.search(r"^POCKET_PUBLIC_URL=", s, re.M) else s.rstrip("\n") + "\n" + line + "\n"
p.write_text(s)
EOF

# --- 3. server (stop anything already on the port first)
if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "→ stopping what's already on :$PORT"
  kill $(lsof -tiTCP:"$PORT" -sTCP:LISTEN) 2>/dev/null || true
  sleep 2
fi
mkdir -p data
echo "→ starting pocket on :$PORT (logs: data/server.log)"
uv run pocket serve --port "$PORT" > data/server.log 2>&1 &
server_pid=$!
for _ in $(seq 1 60); do curl -s "localhost:$PORT/healthz" >/dev/null && break; sleep 1; done
curl -s "localhost:$PORT/readyz"; echo

set -a; source .env; set +a

# --- 4. discord
if [[ -n "${POCKET_DISCORD_BOT_TOKEN:-}" ]]; then
  got=$(curl -s -X PATCH https://discord.com/api/v10/applications/@me \
    -H "Authorization: Bot $POCKET_DISCORD_BOT_TOKEN" -H "Content-Type: application/json" \
    -d "{\"interactions_endpoint_url\": \"$URL/webhook/discord\"}" |
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('interactions_endpoint_url') or d)")
  echo "→ discord endpoint: $got"
fi

# --- 5. telegram
if [[ -n "${POCKET_TELEGRAM_BOT_TOKEN:-}" && -n "${POCKET_TELEGRAM_WEBHOOK_SECRET:-}" ]]; then
  echo "→ telegram: $(uv run pocket telegram-webhook 2>/dev/null | head -1)"
fi

echo
echo "✓ Pocket is up. Dashboard: http://localhost:$PORT/app?token=\$POCKET_ADMIN_TOKEN (token is in .env)"
echo "  Ctrl-C to stop. Following the log:"
tail -f data/server.log
