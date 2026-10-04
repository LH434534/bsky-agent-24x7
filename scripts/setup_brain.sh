#!/usr/bin/env bash
# setup_brain.sh — instala um LLM de verdade dentro do runner do GitHub Actions.
#
# O runner tem internet livre (ao contrário do sandbox), então baixamos o
# Ollama + um modelo quantizado e servimos em localhost:11434.
# O core/brain.py já sonda essa porta em primeiro lugar — então o agente
# passa a usar o modelo automaticamente, sem trocar uma linha de código.
set -uo pipefail

PRIMARY="${OLLAMA_MODEL:-qwen2.5:7b-instruct}"
FALLBACKS="${OLLAMA_FALLBACKS:-qwen2.5:3b-instruct llama3.2:3b qwen2.5:1.5b-instruct}"
HOST="${OLLAMA_HOST:-http://localhost:11434}"

say() { echo "[brain] $*"; }

# ── 1. instala o binário ────────────────────────────────────────────────────
if ! command -v ollama >/dev/null 2>&1; then
  say "instalando ollama..."
  curl -fsSL https://ollama.com/install.sh | sh || say "install.sh falhou"
fi
command -v ollama >/dev/null 2>&1 || { say "ollama ausente — seguindo com motor offline"; exit 0; }

# ── 2. sobe o servidor ──────────────────────────────────────────────────────
mkdir -p "$HOME/.ollama"
pkill -f "ollama serve" 2>/dev/null || true
nohup ollama serve > /tmp/ollama.log 2>&1 &
for i in $(seq 1 40); do
  curl -sf "$HOST/api/tags" >/dev/null 2>&1 && break
  sleep 2
done
curl -sf "$HOST/api/tags" >/dev/null 2>&1 \
  && say "ollama serve up" \
  || { say "ollama não subiu: $(tail -3 /tmp/ollama.log 2>/dev/null)"; exit 0; }

# ── 3. baixa o modelo (com cadeia de fallback por tamanho) ──────────────────
chosen=""
for m in "$PRIMARY" $FALLBACKS; do
  say "pull $m ..."
  if timeout 900 ollama pull "$m"; then
    chosen="$m"
    say "modelo ok: $m"
    break
  fi
  say "falhou: $m"
done

if [ -z "$chosen" ]; then
  say "nenhum modelo baixou — motor offline assume"
  exit 0
fi

# ── 4. publica a escolha para os próximos steps ─────────────────────────────
[ -n "${GITHUB_ENV:-}" ] && echo "OLLAMA_MODEL=$chosen" >> "$GITHUB_ENV"
echo "$chosen" > "${GITHUB_WORKSPACE:-.}/data/brain_model.txt" 2>/dev/null || true

# ── 5. warm-up + prova de que gera texto ────────────────────────────────────
say "warm-up..."
warm=$(curl -s "$HOST/api/chat" -H 'Content-Type: application/json' -d "{
  \"model\":\"$chosen\",\"stream\":false,
  \"options\":{\"temperature\":0.9,\"num_predict\":60},
  \"messages\":[{\"role\":\"user\",\"content\":\"Write one short Bluesky post about automation. Max 280 chars.\"}]}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['message']['content'])" 2>/dev/null)

if [ -n "$warm" ]; then
  say "MODELO REAL ATIVO: $chosen"
  say "sample: $warm"
else
  say "warm-up sem resposta — motor offline assume"
fi

ollama list || true
