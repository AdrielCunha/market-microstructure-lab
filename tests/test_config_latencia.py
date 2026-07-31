"""A latencia usada pelas analises tem de vir da configuracao, nao do codigo.

Isto foi bug de verdade: `analysis/nichos.py` tinha `LATENCIA_MS = 230` cravado.
A operacao mudou para Londres e passou a rodar a 15ms, mas o modulo continuou
respondendo "onde um operador de 230ms consegue jogar". Medido depois na serie
historica: a 230ms sobrevivem ~52% das cotacoes, a 15ms sobrevivem 92,8%. O
ranking de nichos apontava para o lugar errado por um fator de quase dois.

A primeira tentativa de conserto tambem errou: deduzir a latencia real como o
maior valor de `latencias_ms` devolvia 170 — a maquina antiga do Brasil, que
fica na lista so para comparacao. Por isso a chave e explicita.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def carregar_config() -> dict:
    with open(RAIZ / "config.toml", "rb") as f:
        return tomllib.load(f)


class TestLatenciaVemDaConfiguracao:
    def test_config_declara_a_latencia_real(self):
        paper = carregar_config()["paper"]
        assert "latencia_real_ms" in paper, \
            "qual latencia e a real precisa ser declarado, nao adivinhado"
        assert paper["latencia_real_ms"] > 0

    def test_a_latencia_real_esta_entre_as_simuladas(self):
        """Se a maquina real nao estiver na matriz simulada, nenhum motor
        descreve o que de fato acontece."""
        paper = carregar_config()["paper"]
        assert paper["latencia_real_ms"] in paper["latencias_ms"]

    def test_nichos_usa_a_configurada(self):
        from analysis import nichos
        assert nichos.LATENCIA_MS == carregar_config()["paper"]["latencia_real_ms"]

    def test_nenhuma_latencia_cravada_no_codigo_de_analise(self):
        """Constante de latencia no meio do codigo envelhece calada."""
        alvo = (RAIZ / "analysis" / "nichos.py").read_text(encoding="utf-8")
        # Fora dos comentarios e docstrings, nao pode haver atribuicao fixa.
        for linha in alvo.splitlines():
            corpo = linha.split("#")[0]
            assert "LATENCIA_MS = 230" not in corpo, \
                "a constante velha voltou para o codigo"

    def test_motor_de_referencia_do_painel_bate_com_a_config(self):
        """O painel destaca um motor como 'o real'. Se o nome nao corresponder
        a latencia configurada, o numero em destaque descreve outra maquina."""
        from reports import paper_dash
        real = carregar_config()["paper"]["latencia_real_ms"]
        assert paper_dash.REFERENCIA.endswith(f"lat{real}"), \
            f"{paper_dash.REFERENCIA} nao corresponde a latencia real {real}ms"
