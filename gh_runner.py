#!/usr/bin/env python3
"""Runner para GitHub Actions.

Sobe o agente e roda por RUN_MINUTES minutos (o teto de job do Actions é 6h,
então usamos ~5h30 e quatro horários de cron por dia iniciam a próxima sessão).

Persistência: o estado (memory.db, corpus.txt, spam_state.json, session.json)
vive no próprio repositório. Ele é commitado a cada STATE_EVERY_MIN minutos e
no final — então um job morto pelo scheduler nunca perde mais que esse janela.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "345"))
STATE_EVERY_MIN = float(os.environ.get("STATE_EVERY_MIN", "12"))
DEADLINE = time.time() + RUN_MINUTES * 60

DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


LOGFILE = DATA / "last_run.log"


def log(m: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} GH      │ {m}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        # Telemetria nao pode desaparecer em silencio: se o arquivo de log
        # estiver indisponivel, o stderr do runner ainda registra o motivo.
        print(f"[log] arquivo indisponivel: {type(e).__name__}", file=sys.stderr, flush=True)


def sh(*args: str, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 127, str(e)


def commit_state(tag: str) -> bool:
    """Commita o estado de volta no repo. Falha silenciosa — o bot vem primeiro."""
    sh("git", "config", "user.email", "bot@users.noreply.github.com")
    sh("git", "config", "user.name", "bsky-bot")
    # session.json contem accessJwt/refreshJwt. NUNCA vai para o git:
    # o branch 'state' e publico e ja vazou uma sessao valida por isso.
    # Cada run faz login fresco com a App Password (secret), sem custo real.
    keep = ["memory.db", "corpus.txt", "spam_state.json",
            "autofix_state.json", "last_run.log", "agent.log", "agency_state.json",
            "social_state.json"]
    for p in keep:
        f = DATA / p
        if f.exists():
            sh("git", "add", str(f))
    sh("git", "add", "data/seed_corpus.txt")
    rc, out = sh("git", "status", "--porcelain")
    log(f"git status rc={rc} -> {out.strip()[:200] or '(vazio)'}")
    if not out.strip():
        return False
    rc, out = sh("git", "commit", "-q", "-m",
                 f"state: {tag} [{time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())}]")
    log(f"git commit rc={rc} {out.strip()[:150]}")
    if rc != 0:
        return False
    # NUNCA empurra para main: um job antigo force-pushando main apagaria
    # arquivos de código novos (já aconteceu — core/agency.py sumiu).
    # O estado vai para o branch 'state', que só carrega data/.
    rc, err = sh("git", "push", "--force", "origin", "HEAD:refs/heads/state", timeout=180)
    log(f"git push state rc={rc} {err.strip()[:200]}")
    if rc == 0:
        log(f"estado commitado ({tag})")
        return True
    for attempt in range(3):
        rc, err = sh("git", "push", "--force", "origin", "HEAD:refs/heads/state", timeout=180)
        log(f"git push state (retry {attempt}) rc={rc} {err.strip()[:150]}")
        if rc == 0:
            return True
        time.sleep(5 * (attempt + 1))
    log("push falhou (não-fatal)")
    return False


def main() -> int:
    log(f"runner start — {RUN_MINUTES:.0f} min, deadline {time.strftime('%H:%MZ', time.gmtime(DEADLINE))}")
    log(f"cwd={ROOT} py={sys.version.split()[0]}")
    log("RunnerGH v5 (autônomo) — módulos: agency/autonomy/persona")
    log(f"data/: {sorted(p.name for p in DATA.glob('*')) if DATA.exists() else 'vazio'}")
    log(f"env handle={'ok' if os.environ.get('BSKY_HANDLE') else 'FALTANDO'} "
        f"pw={'ok' if os.environ.get('BSKY_APP_PASSWORD') else 'FALTANDO'}")

    # health antes de tudo
    from core import health, autofix
    h = health.full()
    for c in h.failed:
        log(f"health {c.name}: {c.detail}")
        if not c.ok and c.fatal:
            autofix.run()

    from core.beacon import Beacon
    from core.atproto import Bsky, AuthError
    from core.brain import Brain
    from core.antispam import SpamGuard, Limits
    from core.memory import Memory
    from core.actions import Actions, DEFAULT_QUERIES

    handle = os.environ.get("BSKY_HANDLE", "")
    apppw = os.environ.get("BSKY_APP_PASSWORD", "")
    if not handle or not apppw:
        log("ERRO: BSKY_HANDLE / BSKY_APP_PASSWORD ausentes")
        return 2

    bsky = Bsky(handle, apppw,
                pds=os.environ.get("BSKY_PDS", "https://bsky.social"),
                session_file=str(DATA / "session.json"))
    try:
        bsky.login()
    except AuthError as e:
        # 401/403 é permanente: o auto-reparo só queimaria o job inteiro.
        log("")
        log("=" * 66)
        log("CREDENCIAL INVÁLIDA — o agente não vai conseguir postar.")
        log(str(e))
        log("")
        log("Como corrigir:")
        log("  1. Entre em bsky.app com a conta do bot")
        log("  2. Settings → Privacy and Security → App Passwords")
        log("  3. Gere uma nova senha de app")
        log("  4. No GitHub: Settings → Secrets → BSKY_APP_PASSWORD")
        log("=" * 66)
        return 3
    log(f"logado como @{bsky.handle} ({bsky.did})")

    from core.adaptive import Adaptive
    AD = Adaptive(DATA / "adaptive_state.json")

    guard = SpamGuard(limits=Limits(
        day_posts=int(os.environ.get("DAY_POSTS", 20)),
        day_replies=int(os.environ.get("DAY_REPLIES", 35)),
        day_follows=int(os.environ.get("DAY_FOLLOWS", 60)),
        day_likes=int(os.environ.get("DAY_LIKES", 150)),
        day_reposts=int(os.environ.get("DAY_REPOSTS", 25))),
        tz_offset=int(os.environ.get("TZ_OFFSET", "-3")),
        state_file=DATA / "spam_state.json",
        adaptive=AD)
    guard.load()

    brain = Brain()
    mem = Memory(DATA / "memory.db")
    prior = mem.recent_texts(300)
    if prior:
        brain.offline.feed(prior)
    brain.offline._train()
    provider = brain.pick().name
    log(f"brain provider: {provider}")
    if os.environ.get("OLLAMA_MODEL"):
        log(f"OLLAMA_MODEL={os.environ['OLLAMA_MODEL']} host={os.environ.get('OLLAMA_HOST')}")
    try:
        t0 = time.time()
        sample = brain.write_post("automação e infraestrutura")
        log(f"brain sample ({time.time()-t0:.1f}s): {sample!r}")
    except Exception as e:
        log(f"brain sample falhou: {e}")

    # ── agência: estado interno que decide, não pesos fixos ──────────────────
    from core.agency import Agency
    from core.autonomy import Autonomous

    A = Agency.load(tz_offset=int(os.environ.get("TZ_OFFSET", "-3")))
    A.seed_topics(DEFAULT_QUERIES)
    log(f"agência: {A.describe()}")

    # garante que os arquivos de agência entrem no primeiro commit
    A.save()
    (DATA / "agent.log").touch()
    for f in ("social_state.json", "adaptive_state.json",
              "immune_state.json", "conductor_state.json", "growth_state.json",
              "images_state.json"):
        (DATA / f).touch()

    from core.social import Social
    from core.immune import Immune
    from core.conductor import Conductor
    from core.growth import Growth
    from core.images import Images

    IMG = Images(DATA / "images_state.json",
                 enabled=os.environ.get("IMAGES", "1") not in ("0", "", "false"))
    IMG.save()
    log(f"imagens: {'ligadas' if IMG.available() else 'desligadas'} · {IMG.describe()}")
    G = Growth(DATA / "growth_state.json")
    log(f"crescimento: {G.describe()}")
    S = Social(DATA / "social_state.json")
    IM = Immune(DATA / "immune_state.json")
    log(f"social: {S.describe()}")
    log(f"parâmetros: {AD.describe()}")

    C = Conductor(agency=A, adaptive=AD, immune=IM, social=S,
                  state_file=DATA / "conductor_state.json")
    log(f"maestro: {C.report()}")

    acts = Actions(bsky, brain, guard, mem, agency=A, social=S, growth=G, images=IMG)
    H = {"post": acts.do_post, "reply": acts.do_reply, "follow": acts.do_follow,
         "like": acts.do_like, "repost": acts.do_repost,
         "notifications": acts.do_engage_notifications, "harvest": acts.do_harvest,
         "visit": acts.do_visit, "seek": acts.do_seek, "measure": acts.do_measure,
         "image": acts.do_image}

    # DMs: NÃO entra no sorteio de ações (o motor poderia escolher 50x por
    # minuto). Vai no heartbeat, que core/autonomy.py chama 1x por step
    # dentro de try/except, com throttle interno de 15 min. O guard limita a
    # 3 respostas/24h — mesmo que o heartbeat dispare mil vezes, o teto vale.
    # Padrão: BOT_DM_REPLY=0 (desligado). Ver docs/DM.md.
    try:
        from core.dm import make_heartbeat
        dm_heartbeat = make_heartbeat(bsky, brain, interval_min=15.0)
        log("dm: modulo carregado (fail-closed, shadow por padrao)")
    except Exception as e:
        log(f"dm: modulo indisponivel ({type(e).__name__}) — seguindo sem DMs")
        dm_heartbeat = lambda: None

    # CORTEX: a "qualidade Hollywood" que e ENGENHARIA (memoria com
    # proveniencia, plano auditavel, tools tipadas, auditor separado,
    # circuit breaker). CONSULTIVO: observa, propoe, audita e registra —
    # NUNCA executa acao externa nem muda o texto publicado. Mesmo padrao
    # do heartbeat das DMs (throttle 15min, fora do sorteio de acoes).
    # Default: ligado em SHADOW, sem LLM extra. Kill switch BOT_CORTEX=0.
    try:
        from core.cortex import make_heartbeat as make_cortex_heartbeat
        cortex_heartbeat = make_cortex_heartbeat(agency=A, brain=brain,
                                                 interval_min=15.0)
        log("cortex: carregado (consultivo, shadow, zero acao externa)")
    except Exception as e:
        log(f"cortex: indisponivel ({type(e).__name__}) — seguindo sem ele")
        cortex_heartbeat = lambda: None

    def heartbeat():
        # Cada subsistema e isolado: uma excecao em DMs nao impede o cortex,
        # e nenhuma delas chega ao motor (autonomy.py ja usa try/except).
        for fn in (dm_heartbeat, cortex_heartbeat):
            try:
                fn()
            except Exception as e:
                log(f"heartbeat: {type(e).__name__} isolado")

    if os.environ.get("TEST_FORCE_ACTIONS", "").lower() in ("1", "true", "yes"):
        log("modo teste ativo: uma curtida e uma publicação")
        acts.do_like()
        acts.do_post()

    beacon = Beacon("worker", every=15).start()

    def flush():
        guard.save()
        A.save()
        S.save()
        AD.save()
        C.save()
        G.save()
        IMG.save()

    last_state = time.time()

    def maybe_commit():
        nonlocal last_state
        if time.time() - last_state > STATE_EVERY_MIN * 60:
            last_state = time.time()
            commit_state(f"tick{A.ticks}")

    # O motor chama flush() a cada 8 delibeções — mesmo quando o agente está
    # fora do app e nenhum handler roda. Sem isso o estado nunca é commitado
    # durante os longos períodos de inatividade (que agora são a maior parte).
    def flush_and_commit():
        flush()
        maybe_commit()

    commit_state("boot")          # primeiro commit: cria o branch state

    # envolve os handlers para commitar estado periodicamente
    def wrapped(kind: str):
        fn = H.get(kind)
        def _f():
            r = fn()
            maybe_commit()
            return r
        return _f

    engine = Autonomous(
        agency=A,
        handlers={k: wrapped(k) for k in H},
        feed_provider=lambda n: acts.browse(n),
        notif_check=acts.has_pending_notifications,
        on_flush=flush_and_commit,
        social=S,
        conductor=C,
        adaptive=AD,
        immune=IM,
        growth=G,
        heartbeat=heartbeat,
    )

    try:
        engine.run(until_ts=DEADLINE)
    except KeyboardInterrupt:
        pass
    except BaseException:
        log("EXCEÇÃO FATAL:\n" + traceback.format_exc()[-1500:])
        raise
    finally:
        beacon.stop()
        guard.save()
        A.save()
        S.save()
        AD.save()
        C.save()
        G.save()
        IMG.save()
        commit_state("final")
        st = mem.stats(24)
        log(f"fim — deliberações={A.ticks} stats24h={json.dumps(st, ensure_ascii=False)}")
        log(f"agência final: {A.describe()}")
        log(f"social final: {S.describe()}")
        log(f"maestro final: {C.report()}")
        log(f"parâmetros:\n{AD.report()}")
        log(f"sistema imune:\n{IM.report()}")
        log(f"crescimento:\n{G.report()}")
        log(f"imagens: {IMG.describe()}")
    return 0


if __name__ == "__main__":
    restarts = 0
    while True:
        try:
            sys.exit(main())
        except SystemExit:
            # main() encerrou de forma deliberada (sucesso ou falha tratada).
            # Sem esta cláusula, SystemExit seria capturado por BaseException
            # abaixo e o "auto-reparo" reiniciaria o agente mesmo no caminho
            # de sucesso — inclusive com credencial inválida (exit 3).
            raise
        except KeyboardInterrupt:
            sys.exit(130)
        except BaseException:
            restarts += 1
            try:
                log("EXCEÇÃO FATAL (topo):\n" + traceback.format_exc()[-1500:])
            except Exception:
                traceback.print_exc()
            if time.time() >= DEADLINE or restarts >= 8:
                raise
            delay = min(60, 10 * restarts)
            log(f"auto-reparo: reiniciando o agente em {delay}s ({restarts}/8)")
            time.sleep(delay)
