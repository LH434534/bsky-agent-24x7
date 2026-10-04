#!/usr/bin/env bash
# install.sh — instala o agente + o cérebro local (Ollama) numa máquina Linux/macOS.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${OLLAMA_MODEL:-qwen2.5:7b-instruct}"

echo "── 1/4 dependências python ──────────────────────────────────"
python3 -m pip install --upgrade pip
python3 -m pip install -r "$HERE/requirements.txt"

echo "── 2/4 Ollama (cérebro local, grátis, sem limites) ──────────"
if ! command -v ollama >/dev/null 2>&1; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "macOS: instale pelo site https://ollama.com/download ou: brew install ollama"
    echo "depois rode:  ollama serve &"
  else
    curl -fsSL https://ollama.com/install.sh | sh
  fi
else
  echo "ollama já instalado: $(ollama --version 2>/dev/null || echo ok)"
fi

echo "── 3/4 modelo ────────────────────────────────────────────────"
if command -v ollama >/dev/null 2>&1; then
  (pgrep -x ollama >/dev/null || (nohup ollama serve >/dev/null 2>&1 &)) || true
  sleep 4
  ollama pull "$MODEL" || echo "⚠ não consegui baixar $MODEL — o agente cai no motor offline"
else
  echo "⚠ ollama ausente — o agente usa o motor offline (ainda funciona)"
fi

echo "── 4/4 teste de credenciais ──────────────────────────────────"
python3 "$HERE/main.py" doctor || true

cat <<EOF

pronto. para rodar 24/7:

  cd "$HERE"
  python3 main.py run                     # em primeiro plano
  # ou, recomendado, como serviço:
  sudo cp deploy/bsky-agent.service /etc/systemd/system/
  sudo systemctl enable --now bsky-agent
  sudo journalctl -u bsky-agent -f

EOF
