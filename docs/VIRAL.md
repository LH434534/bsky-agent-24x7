# Camada viral — compartilhamento sem manipulação

**Data:** 2026-11-03 · `core/viral.py`

## 1. O que foi RECUSADO, e por quê

O pedido falava em "todos os métodos mais poderosos do mundo". Os métodos de
maior alcance bruto são justamente os **proibidos**. Confirmado nas fontes
oficiais antes de escrever uma linha:

| Método | Situação |
|---|---|
| like / reply / follow automatizado em massa | **Spam** pelas Developer Guidelines: "interações automatizadas ou em massa, inclusive as que geram notificação" [citation:3] |
| Automatizar geração de seguidores ou interações | **Proibido** explicitamente [citation:3] |
| Astroturfing, contas falsas, compra de engajamento | Veda "manipular sinais sociais para alcançar alcance imerecido" [citation:4][citation:8] |
| Outrage / negatividade manufacturada | Aumenta alcance [citation:13], mas é antiético e o Transparency Report 2025 mostra detecção automatizada de "coordinated inauthenticity" — 81% das remoções são "Be Authentic" [citation:14] |
| Clickbait / curiosity gap | Reduz compartilhamento **por percepção de manipulação** [citation:13] |
| "Curte, comenta, compartilha" | Pedido de engajamento, contrário às diretrizes |
| Burlar rate limits / aumentar volume | 5.000 pontos/hora, 35.000/dia, 300 `createSession`/dia [citation:12]; o `SpamGuard` do bot já limita e não foi alterado |

**Nada disso foi implementado.** `scripts/viral_selftest.py` tem asserções que
leem o código-fonte e falham se qualquer uma dessas chamadas aparecer.

## 2. O que FOI implementado (e a evidência por trás)

| Sinal | Peso | Evidência |
|---|---|---|
| **Concretude** | 22 | Hu et al. 2025, *QJEP*, PMID 41117138: 15M tweets + 50k posts Reddit + experimento causal pré-registrado — posts com linguagem mais concreta são mais compartilhados [citation:9] |
| **Facilidade de processamento** | 18 | Legibilidade, palavras curtas/frequentes, frases curtas [citation:6][citation:13] |
| **Autenticidade** | 18 | Self-disclosure, segunda pessoa, ausência de clichê de IA; autenticidade valorizada quando posts artificialmente polidos proliferam [citation:6] |
| **Gancho** | 16 | STEPPS de Jonah Berger (*Contagious*): valor prático, gatilhos, história [citation:5] |
| **Novidade** | 14 | `rapidfuzz` detecta quase-duplicata; posts repetitivos são o sinal de spam mais antigo [citation:7] |
| **Excitação segura** | 12 | Alta excitação aumenta compartilhamento [citation:5][citation:6], mas **só a positiva** é incentivada; negatividade e toxicidade são penalizadas |

Cada ponto vem com **razão em linguagem natural**. Não é caixa-preta:
`avaliar()` devolve `componentes`, `razoes[]`, `bloqueios[]`, `hook`, `score`.

## 3. Exemplos reais (rodados aqui, `docs/viral_exemplos.json`)

| Texto | Score | Bloqueios |
|---|---|---|
| Observação concreta com detalhe sensorial | **92.7** | nenhum |
| Post humano natural | **75.6** | nenhum |
| Texto abstrato / jargão | **49.4** | nenhum |
| Clickbait + pedido de curtida | **20.8** | clickbait, engagement bait |
| Clichê de IA | **14.7** | clichê de IA |

Um bloqueio multiplica o score por **0.35** e é registrado no log.

## 4. Calibração com dados REAIS — sem inventar

O bot **já mede** engajamento dos próprios posts em `core/growth.py`
(`Outcome.reach()`, `best_hooks()`, `hour_engagement`). A camada lê esses
`outcomes` e ajusta **apenas** o peso do gancho:

- `≥ VIRAL_MIN_N` (padrão 3) amostras com reach > 0 → `calibrado=True`
- menos que isso → `calibrado=False`, pesos neutros, **hint genérico**

⚠️ **Honestidade importante:** o ATProto não expõe impressões, alcance ou
CTR [citation:9]. Portanto o módulo **não afirma "melhor horário"** quando não
há `hour_engagement` suficiente (≥ 6 horas com dado). `TimingModel` devolve
`melhor_hora=None` e uma nota explicando a limitação. Nenhum benchmark de
"viralização garantida" é prometido — a pesquisa mostra que rede, credibilidade
e audiência também determinam o resultado [citation:6].

## 5. Onde a camada realmente atua (custo ZERO de LLM extra)

Três pontos de integração, todos conservadores:

1. **`core/actions.py::_growth_hint()`** — a dica que já era injetada no prompt
   agora inclui a diretriz de concretude/estrutura calibrada. Filosofia
   preservada: *"não é 'use pergunta' seco — é o modelo receber o dado e
   decidir"*.
2. **`core/actions.py::do_post()`** — o ramo de similaridade **já gerava uma
   segunda variante** e descartava a primeira. Agora `_viral_pick()` ranqueia
   as duas e fica com a melhor. **Zero chamadas adicionais ao Ollama.**
   Baseline auditável em `scripts/viral_baseline.json` prova que as contagens
   de `write_post`, `do_like`, `do_follow`, `do_repost`, `createRecord` e
   `createSession` **não mudaram**.
3. **`core/toolkit.py::prepare_post()`** — anexa `meta["viral"]` ao log
   (score, hook, bloqueios, calibrado). **Não reescreve o texto.**

`rank_variants()` nunca cria texto: só ordena variantes que o bot já gerou,
com desempate determinístico por SHA-256.

`DecisionLog` grava decisões em `data/viral_decisions.json` — **fora do git**,
sem armazenar o texto do post.

## 6. As 5 bibliotecas novas

| Lib | Versão | Uso real |
|---|---|---|
| `rapidfuzz` | 3.14.5 | Similaridade de tokens para quase-duplicata (complementa o hash exato do Outbox) |
| `croniter` | 6.2.4 | Próxima janela válida respeitando `gap_posts` e `quiet_hours` |
| `python-slugify` | 9.1.3 | Normalização de tópicos |
| `more-itertools` | 11.1.0 | Janelas deslizantes de histórico |
| `numpy` | 2.2.6 | Percentis de reach na calibração |

Somadas às 17 anteriores: **22 bibliotecas no kit** (+3 originais = 25).
Todas puras, sem modelo, sem GPU, sem download em runtime.

## 7. Kill switches e rollback

```bash
BOT_VIRAL=0      # desliga avaliação, hint e ranking
VIRAL_RANK=0     # mantém o relatório, desliga só o ranqueamento
VIRAL_MIN_N=5    # exige mais amostras para calibrar
VIRAL_FUZZY=92   # ajusta o limiar de quase-duplicata
git revert <sha> # rollback completo
```

Degradação confirmada: **sem nenhuma das libs opcionais** (é o caso do
`auth-check`, que instala só `requests`), `core.atproto`, `core.actions`,
`core.toolkit` e `core.viral` importam e funcionam.

## 8. Como usar

```python
from core.viral import avaliar, calibrar, rank_variants, TimingModel
r = avaliar("Reparei que a luz bate no teclado às 6h. Você viu?")
r["score"], r["razoes"], r["bloqueios"]

cal = calibrar(growth.outcomes)       # dados reais do próprio bot
melhor = rank_variants([texto_a, texto_b], historico=mem.recent_texts(80), cal=cal)[0]

TimingModel(growth.hour_engagement).resumo()   # calibrado? melhor_hora?
```
