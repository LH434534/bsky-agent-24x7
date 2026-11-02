# Cron externo — cobre quando o próprio GitHub Actions falha

O `schedule` do GitHub Actions **atrasa** (e pode nem disparar) sob carga da
plataforma. Um disparo vindo de FORA não sofre esse atraso.

## Endpoint

```
POST https://api.github.com/repos/LH434534/bsky-agent-24x7/actions/workflows/bsky-bot-24x7.yml/dispatches
Accept: application/vnd.github+json
Authorization: Bearer <TOKEN>
Content-Type: application/json

{"ref":"main"}
```

Resposta esperada: `204 No Content`.

## Token de escopo mínimo

Crie um **fine-grained PAT** só com `Actions: read/write` neste repositório.
Não reutilize um token com `delete_repo` / `admin:org`. Guarde-o como secret
no serviço de cron, nunca na URL nem em log.

## Serviços que fazem isso sem servidor próprio

| Serviço | Intervalo | Como configurar |
|---|---|---|
| cron-job.org | 1 min+ | "Common cron expression" `*/15 * * * *`, método POST, headers acima |
| Better Stack / UptimeRobot | 1–5 min | Monitor HTTP com método POST + body + headers |
| Cloudflare Workers + Cron Trigger | 1 min | `fetch()` no endpoint, token em secret |
| GitHub Actions de OUTRO repo | 5 min | workflow com `schedule` chamando a API |

## Importante

O workflow principal já tem `concurrency: group: bsky-bot-24x7` com
`cancel-in-progress: false`. Disparos repetidos **não criam bots duplicados** —
no máximo entram na fila. O `watchdog.yml` deste repo faz a mesma checagem de
dentro; usar os dois é redundante, mas barato e aumenta a resiliência.

## Verificação

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/repos/LH434534/bsky-agent-24x7/actions/workflows/bsky-bot-24x7.yml/dispatches \
  -d '{"ref":"main"}'
# 204 = ok
```
