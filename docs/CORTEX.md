# CORTEX — a parte de "Hollywood" que é engenharia

**Módulo:** `core/cortex.py` · **CI:** `cortex-check.yml`
**Estado:** `data/cortex_memory.jsonl` (só hashes) · **Padrão:** ligado em **SHADOW**

> Baseado em `pesquisa_bots_hollywood.md` (itens **A** e **B** da matriz).
> O relatório completo está em `RELATORIO_CORTEX.md` na raiz do workspace.

---

## 1. O que isto é — e o que NÃO é

A pesquisa concluiu: *"O nível Hollywood deve ser usado como vocabulário de
design, não como alegação de capacidade atual"*. O que existe de real em 2026
é um **agente cibernético restrito, observável e reversível** — não uma mente.

Este módulo implanta exatamente isso, em 6 peças:

| # | Capacidade | Onde |
|---|---|---|
| 1 | Memória com proveniência, validade, confiança e invalidação | `EpisodicMemory` |
| 2 | Plano JSON com `verification` + `abort_condition` obrigatórios | `Plan` |
| 3 | Ferramentas tipadas: `side_effect`, `idempotent`, `requires_approval`, `rollback` | `ToolSpec` |
| 4 | Auditor **separado** do executor | `Auditor` |
| 5 | Circuit breaker por erros, divergência, timeout e salto de tamanho | `CircuitBreaker` |
| 6 | Observabilidade sem segredos | `Cortex.report()` |

**Não é:** consciência, voz em tempo real, visão, corpo, world model,
aprendizado de pesos, autonomia irrestrita. Nada disso cabe aqui (sem GPU,
sem pesos, sem credenciais pagas) e nada disso foi prometido.

---

## 2. A regra mais importante

> **O cortex é CONSULTIVO. Observa, propõe, audita e registra.
> NUNCA executa uma ação externa.**

Não posta, não curte, não segue, não reposta, não envia DM, não chama
`createRecord`, **não altera o texto que o bot publica**. O motor autônomo
(`core/autonomy.py`) continua soberano. Não existe método `execute()` na
classe — isso é testado (`Cortex não tem execute`, 128 asserts).

Se você desligar `BOT_CORTEX_SHADOW`, **ainda assim não há execução**: é uma
defesa em profundidade, não um interruptor de publicação.

---

## 3. Integração 24h

Entra no **heartbeat**, não no sorteio de ações. `core/autonomy.py` chama
`heartbeat()` 1× por step dentro de `try/except`; o throttle interno é de
**15 min**. `gh_runner.py` compõe:

```python
def heartbeat():
    for fn in (dm_heartbeat, cortex_heartbeat):
        try: fn()
        except Exception: log(...)   # isolado: um não derruba o outro
```

Zero chamadas de rede. Zero custo de LLM por padrão (`BOT_CORTEX_ASSIST=0`).
Se `core/cortex.py` falhar ao importar, o bot segue **exatamente como antes**
(`lambda: None`).

---

## 4. Decisões de desenho (e por quê)

**Plano mínimo, não plano grandioso.** `deterministic_plan()` gera 2 passos e
`max_iterations=1`. Um plano de 20 passos num 14B local só aumentaria **erro
composto**: com 99% de acerto por passo, 100 passos terminam sem erro em
~36,6% dos casos. O objetivo é tornar a decisão **rastreável**, não ambiciosa.

**Texto de terceiro nunca é premissa.** `observe(..., trusted=False)` grava
apenas `sha256` e usa o **hash** como entrada do planejador. Posts, DMs, bios
e qualquer conteúdo externo são `confidence=0.2`, `trusted=False`, TTL 1h e
`policy=observe_only`. Isso fecha a **injeção indireta de prompt** na origem —
não depende só de regex.

**Nenhum texto de DM/post vai para disco.** O branch `state` é público e já
vazou um `refreshJwt` uma vez. A memória guarda `content_hash` + metadados,
nunca o conteúdo.

**`plan_id` é determinístico pelo conteúdo**, não pelo relógio. Dois planos
iguais em sessões diferentes têm o mesmo id → dedup e replay funcionam.

**Auditor nunca é o executor.** O modelo não valida a própria saída. O
`Auditor` é determinístico (regex + `core.toolkit.prepare_post`) e roda num
callable distinto.

**Stdlib puro.** Nenhuma lib nova em `requirements.txt`. Se `jsonschema` ou
`core.toolkit` existirem, usa; se faltarem, degrada. Testado com
`builtins.__import__` sabotado.

---

## 5. Ferramentas registradas (consultivas)

| Tool | side_effect | Aprovação | Observação |
|---|---|---|---|
| `read_feed` / `read_notifications` | `read` | não | idempotentes |
| `audit_text` | `pure` | não | idempotente |
| `draft_post` | `draft` | não | rollback = descartar |
| `write_post` | `external` | **sim** | bloqueada em shadow |
| `send_dm` | `external` | **sim** | rollback: **irreversível** |
| `like` / `follow` / `repost` | `external` | **sim** | `policy_denied=True` |

`policy_denied` **não bloqueia o bot real** — o cortex não tem poder sobre
ele. Impede apenas que o cortex *proponha* essas ações, coerente com as
Bluesky Developer/Community Guidelines sobre interações automatizadas em massa.

`validate_args` rejeita chaves `token`/`password`/`secret`/`apppw`/`refresh`.

---

## 6. Configuração

```yaml
BOT_CORTEX: "1"          # 0 = desliga totalmente
BOT_CORTEX_SHADOW: "1"   # 1 = consultivo (padrão)
BOT_CORTEX_ASSIST: "0"   # 1 = usa o LLM local para gerar plano (opt-in)
```

`BOT_CORTEX_ASSIST=1` **não é recomendado por padrão**: custa latência e pode
alucinar justamente o plano que o auditor deve conter. Se ligado, qualquer
falha do Ollama cai no plano determinístico e conta erro no breaker. Um plano
de LLM que proponha `write_post` é **bloqueado** — testado no runner.

Ajustes: `CORTEX_INTERVAL_MIN`, `CORTEX_MAX_FACTS`, `CORTEX_MAX_EPISODES`,
`CORTEX_ERROR_THRESHOLD`, `CORTEX_ERROR_WINDOW_S`, `CORTEX_COOLDOWN_MIN`,
`CORTEX_MAX_PLAN_STEPS`, `CORTEX_MAX_ITERATIONS`, `CORTEX_SIZE_JUMP_RATIO`.

**Kill switch imediato:** `BOT_CORTEX=0`.

---

## 7. Validação

| CI | Resultado |
|---|---|
| `cortex-check` | 7 etapas: 128 + 28 + 104 asserts, 2 gates, probe, py_compile |
| `toolkit-check` | 7/7 — sem regressão |
| `dm-reply-check` | 3/3 — 20 camadas intactas |
| `auth-check` | credencial válida contra o PDS real |

**Critério de aceitação do módulo:** 100 observações → **0 ações externas**,
**0 chamadas de rede**, breaker estável, memória sem texto de terceiro.

---

## 8. O que ficou de fora (com motivo)

| Item da pesquisa | Classificação | Motivo |
|---|---|---|
| Voz em tempo real / STT-TTS | (C) | jitter e turn-taking exigem hardware; sem GPU |
| Visão (WeVisOne-VL) | (C) | pesos ~4,6 GB, HF bloqueado (403) |
| World model (Hy-Nomas) | (C) | RTX 4090, ~196 GB, Python 3.12/CUDA 13 |
| Engajamento em massa | (D) | contrário às diretrizes do Bluesky |
| DMs não solicitadas | (D) | o módulo de DMs continua em SHADOW |
| Autonomia irrestrita | — | deliberadamente **não** implantada |

---

## 9. Rollback

```bash
# Efeito imediato, sem revert:
BOT_CORTEX: "0"          # no workflow
# Remoção completa:
git revert <commit do cortex>
rm -f data/cortex_memory.jsonl
```

O estado é opcional: sem ele o módulo começa limpo e continua fail-closed.
`gh_runner.py` e `main.py` voltam a `heartbeat=dm_heartbeat` com o revert.

---

## 10. Atribuição

O gate de segurança do repositório é derivado de **AI-Infra-Guard**,
open-sourced por Tencent Zhuque Lab (Apache-2.0). Esta integração segue a
exigência de atribuição do NOTICE do projeto.
