"""Fumaça de imagem — roda no runner, que tem internet de verdade.

O sandbox onde eu escrevo o código não tem saída pra nada além da API do
GitHub, então não consigo provar aqui que a Pollinations responde. Este script
roda dentro do Actions e grava o resultado no log: se o provedor real funcionar,
a gente vê.

Não falha o job se não funcionar — imagem é enriquecimento, não dependência.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.images import (Images, image_prompt, looks_like_image,   # noqa: E402
                         compress, PROVIDERS)


def log(m: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} IMG-SMOKE │ {m}"
    print(line, flush=True)
    try:
        # vai no agent.log, que é commitado no branch state — assim dá pra ler
        # o resultado do provedor real sem precisar baixar log do Actions
        with open("/data/workspace/bsky_agent/data/agent.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main() -> int:
    log("começando teste de fumaça (rede livre do runner)")
    I = Images(Path("/data/workspace/bsky_agent/data/images_state.json"))
    I.enabled = True
    I.min_gap = 0

    prompt = image_prompt("a cidade de manhã com neblina, luz baixa")
    log(f"prompt: {prompt[:100]}")

    for name in I.order():
        fn = dict(PROVIDERS)[name]
        t0 = time.time()
        try:
            raw = fn(prompt, 4242, 1024, 1024)
        except Exception as e:
            log(f"{name}: EXCEÇÃO {str(e)[:90]}")
            I.note(name, False)
            continue
        dt = time.time() - t0
        if not raw:
            log(f"{name}: sem resposta ({dt:.0f}s)")
            I.note(name, False)
            continue
        mime = looks_like_image(raw)
        if not mime:
            log(f"{name}: resposta não é imagem — {raw[:80]!r}")
            I.note(name, False)
            continue
        d, m = compress(raw, mime)
        log(f"{name}: OK {len(d)//1024} KB em {dt:.0f}s ({m})")
        I.note(name, True)
        log(f"ordem agora: {I.order()}")
        I.save()
        return 0

    log("nenhum provedor respondeu — offline também falhou (improvável)")
    I.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
