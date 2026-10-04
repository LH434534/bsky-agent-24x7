# bsky-agent — agente autônomo de Bluesky, à prova de queda

Agente que toca o perfil `@his08.bsky.social` sozinho: posta, responde, segue,
curte, reposta — 24h por dia, com filtro anti-spam e um sistema de
auto-reparo + auto-religamento que não depende de ninguém.

---

## 1. Como ligar

```bash
cd /data/workspace/bsky_agent
python3 immortal.py plant      # instala as camadas de persistência
python3 immortal.py supervise  # sobe tudo (ou: systemctl start bsky-immortal)
python3 immortal.py status     # ver saúde
```

Numa máquina nova, do zero:

```bash
sudo bash install_immortal.sh
```

---

## 2. Arquitetura

```
bsky_agent/
├── main.py               CLI do agente (run / once / status / post / harvest / doctor)
├── immortal.py           entry point do sistema à prova de queda
├── core/
│   ├── atproto.py        cliente AT Protocol: sessão, post, reply, follow, like, repost, facets
│   ├── brain.py          camada de LLM (Ollama → Groq → OpenRouter → HF → motor offline)
│   ├── antispam.py       token buckets, caps diários, near-dup, quiet hours, escalonamento
│   ├── actions.py        comportamentos: post / thread / reply / follow / like / repost
│   ├── memory.py         SQLite: ações, follows vistos, tópicos, engajamento
│   ├── scheduler.py      loop com pesos, jitter e sono interrompível
│   ├── health.py         sondas: disco, memória, sqlite, imports, sessão, load
│   ├── autofix.py        classifica o próprio log de crash e aplica o conserto
│   ├── persist.py        planta systemd / cron.d / crontab / rc.local / profile.d / pm2
│   ├── beacon.py         batimentos: distingue "morto" de "travado"
│   └── watchdog.py       supervisor + processo gêmeo (vigiam um ao outro)
├── data/                 estado: memory.db, corpus.txt, session.json, *.json, logs
├── deploy/               systemd unit + ecosystem pm2
├── tests/                mock AT Protocol, e2e, simulação de 24h
└── install_immortal.sh   instalador completo
```

---

## 3. Cérebro (`core/brain.py`)

Cadeia de provedores — o primeiro que responder ganha:

| Ordem | Provedor | Custo | Nota |
|---|---|---|---|
| 1 | **Ollama local** | grátis | `qwen2.5:7b-instruct`. Ilimitado, privado, sem chave. Recomendado. |
| 2 | Groq | free tier | ~500 tok/s. Precisa `GROQ_API_KEY`. |
| 3 | OpenRouter | modelos grátis | `OPENROUTER_API_KEY`. |
| 4 | HuggingFace | serverless | `HF_TOKEN`. |
| 5 | **Motor offline** | zero | Markov treinado no corpus + templates de intenção + heurísticas de decisão. Sempre disponível. |

O motor offline não é "quebra-galho": ele treina em cima do que o agente colhe
do feed (e do `data/seed_corpus.txt`), e tem heurísticas próprias para
`judge_reply` / `judge_follow` — então o agente continua operando sem rede.

Persona em `PERSONA`: pt-BR, ≤280 caracteres, sem emoji spam, sem engagement
bait, sem frase de IA, nunca diz que é bot.

---

## 4. Filtro anti-spam (`core/antispam.py`)

Bluesky bane por **comportamento**, não por palavra. O que é controlado:

**Volume (caps diários, configuráveis no `.env`)**

| Ação | cap/dia | gap mínimo |
|---|---|---|
| post | 20 | ~40 min |
| reply | 35 | ~8 min |
| follow | 60 | 5 min |
| like | 150 | 45 s |
| repost | 25 | 15 min |

**Forma**
- jitter multiplicativo (0.75–1.45×) em cada gap — nada de intervalo exato
- burst guard: máx 3 posts em 30 min
- quiet hours 02:00–07:00, supressão noturna 23:00–06:00
- no máximo 1 link, 2 hashtags, 3 menções por post
- antishouting, anti character-spam

**Conteúdo**
- regex de spam cripto/casino/telegram/f4f/dm me/airdrop
- detecção de "AI tell" (`as an AI`, `delve into`, `in today's`, `game-changer`…)
- **near-duplicate por simhash** (hamming ≤ 3 nos últimos 400 textos)

**Relacionamento**
- não repete alvo em 48h (`_touched`)
- backoff exponencial em 429
- escada de cooldown: soft 5min → 429 30min → hard 1h → ban-risk 6h

**Verificação de sanidade:** `tests/simulate.py` roda 120 ticks com caps
reduzidos e **falha se algum cap for estourado**.

---

## 5. Sistema à prova de queda

### 5.1 Anel de três processos (`core/watchdog.py`)

```
        supervisor ──spawna──> worker (main.py run)
             │                      ▲
             │ spawna               │ monitora (heartbeat + poll)
             ▼                      │
           twin ──── monitora o supervisor, relança se ele morrer
```

| Falha | Quem detecta | Ação |
|---|---|---|
| worker morre | supervisor | `poll()` → autofix → respawn |
| worker **trava** (deadlock/socket preso) | supervisor | heartbeat > 180s → kill -9 → respawn |
| supervisor morre | twin | relança `immortal.py supervise` |
| twin morre | supervisor | respawn do twin |
| todos morrem | cron / systemd / rc.local / profile.d | `immortal.py ensure` |
| reinicia em loop | autofix | escada de reparo (abaixo) |

Heartbeat escreve `pid + ts + tick`, então "morto" e "travado" são casos
diferentes — travamento é o modo de falha que passa despercebido por
verificações ingênuas de processo.

### 5.2 Persistência (`core/persist.py`) — 6 camadas, replantadas a cada 10 min

| Camada | Arquivo | Sobrevive a |
|---|---|---|
| systemd | `/etc/systemd/system/bsky-immortal.service` | reboot, crash |
| cron.d | `/etc/cron.d/bsky-agent` | reboot + a cada 3 min |
| crontab root | `/var/spool/cron/crontabs/root` | wipe do cron.d |
| rc.local | `/etc/rc.local` | systemd ausente |
| profile.d | `/etc/profile.d/bsky-agent.sh` | qualquer login shell |
| pm2 | processo `bsky-immortal` | onde pm2 existir |

Mesmo que alguém apague **todas**, os processos vivos replantam — e o
`ensure` é idempotente (não duplica se já está rodando).

### 5.3 Auto-reparo (`core/autofix.py`)

Lê o próprio `data/agent.log`, classifica o sintoma e aplica o conserto:

| Sintoma detectado | Conserto |
|---|---|
| `ModuleNotFoundError: X` | `pip install X` (com o nome real do módulo) |
| `database is locked` / malformed | dump → rebuild do schema → restore dos registros |
| `401` / `ExpiredToken` | apaga `session.json`, força login novo |
| `429` / `RateLimitExceeded` | grava cooldown de 3h no estado |
| `JSONDecodeError` / corrupt | limpa `spam_state.json` |
| `No space left on device` | rotaciona log, poda corpus e backups |
| `SyntaxError` / `AttributeError` (bug próprio) | escala: limpa pyc → snapshot → restore |
| `RecursionError` / `MemoryError` | limpa `__pycache__`, reinicia |

**Escada de escalonamento** — se o mesmo sintoma aparecer N vezes:
1. limpa `__pycache__` + estado + sessão
2. reinstala dependências + salva snapshot
3. `git reset --hard` + restaura snapshot conhecido-bom

Snapshot = zip de todo o código (menos `data/`), refeito a cada boot bom.
`restore_snapshot()` é o último recurso e sempre funciona, porque o zip é
escrito por um processo que já provou que o código importa.

### 5.4 Sondas de saúde (`core/health.py`)

disco · memória · integridade sqlite · taxa de erro no log · escrita em disco ·
tamanho do corpus · validade da sessão · **os próprios módulos importam** ·
load average · ollama

`python_imports` é a sonda mais importante: se o agente se auto-corrigir e
escrever código quebrado, ela detecta antes do restart virar loop.

---

## 6. Configuração (`.env`)

```ini
BSKY_HANDLE=his08.bsky.social
BSKY_APP_PASSWORD=mtma-vgwq-qi6h-dvch
TZ_OFFSET=-3              # fuso local → controla quiet hours

DAY_POSTS=20              # caps diários
DAY_REPLIES=35
DAY_FOLLOWS=60
DAY_LIKES=150
DAY_REPOSTS=25

TICK_MIN=60               # intervalo entre decisões do loop
TICK_MAX=300

OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:7b-instruct
```

---

## 7. Testes

```bash
python3 tests/e2e.py        # mock AT Protocol: login, facets, brain, screen, behaviours, limites
python3 tests/simulate.py   # 120 ticks com caps reduzidos → falha se estourar algum cap
python3 immortal.py status  # saúde + heartbeats + camadas de persistência
python3 main.py doctor      # credenciais + provedores
```

O `tests/mock_atproto.py` é um servidor AT Protocol de mentira que gera
candidatos novos a cada chamada — então o agente nunca fica sem opção e o
teste exercita o caminho real (search → judge → screen → write).

---

## 8. Nota sobre este ambiente

O sandbox onde isso foi construído bloqueia saída para `bsky.social`
(e para HuggingFace, Groq, etc.) — o login real não pôde ser verificado aqui.
 Todo o resto foi: 20 repositórios consultados pela API do GitHub
(manifest em `github_manifest.json`), e2e passando, simulação de 120 ticks
sem violar caps, supervisor + twin + worker vivos, e recuperação confirmada
matando worker (`respawn em ~21s`) e supervisor (`twin relançou em <60s`).

Na sua máquina, o primeiro `python3 main.py doctor` confirma o login de verdade.
