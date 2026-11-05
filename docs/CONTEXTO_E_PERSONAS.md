# Contexto real + arquétipos de voz

**Status:** implantado e validado. **Nenhuma** chamada de rede nova em produção
por padrão. **Nenhuma** credencial envolvida.

## O problema

O bot tinha `"clima", "chuva", "calor"` entre os tópicos sorteados e pedia ao
modelo para "pensar no dia a dia real". Sem nenhuma observação, o modelo
chutava — e publicava **"tá chovendo" num dia de sol**.

A causa não era o modelo ser ruim: era o **prompt não conter nenhum fato**.
`TOPIC_RULES` dizia "o clima" como sugestão, e nada verificava o resultado.

## A regra central

> **Fato não observado não é fato.** Se não há clima confirmado, o bot não
> afirma clima nenhum. Nunca se preenche a lacuna com palpite.

## Duas camadas

| Camada | Onde | O que faz |
|---|---|---|
| **Prevenção** | `Brain.gen()` → `core/context.grounding()` | injeta hora, dia da semana, período, estação e clima (só se confirmado) no system prompt |
| **Rede** | `Actions._safe()` + gate final em `_post_text()` | barra qualquer texto inconsistente; regenera até 3× com feedback; se falhar, **não publica** |

`_safe()` é o ponto único por onde passam **posts, replies e partes de thread**.
O gate final em `_post_text()` é defesa em profundidade.

## O que é observado

| Fato | Fonte | Rede |
|---|---|---|
| hora, minuto, período | `zoneinfo` (`America/Sao_Paulo`) | nenhuma |
| dia da semana, fim de semana | `datetime.weekday()` | nenhuma |
| estação (hemisfério sul) | mês | nenhuma |
| clima | **OPCIONAL** `BOT_WEATHER=1` → Open-Meteo, timeout 5 s, TTL 30 min | 1 chamada |

`fetch_weather()` devolve **`None` em QUALQUER falha** (403, DNS, TLS, JSON
inválido, campo ausente). Falha → modo conservador. **Nunca chuta.**

> No sandbox e, se a política de saída também bloquear, no runner:
> `api.open-meteo.com` retorna **403**. Nesse caso o bot simplesmente não fala
> de clima — que é exatamente o comportamento correto.

## Inconsistências barradas

`CLIMA_NAO_VERIFICADO` · `CLIMA_CONFLITO` · `CONTRADICAO_INTERNA` ·
`PERIODO_ERRADO` · `DIA_ERRADO` · `FDS_ERRADO` · `ESTACAO_INCOMPATIVEL` ·
`SEM_SENTIDO` · `TEMPLATE_VAZOU` · `REPETICAO`

Exemplos (quinta, 14:30, primavera, sem clima observado):

| Texto | Veredito |
|---|---|
| `o sol tá lindo mas tá chovendo aqui kkkk` | ❌ contradição + clima não verificado |
| `bom dia gente` | ❌ período errado |
| `hoje é segunda` | ❌ dia errado |
| `fim de semana chegando` | ❌ fim de semana errado |
| `o café da tarde saiu bom hoje` | ✅ |

Com clima confirmado `chuva`: `tá chovendo` ✅ · `faz sol` ❌.

## Arquétipos de voz (6)

**Não são 6 pessoas diferentes.** É o mesmo `His`, 18 anos, Brasil, com
nome/idade/local e **todas** as regras base de `core/persona.py` intactas.
Só o registro, o foco e as sementes offline mudam — e mudam **uma vez por dia**,
por `SHA-256(data)`, não a cada post. Gente real muda de tom ao longo dos dias,
não de identidade a cada frase.

| id | tom |
|---|---|
| `observador` | repara em detalhe miúdo, quase não opina |
| `seco` | frases curtas, direto, sem ironia hoje |
| `curioso` | pergunta genuína, admite dúvida |
| `empolgado` | animado com coisa pequena, **sem** emoji/exclamação |
| `descontraido` | levemente mais falante, humor leve |
| `noturno` | tom baixo de fim de dia, concreto, sem filosofia |

Todos mantêm as proibições originais (sem hashtag, sem CTA, sem autoajuda,
sem "excelente reflexão"). **Nenhum conteúdo explícito, sexual, agressivo ou
sensacionalista foi adicionado** — a camada é de tom/ritmo apenas.

`PERSONA_DIA=curioso` força um arquétipo. `BOT_PERSONAS=0` volta ao `DEFAULT`.

## Correção colateral: `persist state`

As sessões **#166, #167, #168 e #170** falhavam sempre no mesmo step. Causa
encontrada lendo o workflow: `git rm --cached data/session.json` staged uma
**deleção**, e `git diff --cached --name-only | grep session.json` casava com
a própria remoção → `exit 1` → o push nunca acontecia → o arquivo continuava
versionado no branch público `state`.

Corrigido: agora checa `git ls-files --error-unmatch` (o arquivo ainda está no
índice?) e usa `--diff-filter=ACMRT` para ignorar deleções no diff.

## Variáveis

| Var | Default | Efeito |
|---|---|---|
| `BOT_CONTEXT` | `1` | `0` desliga prevenção e gate (rollback total) |
| `BOT_WEATHER` | `0` | `1` tenta Open-Meteo; falha → conservador |
| `BOT_WEATHER_TTL` | `1800` | validade do clima em segundos |
| `BOT_PERSONAS` | `1` | `0` volta à persona única |
| `PERSONA_DIA` | — | força um arquétipo |
| `BOT_TZ` | `America/Sao_Paulo` | fuso dos fatos locais |
| `TZ_OFFSET` | `-3` | fallback se `zoneinfo` indisponível |

## Testes

```bash
python scripts/context_selftest.py       # coerência (inclui o caso do sol+chuva)
python scripts/personas_selftest.py      # 6 arquétipos, determinismo, sem 18+
python scripts/context_integration.py    # PROVA: 0 createRecord para post ruim
```

CI: `Actions → context-check → Run workflow` (11 etapas, sem credencial, sem
postar nada).

## Rollback

1. `BOT_CONTEXT: "0"` + `BOT_PERSONAS: "0"` no workflow → comportamento anterior.
2. `git revert <commit>` → remove os módulos novos.
3. Os 3 arquétipos não gravam estado: nada a limpar em `data/`.

## Limitações honestas

- O validador é **regex + fatos locais**, não compreensão semântica. Ele barra
  afirmações explícitas; uma contradição muito indireta pode escapar.
- Clima real depende de `api.open-meteo.com:443` liberado. Sem isso, o bot
  **não fala de clima** — seguro, porém mais restrito.
- Estação usa limites de mês aproximados, não cálculo astronômico. Suficiente
  para barrar "nevando" em novembro.
- O corpus de testes cobre português do Brasil. Texto em outros idiomas passa
  pelas regras de "sem sentido", mas não pelas de dia/período.
