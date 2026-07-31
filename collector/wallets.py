"""Coletor de trades públicos das carteiras vigiadas.

O ponto central: gravar `ts_trade` (quando o trade aconteceu) **e** `ts_seen`
(quando eu enxerguei). A diferença é o piso do atraso de qualquer estratégia de
cópia, e é o que `analysis/copyability.py` usa para simular a cópia realista.

Não existe stream de trades por carteira, só polling REST — o atraso de
observação é inerente, não um defeito da implementação.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections import deque
from typing import Any, Iterable

from core import config
from core.api import PolymarketAPI, best_levels
from core.db import Store, now_ms

# Leaderboard fica numa API própria. Janelas aceitas: 1d, 7d, 30d, all
# (1m/1w/1h devolvem 400). Resposta: lista de {name, proxyWallet, amount}.
LEADERBOARD_URL = "https://lb-api.polymarket.com/profit"
LEADERBOARD_WINDOWS = ("1d", "7d", "30d", "all")


def trade_uid(t: dict[str, Any]) -> str:
    """Chave estável de deduplicação.

    O transactionHash sozinho não serve: uma transação agrupa vários fills, e a
    API repete os mesmos trades a cada poll. Combinamos os campos que definem o
    fill unicamente.
    """
    parts = (str(t.get("transactionHash")), str(t.get("proxyWallet")), str(t.get("asset")),
             str(t.get("side")), f"{float(t.get('size', 0)):.6f}",
             f"{float(t.get('price', 0)):.6f}", str(t.get("timestamp")))
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def to_row(t: dict[str, Any], ts_seen: int, is_backfill: bool) -> tuple[Any, ...]:
    size = float(t.get("size") or 0.0)
    price = float(t.get("price") or 0.0)
    # A Data API devolve timestamp em segundos; o resto do projeto usa ms.
    ts_trade = int(float(t.get("timestamp") or 0)) * 1000
    return (
        trade_uid(t), t.get("proxyWallet"), t.get("name"), t.get("asset"),
        t.get("conditionId"), t.get("side"), size, price, size * price,
        ts_trade, ts_seen, is_backfill, t.get("title"), t.get("outcome"),
        t.get("slug"), t.get("transactionHash"),
    )


class WalletCollector:
    def __init__(self, store: Store, wallets: Iterable[str] | None = None) -> None:
        cfg = config.load()["wallets"]
        self.store = store
        self.cfg = cfg
        self.wallets = list(dict.fromkeys(wallets or cfg.get("seed", [])))
        self.trades_seen = 0
        self.trades_novos = 0
        self._discovered = False
        self._primed: set[str] = set()
        # Cada poll devolve os mesmos últimos 100 trades. Sem um filtro em
        # memória, mandaríamos ~100 linhas por carteira por poll para o banco só
        # para o INSERT OR IGNORE descartar. Guardamos uma janela de uids já
        # vistos por carteira.
        self._recent: dict[str, deque[str]] = {}
        self._recent_set: dict[str, set[str]] = {}
        self._recent_max = 400
        # Fila de tokens para sondar o livro assim que um trade novo aparece.
        # As carteiras vigiadas negociam o mercado inteiro, enquanto o
        # WebSocket cobre só os 400 tokens do catálogo — sem isso a cobertura
        # da análise de cópia fica em ~2%. E a sondagem sob demanda é
        # justamente o caminho real de quem copia: vejo o trade, consulto o
        # livro, executo.
        self._probe_q: asyncio.Queue[str] = asyncio.Queue(maxsize=2000)
        self.probes = 0

    def _is_new(self, wallet: str, uid: str) -> bool:
        seen = self._recent_set.setdefault(wallet, set())
        if uid in seen:
            return False
        order = self._recent.setdefault(wallet, deque())
        seen.add(uid)
        order.append(uid)
        if len(order) > self._recent_max:
            seen.discard(order.popleft())
        return True

    async def discover(self, api: PolymarketAPI) -> None:
        """Acrescenta as carteiras mais lucrativas do leaderboard às vigiadas."""
        top_n = int(self.cfg.get("top_n_from_leaderboard", 0))
        if top_n <= 0 or self._discovered:
            return
        self._discovered = True
        window = self.cfg.get("leaderboard_window", "30d")
        if window not in LEADERBOARD_WINDOWS:
            window = "30d"
        try:
            r = await api.client.get(LEADERBOARD_URL,
                                     params={"window": window, "limit": top_n})
            r.raise_for_status()
            rows = r.json()
            if not isinstance(rows, list):
                rows = rows.get("data", [])
            novos = []
            for row in rows:
                w = row.get("proxyWallet")
                if w and w not in self.wallets:
                    self.wallets.append(w)
                    novos.append({"nome": row.get("name"), "lucro": row.get("amount")})
            self.store.log("wallets", "info", f"leaderboard {window}",
                           {"novos": len(novos), "total": len(self.wallets),
                            "top3": novos[:3]})
        except Exception as exc:
            # Leaderboard é conveniência; as carteiras semente já bastam.
            self.store.log("wallets", "warn", "leaderboard indisponivel",
                           {"erro": repr(exc)[:300]})

    async def _poll_one(self, api: PolymarketAPI, wallet: str) -> None:
        trades = await api.wallet_trades(wallet, limit=100)
        ts_seen = now_ms()
        # O primeiro poll traz os últimos 100 trades de uma vez — histórico, não
        # descoberta. Marcamos para não poluir a medição de atraso.
        backfill = wallet not in self._primed
        self._primed.add(wallet)
        for t in trades:
            if self._is_new(wallet, trade_uid(t)):
                self.store.add("wallet_trades", to_row(t, ts_seen, backfill))
                self.trades_novos += 1
                # Backfill é histórico: sondar o livro agora não diria nada
                # sobre o que era possível na época.
                if not backfill and t.get("asset"):
                    with contextlib.suppress(asyncio.QueueFull):
                        self._probe_q.put_nowait(str(t["asset"]))
        self.trades_seen += len(trades)

    async def _probe_worker(self, api: PolymarketAPI) -> None:
        """Busca o livro dos tokens que acabaram de aparecer num trade novo."""
        while True:
            token_id = await self._probe_q.get()
            try:
                book = await api.book(token_id)
                if book:
                    ts_local = now_ms()
                    bid, bid_sz, ask, ask_sz = best_levels(book)
                    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
                    spread = (ask - bid) if bid is not None and ask is not None else None
                    ts_ex = int(float(book.get("timestamp") or ts_local))
                    self.store.add("book_top", (token_id, ts_ex, ts_local, bid, bid_sz,
                                                ask, ask_sz, mid, spread, "copy_probe"))
                    self.probes += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.log("wallets", "warn", "sondagem de livro falhou",
                               {"token": token_id[:20], "erro": repr(exc)[:200]})
            finally:
                self._probe_q.task_done()

    async def run(self) -> None:
        interval = float(self.cfg.get("poll_interval_s", 3.0))
        n_workers = int(self.cfg.get("probe_workers", 4))
        async with PolymarketAPI() as api:
            await self.discover(api)
            workers = [asyncio.create_task(self._probe_worker(api))
                       for _ in range(n_workers)]
            try:
                while True:
                    try:
                        await asyncio.gather(
                            *[self._poll_one(api, w) for w in self.wallets],
                            return_exceptions=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.store.log("wallets", "warn", "poll falhou",
                                       {"erro": repr(exc)[:300]})
                    await asyncio.sleep(interval)
            finally:
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
