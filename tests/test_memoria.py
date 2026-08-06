"""O banco nao pode pedir mais memoria do que o container pode dar.

O DuckDB nao enxerga cgroup: le a RAM da maquina e se da ~80% dela. Numa
droplet de 2 GB isso e ~1,5 GB, que e exatamente o teto do container. Medido em
producao: cinco mortes por OOM em meia hora, com o Docker reerguendo a cada vez.

    Memory cgroup out of memory: Killed process (python)
    MEM 1.465GiB / 1.465GiB  99.98%

O sintoma na tela era "o painel nao responde". A causa era o banco.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import duckdb

from core.db import connect

RAIZ = Path(__file__).resolve().parent.parent


def cfg() -> dict:
    with open(RAIZ / "config.toml", "rb") as f:
        return tomllib.load(f)


def _bytes(texto: str) -> float:
    m = re.match(r"([\d.]+)\s*([KMGT]?)B?", str(texto).upper())
    mult = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[m.group(2)]
    return float(m.group(1)) * mult


class TestLimiteDeMemoria:
    def test_config_declara_o_teto(self):
        a = cfg()["analise"]
        assert "duckdb_memoria" in a and "duckdb_threads" in a

    def test_teto_do_banco_cabe_no_teto_do_container(self):
        """O numero do compose e o do banco tem de conversar. Se o banco puder
        pedir tanto quanto o container inteiro, sobra zero para o Python."""
        compose = (RAIZ / "docker-compose.yml").read_text(encoding="utf-8")
        m = re.search(r"mem_limit:\s*(\S+)", compose)
        assert m, "docker-compose.yml sem mem_limit"
        teto_container = _bytes(m.group(1))
        teto_banco = _bytes(cfg()["analise"]["duckdb_memoria"])
        assert teto_banco <= 0.5 * teto_container, (
            f"DuckDB pode pedir {teto_banco/1e6:.0f}MB de "
            f"{teto_container/1e6:.0f}MB do container — sem folga para o "
            f"processo Python, polars e os buffers de escrita")

    def test_conexao_aplica_o_limite(self, tmp_path):
        con = connect(path=tmp_path / "t.duckdb")
        lim = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        thr = con.execute("SELECT current_setting('threads')").fetchone()[0]
        con.close()
        assert _bytes(lim) <= _bytes(cfg()["analise"]["duckdb_memoria"]) * 1.05
        assert int(thr) == cfg()["analise"]["duckdb_threads"]

    def test_toda_analise_pesada_respeita_a_janela(self):
        """Consulta sem janela varre o banco inteiro e materializa em polars —
        e o polars nao obedece o limite de memoria do DuckDB.

        Foi assim que o /gate0 derrubou o container: `nichos` e `markout`
        tinham janela, mas `spreads`, `negrisk` e `copyability`, que o gate0
        chama por baixo, nao tinham. Meia correcao nao corrige.
        """
        for nome in ("spreads", "negrisk", "copyability", "nichos", "markout"):
            fonte = (RAIZ / "analysis" / f"{nome}.py").read_text(encoding="utf-8")
            assert "janela.clausula" in fonte, \
                f"analysis/{nome}.py varre o banco sem limite de janela"

    def test_janela_nao_congela_no_import(self):
        """A janela e relativa ao agora e o coletor fica dias de pe. Consulta
        montada em f-string de MODULO fixaria o corte no momento do import."""
        for nome in ("spreads", "negrisk", "copyability", "nichos", "markout"):
            for linha in (RAIZ / "analysis" / f"{nome}.py").read_text(
                    encoding="utf-8").splitlines():
                if "janela.clausula" not in linha:
                    continue
                assert not linha.lstrip().startswith(("SERIE", "QUERY", "ATRASO",
                                                      "COPIA", "BASE")), \
                    f"analysis/{nome}.py monta a janela no import: {linha[:60]}"

    def test_precompute_pode_ser_desligado(self):
        """Intervalo enorme nao desligava: a PRIMEIRA rodada acontecia antes do
        intervalo, e era ela que matava o container."""
        fonte = (RAIZ / "collector" / "run.py").read_text(encoding="utf-8")
        assert "if minutos <= 0:" in fonte, \
            "precomputador sem chave de desligar de verdade"

    def test_limite_nao_impede_consulta_normal(self, tmp_path):
        """Derramar para disco e aceitavel; recusar a consulta nao e."""
        con = connect(path=tmp_path / "t2.duckdb")
        con.execute("INSERT INTO book_top SELECT "
                    "'t' || (i % 500), i, i, 0.4, 1, 0.42, 1, 0.41, 0.02, 'ws' "
                    "FROM range(200000) AS r(i)")
        n = con.execute("SELECT count(DISTINCT token_id) FROM book_top").fetchone()[0]
        con.close()
        assert n == 500
