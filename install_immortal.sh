#!/usr/bin/env bash
# install_immortal.sh — instala o agente + o sistema à prova de queda numa máquina Linux.
# Rode como root para ganhar todas as camadas (systemd, cron.d, rc.local, profile.d).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="/opt/bsky_agent"

echo "── 1/5 dependências ───────────────────────────────────────────"
python3 -m pip install --upgrade pip
python3 -m pip install -r "$HERE/requirements.txt"

echo "── 2/5 instalando em $TARGET ──────────────────────────────────"
mkdir -p "$TARGET"
cp -r "$HERE"/. "$TARGET"/
cd "$TARGET"
mkdir -p data

echo "── 3/5 cérebro local (Ollama) ─────────────────────────────────"
if ! command -v ollama >/dev/null 2>&1; then
  echo "instalando ollama..."
  curl -fsSL https://ollama.com/install.sh | sh || echo "⚠ falhou — seguindo sem ollama"
fi
if command -v ollama >/dev/null 2>&1; then
  (pgrep -x ollama >/dev/null || (nohup ollama serve >/dev/null 2>&1 &)) || true
  sleep 4
  ollama pull "${OLLAMA_MODEL:-qwen2.5:7b-instruct}" \
    || echo "⚠ sem modelo — o agente usa o motor offline"
fi

echo "── 4/5 persistência (systemd + cron + rc.local + profile.d) ───"
python3 "$TARGET/immortal.py" plant

if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
  systemctl restart bsky-immortal || true
  systemctl is-enabled bsky-immortal || true
else
  setsid nohup python3 "$TARGET/immortal.py" supervise \
    > "$TARGET/data/immortal.log" 2>&1 < /dev/null &
  sleep 3
fi

echo "── 5/5 snapshot + verificação ─────────────────────────────────"
python3 "$TARGET/immortal.py" snapshot
sleep 4
python3 "$TARGET/immortal.py" status || true

cat <<EOF

════════════════════════════════════════════════════════════════
 pronto. o agente agora:
   • roda 24h em   $TARGET
   • revive sozinho se o processo, o supervisor ou a máquina caírem
   • repara o próprio banco, sessão, estado e dependências
   • restaura um snapshot conhecido-bom se o código quebrar

 comandos:
   python3 $TARGET/immortal.py status     # saúde, heartbeats, camadas
   python3 $TARGET/immortal.py autofix    # forçar reparo agora
   python3 $TARGET/immortal.py kill       # parar tudo
   journalctl -u bsky-immortal -f         # logs (systemd)
════════════════════════════════════════════════════════════════
EOF
