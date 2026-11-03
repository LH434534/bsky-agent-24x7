# DMs — resposta automática com guard anti-spam

**Módulo:** `core/dm.py` · **Testes:** `scripts/dm_selftest.py` (103 asserts)
**Validação:** `dm-reply-check.yml` · **Estado:** `data/dm_state.json` (só hashes)
**Padrão:** `BOT_DM_REPLY=1`, **`BOT_DM_SHADOW=1` → somente leitura, zero envios**

---

## 1. O que as regras dizem (e por que o desenho é assim)

As Developer Guidelines são curtas e diretas: **"Don't spam"** [citation:4].
As Community Guidelines proíbem "enviar spam ou postar conteúdo repetidamente
de forma que atrapalhe conversas normais" [citation:6]. A conta oficial de
Safety anunciou reforço contra **"interações automatizadas não solicitadas"**
— com avisos por email antes do label de spam [citation:13]. No Transparency
Report 2025, **81%** das remoções manuais vieram das três políticas
"Be Authentic", com detecção automatizada de **redes de spam coordenadas**
[citation:14].

Duas recomendações oficiais moldaram este módulo [citation:6]:

1. **Bots que interagem devem ser opt-in** — só interagir quando o usuário
   tiver se dirigido a eles primeiro.
2. **Contas automatizadas devem se identificar** — self-label no perfil.

Uma DM que um humano **enviou espontaneamente ao bot** é o opt-in mais
explícito possível. E, por definição, responder a ela **não é uma interação
não solicitada**. É exatamente o oposto de outreach, astroturfing e
engajamento em massa — que são os padrões que a moderação persegue.

> ⚠️ **Risco residual, dito sem rodeios:** nenhum guard interno altera a
> política da plataforma. A Bluesky não publica limiares numéricos e pode
> sinalizar a conta se interpretar o padrão como spam. Este módulo **reduz o
> risco operacional ao mínimo**, mas não o elimina. Por isso o envio real fica
> atrás de duas chaves e começa **desligado**.

---

## 2. As 20 camadas

Todas são **fail-closed**: qualquer dúvida, exceção ou dado ausente → silêncio.

| # | Camada | Bloqueia |
|---|---|---|
| 1 | `BOT_DM_REPLY` | tudo, se desligado |
| 2 | `BOT_DM_SHADOW` | o **envio** (calcula, não manda) |
| 3 | `status=accepted` | chat **requests** — o bot nunca `acceptConvo` |
| 4 | `kind=direct` | grupos |
| 5 | `members == 2` | conversas que não são 1:1 |
| 6 | `sender != self` | mensagem do próprio bot / sem DID |
| 7 | frescor ≤ 120 min | backlog antigo (evita rajada na 1ª execução) |
| 8 | opt-out permanente | DID que já pediu para parar, em todas as sessões |
| 9 | opt-out na mensagem | "pare de responder", "não responda", "stop", "unsubscribe"… |
| 10 | dedup por `message.id` | reenvio da mesma mensagem |
| 11 | anti-ping-pong | penúltima mensagem era do bot (loop entre dois agentes) |
| 12 | filtro de conteúdo | links, crypto/casino/airdrop/telegram, CTA, ≥3 @menções, texto degenerado |
| 13 | 1 resposta / remetente / 24h | insistência |
| 14 | 2 respostas / remetente / 7d | insistência prolongada |
| 15 | 3 respostas / conta / 24h | volume global |
| 16 | 10 respostas / conta / 7d | volume global prolongado |
| 17 | ≥20 min entre respostas | rajadas |
| 18 | resposta gerada | `SILÊNCIO` do brain, vazia, só pontuação |
| 19 | saneamento da saída | link, @menção, CTA, eco da mensagem, >300 graphemes, degeneração (reusa `prepare_post`) |
| 20 | novidade | resposta duplicada (hash) ou >88% similar (difusa) |

**Proibições estruturais** (não são camadas, são ausências no código):
`sendMessage` só aparece em **um** método; não existe `acceptConvo`,
`getConvoForMembers`, `leaveConvo`, `addReaction`, `updateRead` nem
`updateSeen`. O bot **nunca inicia conversa** e **nunca marca como lido**
(para não apagar o indicador de não-lido para você no app).

**Transparência:** `DM_DISCLOSURE=1` prefixa `"(resposta automática) "` em
cada resposta. Recomenda-se também o **self-label de bot no perfil** — é a
boa prática oficial [citation:6]. Se preferir texto mais natural, use
`DM_DISCLOSURE=0` **e** mantenha o self-label.

---

## 3. Serviço e roteamento (validado no runner)

DMs **não** passam por `app.bsky.notification.listNotifications` — aquele
endpoint só devolve `like/repost/follow/mention/reply/quote`. Elas vivem em
`chat.bsky.convo.*`, num serviço separado roteado por
`did:web:api.bsky.chat#bsky_chat` [citation:9].

O `dm-probe` #3 mediu, com a credencial real:

| Rota | HTTP |
|---|---|
| `bsky.social` + proxy (o PDS que o bot usa) | **501** `MethodNotImplemented` |
| PDS real do `didDoc` + proxy | **200** |
| **`https://api.bsky.chat` + `accessJwt`** | **200** ✅ |

O módulo usa a rota direta (a que foi provada). A App Password **já tem
escopo privilegiado** — não precisa ser trocada.

`listConvos` é chamado com `status=accepted&kind=direct&readState=unread`:
três filtros feitos **no servidor**, antes de qualquer processamento [citation:9].

---

## 4. Integração 24h

O módulo **não entra no sorteio de ações** do `core/autonomy.py` — o motor
poderia escolher a mesma ação dezenas de vezes por minuto. Ele vai no
`heartbeat()`, chamado 1× por step dentro de `try/except`, com **throttle de
15 min**. Mesmo que o heartbeat dispare mil vezes, as cotas 13–17 valem.

`gh_runner.py` e `main.py` usam `make_heartbeat(bsky, brain)`. Se o módulo
falhar ao importar, o bot segue **exatamente como antes** (`lambda: None`).

---

## 5. Privacidade

O estado persiste **apenas** `sha256` de `message.id`, `sha256` de DID,
`sha256` do texto da resposta e timestamps. **Nenhum texto de DM** vai para
disco, log ou git — o branch `state` é público e já vazou um JWT uma vez.
O `secret_gate.py` e o `security_gate.py` continuam no CI.

Em **shadow**, a cota **não é consumida**: quando o envio real for ligado, as
mensagens observadas ainda estarão elegíveis.

---

## 6. Como ligar o envio real

No workflow `.github/workflows/bsky-bot-24x7.yml`:

```yaml
env:
  BOT_DM_REPLY: "1"
  BOT_DM_SHADOW: "0"   # ← única mudança
```

São **duas chaves**: um typo ou um `BOT_DM_SHADOW` ausente mantém shadow.
Recomenda-se, antes:

1. Rodar `dm-reply-check.yml` e conferir `ENVIADAS=0 send_calls=0`
2. Adicionar o **self-label de bot** no perfil
3. Observar `teria_enviado` por alguns dias em shadow
4. Só então trocar para `0` e acompanhar os motivos de bloqueio

Variáveis de ajuste (todas com padrões conservadores):
`DM_MAX_PER_SENDER_24H`, `DM_MAX_PER_SENDER_7D`, `DM_MAX_GLOBAL_24H`,
`DM_MAX_GLOBAL_7D`, `DM_MIN_INTERVAL_MIN`, `DM_FRESHNESS_MIN`,
`DM_MAX_GRAPHEMES`, `DM_SIMILARITY_MAX`, `DM_DISCLOSURE`, `DM_MAX_CONVOS`.

**Kill switch imediato:** `BOT_DM_REPLY=0` (ou `BOT_DM_SHADOW=1`).

---

## 7. Rollback

```bash
git revert <commit do módulo>       # remove core/dm.py, integração e workflow
# ou, sem revert: BOT_DM_REPLY=0 no workflow — efeito imediato
rm -f data/dm_state.json             # zera cotas/opt-out (opcional)
```

O estado é opcional: sem ele o módulo começa limpo e continua fail-closed.

---

## 8. O que este módulo **NÃO** faz

- Não faz outreach, não aceita requests, não responde em grupo
- Não curte, segue, reposta ou comenta em massa
- Não compra engajamento, não cria contas, não burla rate limits
- Não otimiza outrage, clickbait ou "curte e compartilha"
- Não publica texto de DM em lugar nenhum
- Não marca conversa como lida
