# Rodando 24 h por dia — opções

## Situação atual (GitHub Actions)

O workflow `bsky-bot-24x7.yml` roda sessões de 330 min em 4 horários
(00:07, 06:07, 12:07, 18:07 UTC). Isso dá **22 h/dia**, não 24 h: sobram
~30 min entre sessões, mais o atraso natural do scheduler do GitHub, mais o
tempo morto se um job cair no meio.

O `watchdog.yml` (a cada 15 min) cobre esses buracos: se não houver nenhum
run `in_progress` ou `queued`, ele dispara uma nova sessão. Cobertura prática
≈ 99%, sem custo (repo público).

## Quando o Actions não basta

| Opção | Cobertura | Esforço | Observação |
|---|---|---|---|
| **Actions + watchdog** (atual) | ≈ 99% | zero | job máximo 6 h, scheduler pode atrasar |
| **+ cron externo** (`cron-externo.md`) | ≈ 99,9% | 5 min | cobre falha do próprio scheduler do GitHub |
| **Container self-hosted** (`docker-compose.yml`) | 100% | 10 min | precisa de um host sempre ligado |
| **systemd em VPS** (`bsky-bot.service`) | 100% | 20 min | reinicia no boot, sem Docker |
| Fly.io / Railway / Render | 100% | conta + cartão | precisa de credenciais suas |
| Celular com Termux | razoável | alto | bateria/rede, não recomendado |

**Recomendação prática:** mantenha Actions + watchdog e adicione **um** cron
externo gratuito. É a combinação com melhor custo/benefício. Se você já tem
um VPS ou Raspberry Pi ligado 24 h, rode o container lá e use o Actions como
fallback — as duas camadas usam o mesmo `data/`, então o estado precisa ser
compartilhado (ex.: branch `state` ou volume sincronizado).

## Container (Docker)

```bash
cd deploy
cp .env.example .env      # preencha BSKY_HANDLE e BSKY_APP_PASSWORD
docker compose up -d --build
docker compose logs -f
docker compose ps         # STATUS deve ser "Up" / healthy
```

- `restart: unless-stopped` → volta após queda, reboot do host e OOM.
- Volume `./state:/app/data` → memória, contadores anti-spam e sessão persistem.
- `main.py run` é o loop 24/7 nativo; **não usa git**, então não precisa de
  token do GitHub dentro do container.
- Sem Ollama, o `Brain` cai sozinho para o gerador offline local.

## systemd (sem Docker)

```bash
sudo useradd -r -m -d /opt/bsky-agent bskybot
sudo cp -r core/ main.py requirements.txt /opt/bsky-agent/
sudo python3 -m venv /opt/bsky-agent/.venv
sudo /opt/bsky-agent/.venv/bin/pip install -r /opt/bsky-agent/requirements.txt
sudo cp deploy/bsky-bot.service /etc/systemd/system/
sudo cp deploy/.env.example /opt/bsky-agent/.env   # editar depois
sudo systemctl daemon-reload
sudo systemctl enable --now bsky-bot
sudo systemctl status bsky-bot
journalctl -u bsky-bot -f
```

## PM2 (alternativa já presente no repo)

`deploy/ecosystem.config.js` e `deploy/bsky-agent.service` são as camadas de
supervisão que já existiam no projeto. Funcionam e podem ser usadas em vez do
`docker-compose.yml`:

```bash
pm2 start deploy/ecosystem.config.js
pm2 save && pm2 startup
pm2 logs bsky-agent
```

## Verificação (qualquer opção)

```bash
# 1. credencial
python3 scripts/check_auth.py                     # exit 0 = ok

# 2. o bot realmente postou
python3 scripts/verify_post.py                    # lê o feed público

# 3. há sessão ativa?
python3 scripts/watchdog.py                      # GH_TOKEN + GH_REPO
```

## Limitação conhecida

`main.py run` e `gh_runner.py` gravam estado em `data/` de forma independente.
Rodar os **dois ao mesmo tempo em máquinas diferentes** pode gerar conflito
nos contadores anti-spam. Escolha uma camada primária e deixe a outra só como
fallback (desligada até a primária cair).
