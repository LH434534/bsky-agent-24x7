# Auditoria de disponibilidade — 2026-11-03

**STATUS: CONCLUÍDO (com um limite honesto)**
**Commits:** `27ceb0aab7c5` (revogação) · `8a7cd7f68d` (remoção + `.gitignore`) · `80981b6875a5` (robustez) · **`4645d3e1eb1`** (correções do CI)
**`main`:** `4645d3e1eb155d054428343b1a6fc2dbae33a8df`

---

## 0. Sobre "10000% sem bugs e rodando 1000%"

Não existe sistema com zero bugs nem 100% de uptime garantido — e eu não vou
fingir que entreguei isso. O que dá para fazer, e foi feito: **auditar tudo,
corrigir o que é comprovável, travar cada correção com teste e dizer
exatamente o que ainda não está coberto**.

Resultado: **264 asserts verdes**, 2 gates, 50 arquivos compilando, 9 workflows
válidos, e **um incidente de segurança real contido**.

---

## 1. 🔴 O achado mais grave: credencial viva e pública

Enquanto procurava bugs, encontrei isto:

| | |
|---|---|
| Repo | **público** (`private: false`) |
| `.gitignore` | **NÃO EXISTIA** |
| `data/session.json` no `main` desde | **2026-10-04** |
| Leitura anônima | **HTTP 200** (sem Authorization) |
| `access` JWT | expirado (05/10) |
| **`refresh` JWT** | **válido até 2027-01-02** |

Ou seja: qualquer pessoa podia ler o refresh, renovar a sessão e **postar como
`@hisbot.bsky.social`** sem saber a App Password. O mesmo arquivo estava em
`state` e `authdiag`.

**Causa raiz:** sem `.gitignore`, o `git add data/` do step `persist state`
 sempre incluía `session.json`. A correção anterior em `gh_runner.py` (remover
 `git add -f` e tirar o arquivo do `keep`) foi **necessária mas insuficiente**.

**Contenção (confirmada no runner do GitHub, que tem saída real para o PDS):**

```
[main]     deleteSession via refresh -> HTTP 200 | refresh pós-revogação -> HTTP 400
[state]    deleteSession via refresh -> HTTP 200 | refresh pós-revogação -> HTTP 400
[authdiag] deleteSession via refresh -> HTTP 200 | refresh pós-revogação -> HTTP 400
REVOGADO: sessões vazadas invalidadas e confirmadas
```

Depois: arquivo **removido dos 3 branches** (`HTTP 404` anônimo), `.gitignore`
criado, e `persist state` agora **aborta o push** se algum `session.json`,
`*.jwt` ou `.env` aparecer no stage — defesa em profundidade caso o ignore
seja apagado.

⚠️ O **histórico git** ainda contém o token. Ele está **revogado e inútil**,
mas reescrever o histórico é destrutivo e não foi feito sem autorização.

**Prevenção permanente:** `scripts/secret_gate.py` roda no CI e falha se
encontrar JWT, `ghp_`, App Password `xxxx-xxxx-xxxx-xxxx`, chave PEM ou
arquivo proibido em qualquer arquivo versionado.

---

## 2. 🟠 A causa real de o bot ter ficado parado ~8h38

Entre #155 (02/11 15:32Z) e #156 (03/11 00:10Z, **disparo meu, manual**) não
houve nenhuma sessão. Evidência levantada pela API:

| Workflow | `state` | Último run `schedule` |
|---|---|---|
| `bsky-bot-24x7` | `active` | **02/11 15:32Z** |
| `watchdog-24x7` | `active` | **nunca** (só `workflow_dispatch` #1) |
| `watchdog-prime` | `active` | **nunca** (só `workflow_dispatch` #1) |

O cron `7 0 * * *` de hoje (00:07Z) **não disparou**. Todos os workflows estão
`active`, então não é desativação. A documentação do GitHub confirma que
schedules atrasam ou são pulados sob carga, e desaconselha depender deles para
SLA [citation:4]. **Não consigo provar a causa raiz pela API** (o GitHub não
expõe `next_run`), então não invento uma.

### A correção que resolve de verdade

O job tinha `timeout-minutes: 350` e a cadeia de renovação vivia num step
`if: always()` **no fim**. Se o job estourasse o timeout, esses steps **não
rodariam** e a cadeia morreria — restando só o cron, que é justamente o que
falhou.

Mudança de 2 linhas, baseada no comportamento documentado de `timeout-minutes`
em nível de **step** [citation:5]:

```yaml
job:  timeout-minutes: 350
step: run agent:      timeout-minutes: 320   # ← NOVO
      RUN_MINUTES: 300                       # era 330
```

Se o Python travar, **só o step é cancelado aos 320 min**; o job continua e
os steps `persist state` + `renovar sessao` (`if: always()`) **rodam
normalmente**. Folga de 20 min dentro do step + 30 min para os steps finais.

Complementos:
- `renovar sessao` ganhou `timeout-minutes: 6` e **`--retries 3
  --retry-delay 45`** — falha transitória da API não depende mais do cron.
- Crons dos watchdogs movidos para minutos **fora do pico** (`3,18,33,48` em
  vez de `*/15`), onde o atraso médio é menor [citation:4].

---

## 3. Todas as correções aplicadas

| # | Arquivo | Correção |
|---|---|---|
| 1 | `.gitignore` | **criado** — `session.json`, `*.jwt`, `.env`, `__pycache__` |
| 2 | 3 branches | `data/session.json` removido |
| 3 | `scripts/revoke_all_sessions.py` | **novo** — varre `main`/`state`/`authdiag`, não só `state` |
| 4 | `scripts/secret_gate.py` | **novo** — gate permanente anti-vazamento no CI |
| 5 | `bsky-bot-24x7.yml` | step timeout 320 · RUN_MINUTES 300 · retry na cadeia · bloqueio de segredo no push |
| 6 | `scripts/chain.py` | `--retries` / `--retry-delay` com teto de tempo |
| 7 | `watchdog.yml` | cron fora do pico |
| 8 | `immortal.py` | `timeout=10` no `pgrep` (único subprocess sem timeout) |
| 9 | `gh_runner.py` | `log()` não engole falha em silêncio — avisa no stderr |
| 10 | `core/scheduler.py` | remove `t0`/`spent` (código morto que sugeria compensação inexistente) + imports mortos |
| 11 | `main.py`, `immortal.py` | imports não usados removidos |
| 12 | `scripts/check_auth.py` | f-string sem placeholder corrigida |
| 13 | `scripts/resilience_selftest.py` | **novo** — 52 asserts, mock real da API do GitHub |
| 14 | `toolkit-check.yml` | 5 → **7 etapas**, novos triggers |

**Bug que eu mesmo introduzi e corrigi:** ao limpar imports, removi
`core.beacon.read` de `immortal.py` sem ver que ele é usado na linha 74. O
smoke de import de 26 módulos pegou o erro; restaurado. A suíte agora trava
isso para sempre.

---

## 4. Evidências

| Teste | Resultado |
|---|---|
| `toolkit_selftest` | **50 / 50** |
| `toolkit_integration` | **15 / 15** |
| `human_selftest` | **41 / 41** |
| `viral_selftest` | **106 / 106** |
| `resilience_selftest` | **52 / 52** |
| **Total** | **264 / 264** |
| `secret_gate.py` | **OK** (0 achados) |
| `security_gate.py` | **OK** (3 pendentes `curl\|sh`) |
| `py_compile` | **50 arquivos, 0 falhas** |
| YAML | **9 workflows válidos** |
| Estados corrompidos | **8/8 loaders sobrevivem** a JSON truncado |
| Imports | **26/26 módulos** |
| Subprocess/HTTP sem timeout | **0** |
| `chain.py` contra API mock | 7 decisões: DESLIGADO, CANCELADO, AGUARDANDO, ATIVO, DISPARADO, retry 503→DISPARADO, 500→ERRO |
| `security-revoke` #4 no runner | **success** — 3 sessões revogadas |
| `auth-check` #12 (PDS real) | **success** — App Password intacta |
| `toolkit-check` #6 no runner | **success, 7/7 etapas** (após correção do #5) |

---

## 5. O que AINDA não está coberto (limite honesto)

1. **Cancelamento manual ou perda do runner:** se o job inteiro for morto
   (não o step), `if: always()` não roda. A retomada depende dos cron dos
   watchdogs — que, como comprovado, **não dispararam**. Para 100% real é
   preciso um **disparador externo** (cron-job.org, Cloudflare Workers,
   Better Stack, VPS com systemd), o que exige conta/credencial que você não
   forneceu [citation:4]. Guia pronto em `deploy/cron-externo.md`.
2. **Histórico git** com o token revogado — inofensivo, mas presente.
3. **3 `curl \| sh`** no bootstrap seguem **PENDENTES**, sem checksum. Não
   foram alterados (mudariam produção sem checksum oficial confirmado).
4. **#156** continua no sha antigo (`36d8ef425e`). Todas as correções valem a
   partir do **#157**, que nasce quando #156 terminar. Não cancelei nada sem
   autorização.
5. **`refresh` de sessões futuras:** o `.gitignore` impede o versionamento, mas
   o ideal é não gravar `session.json` em disco — o `core/atproto.py` já faz
   login fresco a cada run.


---

## 8. Pós-auditoria: o CI #5 falhou — e isso revelou mais 3 bugs

O `toolkit-check` #5 deu **failure na etapa 6/7** (o `secret_gate` que eu
mesmo tinha acabado de criar). Em vez de ignorar ou afrouxar, investiguei.
Foram **4 camadas de achados**, todos reais:

### 8.1 Falso positivo do gate (causa imediata)
`deploy/.env.example` é um **template** com `BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx`;
`docs/AUDITORIA_100.md` e a docstring do próprio gate citam esse formato.
O regex acusava placeholder documental como se fosse credencial.

Correção: `TEMPLATE_OK` para `.env.example/.sample/.template`, `segredo_real()`
descarta repetições e palavras óbvias, e fixtures de teste só são ignorados
quando anotados com `# gate:fixture`. Um `.env` real e um `ghp_` real
**continuam bloqueados** — há testes que injetam ambos e exigem `rc=1`.

### 8.2 Falso NEGATIVO silencioso (mais grave que o falso positivo)
`git ls-files` num diretório que não é repositório devolve **rc=128 com stdout
vazio**. O gate checava só `try/except` e devolvia `OK` **sem varrer nenhum
arquivo**. Localmente parecia verde por razão errada. Agora valida o
`returncode` e cai para `rglob` — o que imediatamente expôs os fixtures.

### 8.3 `langdetect` é estocástico → flakiness real
Sem `DetectorFactory.seed`, o mesmo texto oscilava **pt-BR / sv em 90/10** das
chamadas. O `human_selftest` falhava intermitentemente. Correção: seed fixo
`0`, e detecção não confiável em textos com <3 palavras cai para `pt-BR`.
**200/200 rodadas determinísticas** após a correção.

### 8.4 A camada humana conseguia destruir um post
Ao remover clichês de um texto que era só clichê, `apply_human` devolvia
`"Bom,!."` — **6 caracteres, zero conteúdo**, que chegaria à publicação e
alimentava o `sv` do item anterior. Guarda anti-degeneração em `prepare_post`:
se as edições deixarem <2 palavras, removerem >40% do conteúdo ou o texto
começar com pontuação órfã, **as edições são revertidas** e o original limpo
é preservado, com `recomenda_regenerar=True`. Texto normal continua sendo
editado (100/100 rodadas sem reversão).

> Nota de ambiente: no meio da validação o sandbox reiniciou e perdeu os
> pacotes pip instalados na sessão, derrubando várias suítes de uma vez.
> Reinstalado via `requirements.txt` e revalidado. O CI do GitHub instala as
> dependências em todo run, então não é afetado — e é a fonte de verdade.

**Bug que eu introduzi e corrigi nesta rodada:** removi `core.beacon.read` de
`immortal.py` ao limpar imports (usado na linha 74); o smoke de 26 módulos
pegou. Também esqueci `import re` num teste novo. Ambos travados por testes.

---

## 6. Rollback

```bash
git revert <sha_final>          # robustez
git revert 8a7cd7f68d           # remoção + .gitignore (NÃO recomendado)
BOT_CHAIN=0                    # para a cadeia
git revert 27ceb0aab7c         # revogação (NÃO recomendado)
```

A revogação **não tem rollback**: o token era público e foi invalidado no PDS.
O bot continua autenticando pela App Password do secret.

---

## 7. Próxima ação mínima

Acompanhar **#156 → #157**. Se #157 não nascer sozinho, o problema é o
escalonamento externo — e aí a solução é o cron externo, não mais código.
