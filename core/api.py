"""Clientes HTTP das APIs públicas do Polymarket.

Fase 0 usa só endpoints públicos e de leitura — nenhuma autenticação.
Todos foram verificados respondendo 200 desta máquina.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from core import config


def _parse_maybe_json(value: Any) -> Any:
    """A Gamma API devolve alguns campos como string contendo JSON
    (ex.: outcomes='["Yes","No"]'). Normaliza para o objeto Python."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


class PolymarketAPI:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        cfg = config.load()["api"]
        self.gamma = cfg["gamma"].rstrip("/")
        self.clob = cfg["clob"].rstrip("/")
        self.data = cfg["data"].rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=cfg.get("timeout_s", 20.0),
            headers={"User-Agent": "pmlab/0.1 (research)"},
            limits=httpx.Limits(max_connections=20),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> "PolymarketAPI":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None,
                   retries: int = 3) -> Any:
        delay = 0.5
        for attempt in range(retries):
            try:
                r = await self.client.get(url, params=params)
                if r.status_code == 429:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, json.JSONDecodeError):
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
        return None

    # ---------- Gamma: catálogo ----------

    async def markets_page(self, *, limit: int = 100, offset: int = 0,
                           active: bool = True, closed: bool = False,
                           **extra: Any) -> list[dict[str, Any]]:
        """Uma página do catálogo. A Gamma rejeita offset acima de ~2100 com 422;
        tratamos isso como fim da paginação em vez de erro."""
        params: dict[str, Any] = {
            "limit": limit, "offset": offset,
            "active": str(active).lower(), "closed": str(closed).lower(),
        }
        params.update({k: v for k, v in extra.items() if v is not None})
        try:
            data = await self._get(f"{self.gamma}/markets", params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                return []
            raise
        return data if isinstance(data, list) else []

    async def events_page(self, *, limit: int = 100, offset: int = 0,
                          active: bool = True, closed: bool = False,
                          **extra: Any) -> list[dict[str, Any]]:
        """Uma página de eventos, cada um já com o array `markets` completo.

        Preferimos este endpoint ao /markets porque análise de negative-risk
        exige TODAS as pernas do evento: somar um subconjunto dos resultados
        produz desvio falso, que parece arbitragem e não é.
        """
        params: dict[str, Any] = {
            "limit": limit, "offset": offset,
            "active": str(active).lower(), "closed": str(closed).lower(),
        }
        params.update({k: v for k, v in extra.items() if v is not None})
        try:
            data = await self._get(f"{self.gamma}/events", params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                return []
            raise
        return data if isinstance(data, list) else []

    async def event(self, event_id: str | int) -> dict[str, Any] | None:
        data = await self._get(f"{self.gamma}/events", {"id": event_id})
        if isinstance(data, list):
            return data[0] if data else None
        return data

    # ---------- CLOB: livro ----------

    async def book(self, token_id: str) -> dict[str, Any] | None:
        return await self._get(f"{self.clob}/book", {"token_id": token_id})

    async def books(self, token_ids: list[str]) -> list[dict[str, Any]]:
        """Snapshot em lote — usado na auditoria periódica da série do WebSocket."""
        payload = [{"token_id": t} for t in token_ids]
        r = await self.client.post(f"{self.clob}/books", json=payload)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    # ---------- Data API: carteiras ----------

    async def wallet_trades(self, wallet: str, limit: int = 100,
                            offset: int = 0) -> list[dict[str, Any]]:
        data = await self._get(f"{self.data}/trades",
                               {"user": wallet, "limit": limit, "offset": offset})
        return data if isinstance(data, list) else []

    async def wallet_positions(self, wallet: str, limit: int = 500) -> list[dict[str, Any]]:
        data = await self._get(f"{self.data}/positions", {"user": wallet, "limit": limit})
        return data if isinstance(data, list) else []

    async def wallet_value(self, wallet: str) -> float:
        data = await self._get(f"{self.data}/value", {"user": wallet})
        if isinstance(data, list) and data:
            return float(data[0].get("value", 0.0))
        return 0.0

    async def wallet_volume(self, wallet: str) -> float:
        data = await self._get(f"{self.data}/traded", {"user": wallet})
        if isinstance(data, dict):
            return float(data.get("traded", 0.0))
        return 0.0


def best_levels(book: dict[str, Any]) -> tuple[float | None, float | None,
                                               float | None, float | None]:
    """Extrai (best_bid, bid_size, best_ask, ask_size) de um book do CLOB.

    Atenção: a API devolve bids em ordem crescente e asks em ordem decrescente,
    ou seja, o melhor preço de cada lado é o ÚLTIMO elemento da lista. Ordenamos
    explicitamente em vez de confiar nisso.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max(bids, key=lambda x: float(x["price"])) if bids else None
    best_ask = min(asks, key=lambda x: float(x["price"])) if asks else None
    return (
        float(best_bid["price"]) if best_bid else None,
        float(best_bid["size"]) if best_bid else None,
        float(best_ask["price"]) if best_ask else None,
        float(best_ask["size"]) if best_ask else None,
    )


__all__ = ["PolymarketAPI", "best_levels", "_parse_maybe_json"]
