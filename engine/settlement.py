"""Liquidação de posições quando o mercado resolve.

No Polymarket todo mercado termina. O jogo acaba e cada cota vira **$1 ou $0**
— é o fechamento definitivo, que não depende de encontrar contraparte.

Sem modelar isso, a simulação deixa toda posição pendurada como "não realizado"
para sempre, mesmo depois do jogo ter acabado. Foi exatamente o que se observou:
centenas de execuções e ZERO operações fechadas.

Fonte da verdade: `GET https://clob.polymarket.com/markets/{condition_id}`, que
devolve `closed` e, para cada token, `winner` e `price` (0 ou 1).
"""

from __future__ import annotations

import asyncio
from typing import Iterable

import httpx

from core.api import PolymarketAPI
from core.db import now_ms


async def preco_final(api: PolymarketAPI, condition_id: str) -> dict[str, float] | None:
    """Preço de liquidação por token, ou None se o mercado ainda não resolveu."""
    try:
        r = await api.client.get(f"{api.clob}/markets/{condition_id}")
        r.raise_for_status()
        d = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not d.get("closed"):
        return None
    precos: dict[str, float] = {}
    for t in d.get("tokens") or []:
        tid = t.get("token_id")
        if tid is None:
            continue
        # `price` já vem 0 ou 1 no mercado resolvido; `winner` é o reforço.
        try:
            precos[str(tid)] = float(t.get("price", 1.0 if t.get("winner") else 0.0))
        except (TypeError, ValueError):
            precos[str(tid)] = 1.0 if t.get("winner") else 0.0
    return precos or None


def tokens_abertos(motores: Iterable) -> set[str]:
    abertos: set[str] = set()
    for mot in motores:
        abertos.update(mot.ledger.posicoes())
    return abertos


async def liquidar_resolvidos(store, motores: list, limite: int = 60) -> int:
    """Fecha, ao preço de resolução, toda posição cujo mercado já terminou.

    Devolve quantas posições foram liquidadas.
    """
    abertos = tokens_abertos(motores)
    if not abertos:
        return 0

    # O catálogo guarda o histórico completo, então o condition_id continua
    # disponível mesmo depois do mercado sair da seleção ativa.
    marks = ", ".join("?" * len(abertos))
    linhas = store.execute(
        f"SELECT token_id, condition_id FROM markets WHERE token_id IN ({marks})",
        list(abertos)).fetchall()
    por_condicao: dict[str, list[str]] = {}
    for token_id, cond in linhas:
        if cond:
            por_condicao.setdefault(cond, []).append(token_id)
    if not por_condicao:
        return 0

    liquidadas = 0
    async with PolymarketAPI() as api:
        for cond in list(por_condicao)[:limite]:
            precos = await preco_final(api, cond)
            if not precos:
                continue
            ts = now_ms()
            for token_id in por_condicao[cond]:
                final = precos.get(token_id)
                if final is None:
                    continue
                for mot in motores:
                    if abs(mot.ledger.posicao(token_id)) > 1e-9:
                        mot.liquidar(token_id, final, ts)
                        liquidadas += 1
            await asyncio.sleep(0.05)   # gentileza com a API

    if liquidadas:
        store.log("paper", "info", "posicoes liquidadas por resolucao",
                  {"quantidade": liquidadas})
    return liquidadas
