"""Janela de análise: até onde para trás as consultas pesadas olham.

Existe porque o banco cresce e as análises não podiam crescer junto. Medido em
produção com 1,47 GB acumulados em 5 dias:

    /paper     20,3s      /gate0    38,2s (falhou)
    /markout   20,4s      /nichos   recusou conexão

Enquanto uma dessas consultas roda, ela segura o `db_lock` e **o coletor para
de processar evento**. Uma que passasse de 300s fez o watchdog matar o processo:
`309s sem processar evento algum`. Ou seja: abrir o painel derrubava a coleta.

A janela não é economia de disco — a série inteira continua gravada e
disponível para análise offline. É economia de LATÊNCIA no caminho que disputa
o lock com quem está coletando.

Escolher 48h não é arbitrário: é o horizonte em que qualquer decisão desta fase
é tomada, e cobre um ciclo de fim de semana inteiro. Análise histórica profunda
se faz com o coletor parado, sem disputa.
"""

from __future__ import annotations

import time

PADRAO_HORAS = 48.0


def horas() -> float:
    try:
        from core import config
        return float(config.load()["analise"]["janela_horas"])
    except Exception:
        return PADRAO_HORAS


def corte_ms(agora_ms: int | None = None) -> int:
    """Epoch em ms a partir do qual as consultas pesadas devem olhar."""
    agora = agora_ms if agora_ms is not None else int(time.time() * 1000)
    return int(agora - horas() * 3_600_000)


def clausula(coluna: str = "ts_local", prefixo: str = "AND") -> str:
    """Pedaço de SQL pronto para colar. `prefixo` vira WHERE quando é o
    primeiro filtro da consulta."""
    return f"{prefixo} {coluna} >= {corte_ms()}"
