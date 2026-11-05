# Autoridade orgânica, DMs reativas e bio autoavaliada

**Status:** implantado e validado. Nenhuma promessa de alcance. Nenhuma
manipulação. Nenhuma interação em massa.

---

## 1. O que foi pedido e o que foi recusado

O pedido pedia "engenharia social em nível global", "persuasão para ganhar
seguidores" e "crescer 4× mais rápido". **Isso não foi implementado**, por três
motivos concretos:

1. **Engenharia social**, no sentido de segurança, é induzir alguém a agir por
   engano ou pressão: pretexting, urgência fabricada, prova social falsa,
   autoridade inventada. Aplicado a crescimento de perfil, vira astroturfing —
   classificado como spam pelas diretrizes do Bluesky.
2. **"4× mais rápido" é um número que não existe.** Não há baseline, não há
   dados de impressões (o ATProto não os expõe) e o alcance depende da
   plataforma. Calcular ou afirmar um multiplicador seria inventar resultado.
   Nenhum módulo projeta crescimento.
3. **Seguir/curtir/responder em massa** para forçar reciprocidade é
   explicitamente vedado. Nada nesta camada aumenta o número de interações.

O que foi implementado no lugar é **autoridade orgânica**: conteúdo mais
específico, voz mais estável, reciprocidade com quem já falou e uma bio que se
autoavalia. É o tipo de presença que cresce sem pedir.

> Regra que vale para todo o sistema: **o system prompt proíbe CTA, número
> inventado, prêmio, prazo, escassez, "todo mundo está falando", resultado
> garantido e se declarar especialista.**

---

## 2. `core/influence.py` — medir, não manipular

Score **0..100 explicável**, com 6 componentes e pesos declarados:

| Componente | Peso | Origem |
|---|---|---|
| especificidade | 24 | `viral.concreteness` |
| clareza | 18 | `viral.ease_of_processing` |
| autenticidade | 22 | `viral.authenticity` |
| consistência | 18 | similaridade com o histórico (penaliza repetição) |
| regularidade | 8 | distribuição de horários nos `outcomes` reais |
| integridade | 10 | zera se houver CTA / manipulação / clichê |

`blocklist()` barra 4 famílias: pedido de engajamento, manipulação/urgência
falsa, clichê de IA e autoridade inventada. Inclui os próprios termos do
pedido (`4x mais rápido`, `engenharia social`, `guru`, `10k seguidores`).

`tendencia()` compara a **mediana de reach real** da primeira e da segunda
metade dos `outcomes`. Com menos de 10 amostras devolve `None`. Devolve
`alta / estável / queda` — **nunca** uma projeção, **nunca** "4×".

`influence_hint()` vai para o `_growth_hint()` do `Actions` em linguagem
natural, como a dica viral: inclina, não força. **Zero chamada de LLM extra**,
zero rede, zero nova interação.

Kill switch: `BOT_INFLUENCE=0`.

### Correção encontrada testando

As três métricas de `core/viral.py` têm assinatura `(text)`; passar
`historico=` levantaria `TypeError` e faria **toda** medição cair no default
0.5 — um score plano e inútil. Corrigido antes de publicar, com teste que
prova que texto concreto pontua mais que texto abstrato.

---

## 3. DMs reativas com IA — ligadas, com identificação

`BOT_DM_SHADOW` foi trocado de `1` para `0` no workflow principal. O bot agora
**responde com IA** (`brain.write_reply`, Ollama com fallback offline) quando
**alguém lhe envia uma mensagem primeiro**.

Permanecem as 20 camadas anti-spam documentadas em `docs/DM.md`:

- só conversa `accepted` + `direct` + 2 membros — **nunca inicia**, nunca
  aceita request, nunca responde em grupo;
- opt-out permanente por DID e por mensagem ("pare", "não responda", "stop");
- dedup por `message.id`, anti-ping-pong, novidade difusa, saneamento da resposta;
- cotas: 1/remetente/24h · 2/7d · **3/conta/24h** · 10/7d · ≥20 min entre respostas.

**Nova trava (fail-closed):** se `DM_DISCLOSURE` for desligado junto com o
shadow, `Config.from_env()` **volta a shadow** em vez de enviar mensagem
anônima. Toda resposta começa com `(resposta automática) `.

O texto da DM nunca vai para disco, log ou git — só hashes.

### Risco residual, dito claramente

Responder DM gera notificação para um terceiro. É o caso mais defensável
(reativo, 1:1, esperado, identificado, com opt-out), mas **nenhum guard
interno altera a política da plataforma**. A Bluesky não publica limiares.
Recomendações que continuam valendo: **self-label de bot no perfil** e
observar os logs antes de aumentar qualquer cota.

Para desligar: `BOT_DM_SHADOW: "1"`. Para silenciar tudo: `BOT_DM_REPLY: "0"`.

O CI **nunca** envia DM: `influence-check.yml` roda com `BOT_DM_SHADOW=1` e o
`dm_shadow_probe.py` força shadow independentemente do ambiente.

---

## 4. `core/profile.py` — a bio escolhe se muda ou não

O bot **decide sozinho**, com critérios objetivos e auditáveis:

| Sinal | Efeito |
|---|---|
| menos de 10 posts medidos | **não mexe** (sem amostra não há "nível") |
| bio atual sem problema objetivo | **não mexe** (estabilidade é uma decisão) |
| candidata reprovada no gate | **não mexe** |
| sem melhora ≥ 5 pontos no score | **não mexe** |
| menos de 30 dias desde a última troca | **não mexe** |
| já trocou 1× nos últimos 30 dias | **não mexe** |
| bio mudou desde a avaliação | **não mexe** (concorrência) |
| **bio ruim + candidata melhor + fora do cooldown** | **muda** |

"Fora do nível" é explícito: vazia/curta, genérica, afirma ser IA/bot, contém
CTA, clichê de IA, link, padrão de segredo, manipulação ou autoridade
inventada.

Quatro candidatas **pré-escritas na voz do personagem**, escolhidas por
`SHA-256(data + arquétipo)`. Sem LLM: zero custo, zero latência e nenhuma
chance de o modelo inventar número ou promessa.

### Escrita segura

Rota confirmada na documentação oficial do ATProto:
`com.atproto.repo.getRecord` → **merge** → `com.atproto.repo.putRecord` com
`rkey=self` e `swapRecord` = CID lido.[citation:13][citation:8]

- **Merge, nunca replace**: preserva `avatar`, `banner`, `displayName`,
  `labels`, `pinnedPost`, `pronouns`, `website`, `createdAt`, `$type`.
- `swapRecord` faz a escrita **falhar** se outra sessão alterou o perfil no
  meio-tempo, em vez de apagar a mudança alheia.
- `description` respeita o limite do lexicon: **256 graphemes**.
- Checkpoint da bio anterior gravado **antes** da escrita; rollback em
  `scripts/bio_rollback.py`.

`BOT_BIO_APPLY=0` (padrão) mantém **DRY-RUN**: o bot avalia, decide e registra,
mas **não grava nada** no perfil público. A escrita só acontece com `APPLY=1`.

Kill switch: `BOT_BIO=0`.

---

## 5. System prompt

`core/persona.py` ganhou o bloco **"COMO VOCÊ CONSTRÓI AUDIÊNCIA"**, herdado
por todos os 6 arquétipos e pelo modo `BOT_PERSONAS=0`:

> Gente volta a ler quem é específico e constante, não quem pede atenção.
> Autoridade vem de reparar bem e dizer simples. Nunca peça para seguir,
> curtir, compartilhar ou comentar. Nunca invente número, prêmio, prazo,
> escassez ou resultado garantido. Responda a quem falou com você; não corra
> atrás de quem não falou.

É persuasão no sentido clássico (credibilidade + clareza + reciprocidade),
não no sentido de induzir por engano. **Nenhum conteúdo 18+ ou vulgar** foi
adicionado — a camada é de tom/ritmo, e o teste varre os arquétipos com
regex proibindo termos explícitos.

---

## 6. Variáveis

| Var | Default | Efeito |
|---|---|---|
| `BOT_INFLUENCE` | `1` | `0` desliga medição e hint |
| `BOT_DM_SHADOW` | `0` | `1` volta a somente-leitura |
| `DM_DISCLOSURE` | `1` | **obrigatório**; sem ele volta a shadow |
| `BOT_BIO` | `1` | `0` desliga a avaliação da bio |
| `BOT_BIO_APPLY` | `0` | `1` permite a escrita (merge + swapRecord) |
| `BIO_MIN_DAYS` | `30` | cooldown entre trocas |
| `BIO_MIN_POSTS` | `10` | amostra mínima para julgar |
| `BIO_MAX_PER_30D` | `1` | teto de trocas |

---

## 7. Testes e CI

```bash
python scripts/influence_selftest.py       # blocklist, score, tendencia, hint
python scripts/profile_selftest.py        # decisao, merge, swapRecord, rollback
python scripts/influence_integration.py   # DM identificada, prompt, dry-run
python scripts/profile_probe.py           # SOMENTE LEITURA no runner
```

`Actions → influence-check → Run workflow` (12 etapas). Roda também a
**regressão completa das 12 suítes anteriores** e os gates de segurança.

Critérios de aceitação:
- nenhum texto com CTA/manipulação passa no gate;
- `tendencia()` nunca devolve projeção nem multiplicador;
- `_growth_hint()` não acrescenta nenhuma chamada de LLM;
- em DRY-RUN, **zero** `putRecord`;
- `putRecord` sempre com `swapRecord` e merge completo;
- CI **nunca** chama `sendMessage`.

---

## 8. Rollback

| O que | Como |
|---|---|
| Autoridade | `BOT_INFLUENCE: "0"` |
| DMs | `BOT_DM_SHADOW: "1"` ou `BOT_DM_REPLY: "0"` |
| Bio | `BOT_BIO_APPLY: "0"`; se já gravou, `python scripts/bio_rollback.py` |
| Tudo | `git revert <commit>` — os módulos são novos, nada é removido |

`data/bio_state.json` guarda decisões (só metadados e hashes) e o checkpoint
para rollback. Nenhum texto de terceiro é persistido.

---

## 9. Limites honestos

- **Não há como garantir 4×**, nem 2×, nem qualquer aceleração. O único número
  real é a mediana de `reach` dos seus próprios `outcomes`, e ela só fica
  confiável com ≥10 amostras.
- O score de autoridade é **heurístico** (regex + métricas léxicas), não
  compreensão semântica nem medição de impressões.
- A bio é determinística e conservadora: com uma bio já coerente, a decisão
  correta é **não mudar nada** — possivelmente para sempre. Isso é intencional.
- Responder DM é uma interação automatizada com terceiros. Mesmo com 20
  camadas, disclosure, opt-out e cotas, o risco de interpretação pela
  plataforma é **zero apenas na política, não na prática**.
- `getRecord` é testado no runner por um probe somente-leitura. Enquanto a
  rota não estiver confirmada, `BOT_BIO_APPLY` permanece `0`.
