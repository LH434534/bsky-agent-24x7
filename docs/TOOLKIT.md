# Toolkit de resiliência do `bsky-agent-24x7`

**Data:** 2026-11-03 · **Status:** integrado e testado

## Por que estas ferramentas

Não é uma lista de "libs legais". Cada uma resolve um problema **já observado**
neste bot ou documentado na literatura de ATProto:

| Ferramenta | Versão | Problema real que ataca |
|---|---|---|
| `ftfy` | 6.3.1 | Posts do feed chegam com mojibake (`doesnÃ¢â‚¬â„¢t`). O bot publicava o lixo sem corrigir. |
| `langdetect` | 1.0.9 | `Bsky.post()` gravava `langs=["pt-BR"]` **sempre**, inclusive para texto em inglês. |
| `vaderSentiment` | 3.3.2 | Léxico feito para redes sociais, sem download de modelo. Permite não responder com exaltação a um post de luto. |
| `tenacity` | 9.1.2 | Retry com **jitter** só em falha transitória. O histórico teve 532 retentativas contra um 401 permanente. |
| `circuitbreaker` / guard próprio | 2.0.0 | Fail-fast: 2 credenciais rejeitadas → aborta com instrução humana, sem insistir. |
| `cachetools` | 5.5.2 | `_handle_cache` era um `dict` sem TTL e sem limite de tamanho num processo 24/7. |
| `jsonschema` | 4.23.0 | Valida `outbox.json` antes de gravar — evita corromper estado. |
| `humanize` | 4.11.0 | Uptime legível nos logs/status. |
| `psutil` | já existia | Telemetria estruturada (`health_snapshot()`). |
| **Outbox** (stdlib) | — | **Idempotência.** O protocolo não tem idempotency key; um `createRecord` que expira + retry = post duplicado, a falha mais comum em bots de publicação. |
| **RateLimitGuard** (stdlib) | — | Lê `ratelimit-remaining` / `ratelimit-reset` e expõe o orçamento. |
| **security_gate** (stdlib) | — | Varre o próprio repo antes de cada push. Derivado das categorias de risco da A.I.G. |

> Baseado em parte nas categorias publicadas por **Tencent Zhuque Lab AI-Infra-Guard** —
> https://github.com/Tencent/AI-Infra-Guard (Apache-2.0, atribuição exigida pelo NOTICE).

## O que mudou no código

### `core/atproto.py` (edição mínima, 6 pontos)
1. Import do toolkit dentro de `try` — se faltar, o bot roda **exatamente como antes**.
2. `__init__`: TTLCache, `PermanentFailureGuard`, `RateLimitGuard`, `Outbox`.
3. `login()`: `guard.check()` no início; `record_failure()` em 401/403 de auth; `record_success()` no fim.
4. `_call()`: `rate.observe(r.headers)` em cada resposta.
5. `post()`: `prepare_post(text, langs)` → texto limpo + `langs` real; bloqueia duplicata via outbox.
6. `post()`: `outbox.mark(text)` **só após** confirmação do PDS.

### `core/toolkit.py` (novo, ~350 linhas)
Todas as libs são **opcionais**: `_try()` devolve `None` e cada função tem fallback
em stdlib. Nenhuma importação nova pode derrubar o bot.

### `scripts/security_gate.py` (novo)
Roda em <1 s, sem API key. Os 3 `curl | sh` do bootstrap do Ollama ficam como
**PENDENTES** (não como "seguros"); um quarto arquivo com o padrão derruba o build.

## Kill switch e rollback

```bash
# Desligar sem mexer no código:
BOT_TOOLKIT=0 python gh_runner.py

# Rollback completo: revert apenas estes arquivos no commit do toolkit
git revert <sha-do-commit>
# Ou, emergência, restaurar só o comportamento antigo:
#   remover o bloco "toolkit" de core/atproto.py e as 8 linhas de requirements.txt
```

`outbox.json` fica ao lado de `session.json`, em `data/` — **fora do git**
(mesma regra de segurança aplicada após o vazamento de JWT).

## Testes

| Onde | O quê | Resultado |
|---|---|---|
| Sandbox | `scripts/toolkit_selftest.py` | **50/50** |
| Sandbox | `scripts/toolkit_integration.py` | **14/14** |
| Sandbox | `security_gate` no repo completo | **OK**, 3 pendentes |
| Sandbox | fixtures maliciosos (reverse shell, exec+b64, curl\|sh novo) | **BLOQUEADO** em todos |
| Runner GitHub | `toolkit-check.yml` | verificado via API |

## Bugs encontrados e corrigidos durante a construção

1. `humanize.naturaltime()` recebia `time.time() - uptime` (timestamp absoluto)
   e devolvia **"56 years ago"**. O certo é o delta negativo: `naturaltime(-uptime)`.
2. Recarregar o módulo com `importlib.reload()` trocava a identidade de
   `TransientError` e quebrava o `isinstance()` capturado no decorator do tenacity.
   O teste do kill switch agora usa `mock` de atributo, sem reload.
3. A regex `encoded_payload` usava `DOTALL` e casava um `b64decode` legítimo na
   linha 70 com a palavra "token" 100 linhas depois, bloqueando
   `scripts/revoke_leaked_session.py`. Reescrita sem `DOTALL`.

## O que **não** foi feito

- Não reintroduzi `session.json` no git. A pesquisa sobre ATProto recomenda
  cachear a sessão para não esgotar `createSession` (300/dia), mas **segurança
  vem primeiro**: o token já vazou uma vez por esse arquivo. O `gh_runner.py`
  continua fazendo login fresco com a App Password. Trade-off registrado.
- Não instalei nada que exija GPU, modelo baixado, API key paga ou conta externa.
- Não alterei o agendamento, a cadeia 24h nem os watchdogs.
- Não expus porta pública nem credencial nova.

## Como usar

```bash
pip install -r requirements.txt
python scripts/toolkit_selftest.py
python scripts/toolkit_integration.py
python scripts/security_gate.py --repo .
```

No runner: **Actions → toolkit-check → Run workflow**.
