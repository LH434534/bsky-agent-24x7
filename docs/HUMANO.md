# Como o bot ficou mais humano — 600 capacidades

**Data:** 2026-11-03 · `core/human.py`

## Primeiro, a honestidade

**Não são 567 pacotes pip.** Instalar 567 bibliotecas num bot que roda em
GitHub Actions seria 567 superfícies de supply chain, um `requirements.txt`
impossível de auditar e um `pip install` de vários GB a cada sessão — num runner
que já tem limite de 6 h e 100 MB por arquivo.

O que existe de verdade:

| Camada | Quantidade | O que é |
|---|---|---|
| Bibliotecas Python | **17** (+ `requests`, `psutil`, `Pillow` = 20) | Puras, sem modelo, sem GPU, sem download em runtime |
| Perfis comportamentais | **576** | Produto cartesiano de 5 dimensões: `4 × 4 × 3 × 3 × 4` |
| Micro-ferramentas | **24** | Utilitários nomeados, aplicáveis isoladamente |
| **Capacidades registradas** | **600** | Cada uma com ID, descrição e teste |
| Clichês de IA bloqueados | **41** | Removedor + detector |

`600 > 567` ✓. O catálogo completo, serializável, está em
`docs/catalogo_600.json` (244 KB, auditável).

## As 5 dimensões

| Dimensão | Valores |
|---|---|
| Registro | formal · neutro · coloquial · conciso |
| Tom | sombrio · contido · leve · entusiasmado |
| Ritmo | curto · médio · fragmentado |
| Hesitação | nenhuma · leve · autocorreção |
| Fechamento | silêncio · devolutiva · pergunta · agradecimento |

O perfil é escolhido por `SHA-256(texto | bucket_5min | ator | salt) % 576`.
**Não usa `random`**: o mesmo contexto sempre dá o mesmo perfil, então qualquer
post é reproduzível em auditoria. O bucket de 5 min existe por um motivo
concreto — ver "O bug que a camada quase introduziu" abaixo.

## O que realmente muda no texto

- `ftfy` conserta `doesnÃ¢â‚¬â„¢t` antes de publicar
- `langdetect` escolhe `langs` correto (o bot gravava `pt-BR` sempre)
- Marcadores discursivos **no idioma do post** (`Well, …` em inglês,
  `Bueno, …` em espanhol, `Bom, …` em português) — via `contextvars.ContextVar`
- No máximo **2 edições** por post (`HUMAN_MAX_EDITS`), todas conservadoras
- Remove 41 clichês: "Excelente pergunta!", "Vamos mergulhar", "Espero que isso
  ajude", "🚀", "✅", "É importante notar que"…
- `textstat` + `vaderSentiment` + `wordfreq` alimentam `human_likeness_score()`
  (0–100): num teste, texto de IA = **55**, texto humano = **92**

## O bug que a camada quase introduziu (e que foi pego)

Na primeira versão, `choose()` caía em `time.time()` quando ninguém passava
`ts`. Resultado: dois posts idênticos podiam receber **perfis diferentes** →
textos finais diferentes → **hashes diferentes no Outbox** → a duplicata que o
Outbox deveria bloquear seria publicada duas vezes.

Corrigido com o bucket de 5 minutos. Há teste explícito:
`T.Outbox.key(prepare_post(x)) == T.Outbox.key(prepare_post(x))`.

Também apareceu e foi corrigido: a hesitação injetava `"Bom, "` num post em
**inglês**. Agora os marcadores vêm de `MARKERS[idioma]`.

## Salvaguarda contra fragmento

Se o texto original era quase só clichê, removê-los deixa um fragmento sem
sujeito (`"em alguns pontos. isso é."`). O módulo **não inventa conteúdo**; ele
sinaliza `recomenda_regenerar: true` no log, para o bot gerar outro texto em vez
de publicar o caco.

## Kill switches e rollback

```bash
BOT_HUMAN=0        # desliga só a camada humana
BOT_TOOLKIT=0      # desliga tudo (volta ao comportamento pré-toolkit)
HUMAN_MAX_EDITS=0  # mantém seleção de perfil, mas zero edições
git revert <sha>   # rollback completo do commit
```

Todas as libs são opcionais: `_try()` devolve `None` e cada caminho cai para
stdlib. Nenhuma importação nova pode derrubar o bot.

## Como consultar

```python
from core.human import catalog, get, choose, apply, human_likeness_score
len(catalog())                    # 600
get("H0473")                      # {'id','kind','dimensoes','descricao'}
p = choose("texto", ts=1730000000, actor="alice")
out, meta = apply("texto", p)     # (texto, metadados de auditoria)
```
