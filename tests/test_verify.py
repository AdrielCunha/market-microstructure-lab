"""Testes da verificação de integridade.

Existem porque a própria verificação já mentiu uma vez: a checagem de
continuidade contava lacunas com `lag()` sobre timestamps distintos e reportou
3.198 lacunas de 2 minutos numa janela de 225 minutos — aritmeticamente
impossível. Uma verificação que dá alarme falso é pior que nenhuma, porque
treina quem lê a ignorar o vermelho.
"""

from __future__ import annotations

import contextlib
import io

import duckdb
import pytest

from core import janela
from core.db import SCHEMA, now_ms
from reports import verify

MINUTO = 60_000


def base_ms() -> int:
    """Ponto de partida DENTRO da janela de análise.

    As consultas passaram a olhar só as últimas horas (`core/janela.py`), porque
    varrer o banco inteiro segurava o lock e travava o coletor. Um `base` fixo em
    2023, como havia aqui, cai fora da janela e faz toda checagem rodar sobre
    zero linha — os testes passariam a validar o caminho de "sem dado" achando
    que validavam a checagem.

    Derivado da configuração, e não cravado: a janela já mudou de 48h para 6h
    uma vez, e um número fixo aqui quebraria de novo na próxima mudança.
    """
    return now_ms() - int(janela.horas() * 0.8 * 3_600_000)


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(SCHEMA)
    return c


def povoar(con, minutos: list[int], base: int | None = None) -> None:
    """Grava uma observação de topo em cada minuto pedido."""
    base = base_ms() if base is None else base
    con.executemany(
        "INSERT INTO book_top VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("t1", base + m * MINUTO, base + m * MINUTO,
          0.47, 10.0, 0.49, 10.0, 0.48, 0.02, "ws") for m in minutos])


def topo_avulso(con, token: str, bid, bid_sz, ask, ask_sz, mid, spread) -> None:
    """Uma linha solta, também dentro da janela."""
    t = base_ms()
    con.execute("INSERT INTO book_top VALUES (?,?,?,?,?,?,?,?,?,'ws')",
                [token, t, t, bid, bid_sz, ask, ask_sz, mid, spread])


def inicios(con, n: int, base: int | None = None) -> None:
    base = base_ms() if base is None else base
    con.executemany(
        "INSERT INTO collector_log VALUES (?,?,?,?,?)",
        [(base + i, "run", "info", "dashboard no ar", None) for i in range(n)])


def rodar(con) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        verify.main(con=con)
    return buf.getvalue()


class TestContinuidade:
    def test_serie_continua_passa(self, con):
        povoar(con, list(range(60)))
        inicios(con, 1)
        saida = rodar(con)
        assert "60 de 60 minutos com dado" in saida
        assert "0 vazios" in saida

    def test_lacuna_nao_pode_exceder_a_janela(self, con):
        """O bug original: mais lacunas do que cabiam no tempo observado.

        Com 60 minutos de janela e um buraco de 10, o resultado tem que ser
        exatamente 10 — nunca um numero maior que a propria janela.
        """
        povoar(con, list(range(30)) + list(range(40, 70)))
        inicios(con, 1)
        saida = rodar(con)
        assert "60 de 70 minutos com dado" in saida
        assert "10 vazios" in saida
        # A trava contra a classe de bug que originou este teste.
        for linha in saida.splitlines():
            if "minutos com dado" in linha:
                vazios = int(linha.split(";")[1].strip().split()[0])
                total = int(linha.split("de")[1].strip().split()[0])
                assert vazios <= total

    def test_lacuna_explicada_por_reinicio_nao_falha(self, con):
        """Parar e religar o coletor deixa buraco legitimo na serie."""
        povoar(con, list(range(30)) + list(range(50, 80)))
        inicios(con, 2)          # duas sessoes
        saida = rodar(con)
        assert "cobertura temporal" in saida
        assert "[FALHA] cobertura temporal" not in saida

    def test_lacuna_sem_reinicio_falha(self, con):
        """Sem reinicio, minuto vazio e queda de conexao nao detectada."""
        povoar(con, list(range(30)) + list(range(120, 150)))
        inicios(con, 1)
        saida = rodar(con)
        assert "[FALHA] cobertura temporal" in saida


class TestOutrasChecagens:
    def test_spread_negativo_e_pego(self, con):
        povoar(con, [0, 1])
        topo_avulso(con, "t2", 0.5, 1.0, 0.4, 1.0, 0.45, -0.1)
        saida = rodar(con)
        assert "[FALHA] nenhum spread negativo" in saida

    def test_preco_fora_do_intervalo_e_pego(self, con):
        povoar(con, [0, 1])
        topo_avulso(con, "t3", 1.4, 1.0, 1.6, 1.0, 1.5, 0.2)
        saida = rodar(con)
        assert "[FALHA] precos dentro de [0,1]" in saida

    def test_reconexao_ocasional_nao_falha(self, con):
        """Reconexao de WebSocket e operacao normal; o sintoma e a frequencia."""
        povoar(con, list(range(120)))
        inicios(con, 1)
        con.execute("INSERT INTO collector_log VALUES (?,?,?,?,?)",
                    [base_ms(), "books", "warn", "ws0 caiu, reconectando", None])
        saida = rodar(con)
        assert "[FALHA] avisos em ritmo normal" not in saida
