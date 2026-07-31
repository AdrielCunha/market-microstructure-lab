"""Catálogo de mercados: decide QUAIS tokens vale a pena monitorar.

O universo do Polymarket é grande demais (dezenas de milhares de mercados) para
assinar tudo. Este módulo pagina a Gamma API, aplica os filtros de config.toml e
grava o subconjunto observável em `markets`.

Rodar direto:
    python -m collector.catalog
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from core import config
from core.api import PolymarketAPI, _parse_maybe_json
from core.db import Store, connect

COLUMNS = [
    "token_id", "condition_id", "event_id", "event_slug", "event_title", "question",
    "outcome", "outcome_index", "neg_risk", "neg_risk_market", "event_n_outcomes",
    "category", "end_date",
    "liquidity_usd", "volume_usd", "min_tick_size", "order_min_size",
    "fee_type", "fee_rate", "fee_exponent", "fee_rebate_rate", "fee_taker_only",
    "fees_enabled", "active", "closed", "accepting_orders", "selected",
    "seen_at", "updated_at",
]

# A Gamma não devolve categoria nem tags nos mercados. Derivamos um rótulo do
# que de fato importa para a economia da estratégia: o regime de taxa (esportes
# têm taker fee + rebate de maker) e, dentro de esportes, a liga (prefixo do
# slug do evento, ex.: "ucl-kai-omo-2026-07-29").
def derive_category(fee_type: str | None, event_slug: str | None) -> str:
    if fee_type and "sports" in fee_type.lower():
        league = (event_slug or "").split("-", 1)[0].upper()
        return f"Sports/{league}" if league.isalpha() and len(league) <= 5 else "Sports/outro"
    return "NaoEsporte"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def explode_market(m: dict[str, Any], ev: dict[str, Any] | None = None,
                   n_outcomes: int | None = None) -> list[dict[str, Any]]:
    """Um mercado binário da Gamma vira uma linha por token (Yes e No)."""
    token_ids = _parse_maybe_json(m.get("clobTokenIds")) or []
    outcomes = _parse_maybe_json(m.get("outcomes")) or []
    if not isinstance(token_ids, list) or not token_ids:
        return []

    if ev is None:
        events = m.get("events") or []
        ev = events[0] if events else {}
    fee = m.get("feeSchedule") or {}
    now = datetime.now(timezone.utc)

    rows = []
    for idx, token_id in enumerate(token_ids):
        rows.append({
            "token_id": str(token_id),
            "condition_id": m.get("conditionId"),
            "event_id": str(ev.get("id")) if ev.get("id") else None,
            "event_slug": ev.get("slug"),
            "event_title": ev.get("title"),
            "question": m.get("question"),
            "outcome": outcomes[idx] if idx < len(outcomes) else None,
            "outcome_index": idx,
            "neg_risk": bool(m.get("negRisk")),
            "neg_risk_market": m.get("negRiskMarketID"),
            "event_n_outcomes": n_outcomes,
            "category": derive_category(m.get("feeType"), ev.get("slug")),
            "end_date": _ts(m.get("endDate")),
            "liquidity_usd": _num(m.get("liquidityNum") or m.get("liquidity")),
            "volume_usd": _num(m.get("volumeNum") or m.get("volume")),
            "min_tick_size": _num(m.get("orderPriceMinTickSize"), 0.01),
            "order_min_size": _num(m.get("orderMinSize"), 0.0),
            "fee_type": m.get("feeType"),
            "fee_rate": _num(fee.get("rate")),
            "fee_exponent": _num(fee.get("exponent"), 1.0),
            "fee_rebate_rate": _num(fee.get("rebateRate")),
            "fee_taker_only": bool(fee.get("takerOnly")),
            "fees_enabled": bool(m.get("feesEnabled")),
            "active": bool(m.get("active")),
            "closed": bool(m.get("closed")),
            "accepting_orders": bool(m.get("acceptingOrders")),
            "selected": False,   # definido no refresh
            "seen_at": now,
            "updated_at": now,
        })
    return rows


def passes_filters(row: dict[str, Any], cfg: dict[str, Any], now: datetime) -> bool:
    if row["closed"] or not row["active"] or not row["accepting_orders"]:
        return False
    if row["liquidity_usd"] < cfg.get("min_liquidity_usd", 0.0):
        return False
    end = row["end_date"]
    if end is not None:
        horizon = now + timedelta(days=cfg.get("max_days_to_resolution", 3650))
        if end > horizon or end < now:
            return False
    cats = cfg.get("categories") or []
    if cats and row["category"] not in cats:
        return False
    return True


# A Gamma API silenciosamente limita `limit` a 100 e rejeita offset > ~2100.
# Por isso os filtros pesados (liquidez e data) vão no servidor, não no cliente.
GAMMA_PAGE_SIZE = 100
GAMMA_MAX_OFFSET = 2000


async def fetch_events(api: PolymarketAPI, cfg: dict[str, Any],
                       now: datetime) -> list[dict[str, Any]]:
    """Pagina EVENTOS (não mercados), já filtrados na origem.

    A seleção é por evento inteiro de propósito: filtrar mercado a mercado por
    liquidez descartaria pernas fracas de um evento negative-risk, e somar um
    subconjunto dos resultados produz desvio falso.
    """
    horizon = now + timedelta(days=cfg.get("max_days_to_resolution", 3650))
    server_filters = {
        "liquidity_min": cfg.get("min_liquidity_usd"),
        "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date_max": horizon.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "order": "liquidity",
        "ascending": "false",
    }
    out: list[dict[str, Any]] = []
    offset = 0
    while offset <= GAMMA_MAX_OFFSET and len(out) < cfg.get("max_events_scan", 600):
        batch = await api.events_page(limit=GAMMA_PAGE_SIZE, offset=offset,
                                      active=True, closed=False, **server_filters)
        if not batch:
            break
        out.extend(batch)
        offset += len(batch)
    return out


def selecionar_eventos(eventos: list[dict[str, Any]], cfg: dict[str, Any],
                       now: datetime) -> list[dict[str, Any]]:
    """Escolhe eventos inteiros até esgotar o orçamento de tokens.

    Um evento entra com TODAS as suas pernas ou não entra. Eventos gigantes
    (ex.: 128 candidatos a indicado presidencial) são cortados por
    `max_outcomes_per_event`: sozinhos consumiriam o orçamento inteiro.
    """
    max_tokens = int(cfg.get("max_tokens", 400))
    max_outcomes = int(cfg.get("max_outcomes_per_event", 12))

    candidatos: list[tuple[float, list[dict[str, Any]]]] = []
    for ev in eventos:
        mercados = ev.get("markets") or []
        if not mercados or len(mercados) > max_outcomes:
            continue
        n = len(mercados)
        linhas = [r for m in mercados for r in explode_market(m, ev, n)]
        if not linhas or not all(passes_filters(r, cfg, now) for r in linhas):
            continue  # evento incompleto ou parcialmente fechado: descarta inteiro
        candidatos.append((_num(ev.get("liquidity")), linhas))

    candidatos.sort(key=lambda x: x[0], reverse=True)
    escolhidos: list[dict[str, Any]] = []
    for _, linhas in candidatos:
        if len(escolhidos) + len(linhas) > max_tokens:
            continue
        escolhidos.extend(linhas)
    return escolhidos


async def refresh(store: Store | None = None) -> list[str]:
    """Atualiza a tabela `markets` e devolve os token_ids selecionados."""
    cfg = config.load()["catalog"]
    owns = store is None
    store = store or Store(connect())
    now = datetime.now(timezone.utc)

    async with PolymarketAPI() as api:
        eventos = await fetch_events(api, cfg, now)

    selected = selecionar_eventos(eventos, cfg, now)
    for r in selected:
        r["selected"] = True

    cols = ", ".join(COLUMNS)
    placeholders = ", ".join("?" * len(COLUMNS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c not in ("token_id", "seen_at"))
    # Zera a seleção anterior antes de gravar a nova: `selected` reflete o
    # conjunto assinado agora, não o histórico.
    store.execute("UPDATE markets SET selected = FALSE")
    store.executemany(
        f"INSERT INTO markets ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (token_id) DO UPDATE SET {updates}",
        [tuple(r[c] for c in COLUMNS) for r in selected],
    )
    store.log("catalog", "info", "catalogo atualizado",
              {"eventos_varridos": len(eventos),
               "eventos_selecionados": len({r["event_id"] for r in selected}),
               "tokens_selecionados": len(selected)})
    store.flush()

    if owns:
        store.close()
    return [r["token_id"] for r in selected]


async def _main() -> None:
    tokens = await refresh()
    con = connect(read_only=True)
    stats = con.execute("""
        SELECT count(*), count(DISTINCT condition_id), count(DISTINCT event_id),
               sum(CASE WHEN neg_risk THEN 1 ELSE 0 END),
               round(median(liquidity_usd), 0), round(min(liquidity_usd), 0)
        FROM markets WHERE selected
    """).fetchone()
    print(f"Selecionados {len(tokens)} tokens")
    print(f"  mercados={stats[1]}  eventos={stats[2]}  tokens negRisk={stats[3]}")
    print(f"  liquidez mediana=${stats[4]:,.0f}  minima=${stats[5]:,.0f}")
    print("\nPor categoria (regime de taxa / liga):")
    for cat, n, liq, fee, reb in con.execute("""
        SELECT category, count(*), round(sum(liquidity_usd), 0),
               round(max(fee_rate), 4), round(max(fee_rebate_rate), 4)
        FROM markets WHERE selected GROUP BY 1 ORDER BY 2 DESC LIMIT 15
    """).fetchall():
        print(f"  {cat:<20} {n:>4} tokens  ${liq:>13,.0f}  taker={fee}  rebate={reb}")
    print("\nTempo ate resolucao:")
    for bucket, n in con.execute("""
        SELECT CASE WHEN end_date < now() + INTERVAL 1 DAY  THEN 'a) < 24h'
                    WHEN end_date < now() + INTERVAL 3 DAY  THEN 'b) 1-3 dias'
                    WHEN end_date < now() + INTERVAL 7 DAY  THEN 'c) 3-7 dias'
                    ELSE 'd) 7-14 dias' END, count(*)
        FROM markets WHERE selected GROUP BY 1 ORDER BY 1
    """).fetchall():
        print(f"  {bucket:<14} {n:>4} tokens")
    con.close()


if __name__ == "__main__":
    asyncio.run(_main())
