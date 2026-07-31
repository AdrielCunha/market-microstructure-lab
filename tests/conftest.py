"""Fixtures com payloads reais capturados da API do Polymarket.

Preferimos payloads reais a mocks inventados: os bugs que este projeto teve até
agora vieram todos de suposições erradas sobre o formato da API (limite oculto
de paginação, ordenação dos níveis do livro, campo de taxa sempre zerado).
"""

from __future__ import annotations

import pytest

# Frame real do canal `market`, event_type=book. Note a ordenação dos níveis:
# a API devolve bids em ordem CRESCENTE e asks em DECRESCENTE, ou seja o melhor
# preço de cada lado é o último elemento — não o primeiro.
BOOK_REAL = {
    "market": "0x256f047d092fee27533edfe78f26ddf4de4791f8a4f4a36100889a8b27b035cc",
    "asset_id": "52334850467435521842916615125975586933206709933029159102614978949274926662464",
    "timestamp": "1785343564023",
    "hash": "ad3bb9f09664e304b36af1495b40a2cea1c57f2d",
    "bids": [
        {"price": "0.001", "size": "300020"},
        {"price": "0.01", "size": "723922"},
        {"price": "0.90", "size": "1500"},
        {"price": "0.94", "size": "820"},
    ],
    "asks": [
        {"price": "0.99", "size": "5000"},
        {"price": "0.97", "size": "3000"},
        {"price": "0.96", "size": "1200"},
    ],
    "tick_size": "0.01",
    "event_type": "book",
    "last_trade_price": "0.480",
}

# Frame real de price_change: traz best_bid/best_ask já calculados por asset.
PRICE_CHANGE_REAL = {
    "market": "0x11811b240a44b7cce3beec31d61223b2fc4bca8752956bdf862e265bd69831f6",
    "timestamp": "1785344843089",
    "event_type": "price_change",
    "price_changes": [
        {"asset_id": "90570730666613917518574871037492279122311153102967909821606494664520575912237",
         "price": "0.51", "size": "6314.07", "side": "BUY",
         "hash": "eac65b479776d9ae740a519e95d3b3c42e3b213b",
         "best_bid": "0.52", "best_ask": "0.53"},
        {"asset_id": "106007489289760757104329576702757602800149491510622801752044322949139400901593",
         "price": "0.49", "size": "6314.07", "side": "SELL",
         "hash": "1d4e5a13bf0a9e41ff615a54a9feb37997949dcd",
         "best_bid": "0.47", "best_ask": "0.48"},
    ],
}

# Trade real da Data API (timestamp em SEGUNDOS, diferente do resto do projeto).
TRADE_REAL = {
    "proxyWallet": "0x204f72f35326db932158cba6adff0b9a1da95e14",
    "side": "BUY",
    "asset": "39476185069140671488956560137664767367800437605677087267836800781802377598270",
    "conditionId": "0xb99fc1ddbd249e8a7b6993a036de74634669dd10594991aa056427c180df44fa",
    "size": 2.781607,
    "price": 0.8699999676,
    "timestamp": 1785343928,
    "title": "Universitatea Craiova CS 1st Half O/U 1.5",
    "slug": "ucl-ucr-pls-2026-07-29-first-half-team-total-home-1pt5",
    "outcome": "Under",
    "name": "swisstony",
    "transactionHash": "0xc32b8d889846f1c2df279cb337506771f9b78419ef791307349f307aed9fe1d0",
}

# Mercado real da Gamma: campos vêm como STRING contendo JSON.
MARKET_REAL = {
    "conditionId": "0x947e58cba3202dad1263aae4b762f3f3449d51d653ab3d7edf44e8fbbdb842f4",
    "question": "Will Southend United FC win on 2026-08-08?",
    "clobTokenIds": '["4362565718172399607803233902301070583743211054026619608100805301183001268176", "31907764000000000000000000000000000000000000000000000000000000000000000000000"]',
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.485", "0.515"]',
    "negRisk": True,
    "negRiskMarketID": "0x9a82545406ceba67ab35a17c5677bf290a9da26e3a821ad30af91030fd615400",
    "endDate": "2026-08-08T14:00:00Z",
    "liquidityNum": 99.9995,
    "volumeNum": 0.0,
    "orderPriceMinTickSize": 0.01,
    "orderMinSize": 5,
    "feeType": "sports_fees_v2",
    "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
    "feesEnabled": True,
    "active": True,
    "closed": False,
    "acceptingOrders": True,
    "events": [{"id": "760842", "slug": "enl-alt-sou-2026-08-08",
                "title": "Alt vs Southend"}],
}


@pytest.fixture
def book_real() -> dict:
    return BOOK_REAL


@pytest.fixture
def price_change_real() -> dict:
    return PRICE_CHANGE_REAL


@pytest.fixture
def trade_real() -> dict:
    return TRADE_REAL


@pytest.fixture
def market_real() -> dict:
    return MARKET_REAL
