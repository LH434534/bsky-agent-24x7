# Rodando 24 h por dia — opções

## Arquitetura atual — 4 camadas (implementada)

```
1ª  CADEIA (primária)      cada sessão, ao terminar, dispara a próxima
                           via workflow_dispatch. Não usa cron nenhum.
                           scripts/chain.py no step final do job principal.
2ª  WATCHDOG ×2             checagem a cada ~7 min (crons 0,15,30,45 e
                           7,22,37,52). Dispara só se não houver run
                           in_progress/queued. Cobre: queda no meio do job,
                           timeout duro de 350 min, falha do step final.
3ª  CRON RE-ARMADOR         1x/dia às 00:07 UTC. Só para rearmar se as duas
                           camadas acima falharem ao mesmo tempo.
4ª  CRON EXTERNO (opcional) cron-job.org / Better Stack / Cloudflare
                           Workers chamando a API de dispatch de FORA —
                           não sofre o atraso do scheduler do GitHub.
```

**Por que a cadeia resolve os 24 h de verdade:** o job dura 330 min e o
limite do Actions é 360 min. Antes, 4 crons/dia deixavam ~30 min de buraco
entre sessões, mais o atraso do scheduler, mais tempo morto quando um job
caía. Com a cadeia, o `if: always()` do step final inicia a próxima sessão
assim que a atual termina — inclusive quando o agente falha.

**Kill switch:** variável de repositório `BOT_CHAIN`.
`1` = cadeia ativa (padrão) · `0` = para de renovar (os watchdogs ainda
podem disparar; para parar tudo, desative também os workflows watchdog).

**Guardrails da cadeia** (testados em `test_chain.py`, 11/11):
- exclui o **próprio run** da checagem de "já tem sessão ativa" (senão nunca
  dispararia, pois ele ainda está `in_progress` quando o step final roda);
- falha em < 120 s **não** renova — evita storm; delega ao watchdog;
- cancelamento manual interrompe a cadeia;
- consulta `in_progress` **e** `queued` antes de disparar;
- `concurrency` no job principal impede bots duplicados.

**Prova real:** `chain-probe` encadeou 4 gerações (0→1→2→3), 3 disparos
automáticos e parada limpa, em ~57 s, todos `success`.

**Limitação honesta:** se o job for morto pelo `timeout-minutes: 350`, o
runner não executa steps pendentes — a cadeia não roda. É exatamente esse o
caso coberto pelos watchdogs (até ~7 min de janela).

## Quando o Actions não basta

| Opção | Cobertura | Esforço | Observação |
|---|---|---|---|
| **Cadeia + 2 watchdogs** (atual) | ≈ 99,9% | zero | job máximo 6 h, coberto pela cadeia |
| **+ cron externo** (`cron-externo.md`) | ≈ 99,9% | 5 min | cobre falha do próprio scheduler do GitHub |
| **Container self-hosted** (`docker-compose.yml`) | 100% | 10 min | precisa de um host sempre ligado |
| **systemd em VPS** (`bsky-bot.service`) | 100% | 20 min | reinicia no boot, sem Docker |
| Fly.io / Railway / Render | 100% | conta + cartão | precisa de credenciais suas |
| Celular com Termux | razoável | alto | bateria/rede, não recomendado |

**Recomendação prática:** a cadeia já está ativa; adicione **um** cron
externo gratuito para cobrir falha do próprio scheduler do GitHub. É a combinação com melhor custo/benefício. Se você já tem
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
