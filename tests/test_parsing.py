"""Testes do parsing dos payloads da API.

Todos os bugs reais deste projeto até agora vieram daqui: supor ordenação dos
níveis do livro, supor que `limit` era respeitado, supor que o campo de taxa
tinha valor. Cada teste abaixo trava uma dessas suposições.
"""

from __future__ import annotations

from collector.catalog import derive_category, explode_market, passes_filters
from collector.wallets import to_row, trade_uid
from core.api import best_levels


class TestBestLevels:
    def test_pega_o_melhor_preco_de_cada_lado(self, book_real):
        """A API devolve bids em ordem crescente e asks em decrescente.

        Pegar o primeiro (ou o último) elemento sem ordenar produziria um topo
        de livro errado e um spread negativo.
        """
        bid, bid_sz, ask, ask_sz = best_levels(book_real)
        assert bid == 0.94, "melhor bid e o MAIOR preco de compra"
        assert bid_sz == 820.0
        assert ask == 0.96, "melhor ask e o MENOR preco de venda"
        assert ask_sz == 1200.0

    def test_spread_nunca_negativo(self, book_real):
        bid, _, ask, _ = best_levels(book_real)
        assert ask - bid > 0

    def test_livro_vazio_nao_quebra(self):
        assert best_levels({"bids": [], "asks": []}) == (None, None, None, None)
        assert best_levels({}) == (None, None, None, None)

    def test_um_lado_so(self):
        bid, _, ask, _ = best_levels({"bids": [{"price": "0.3", "size": "10"}],
                                      "asks": []})
        assert bid == 0.3 and ask is None


class TestExplodeMarket:
    def test_campos_json_em_string_sao_desempacotados(self, market_real):
        """A Gamma devolve clobTokenIds e outcomes como string contendo JSON."""
        linhas = explode_market(market_real, market_real["events"][0], 3)
        assert len(linhas) == 2, "um mercado binario vira dois tokens"
        assert linhas[0]["outcome"] == "Yes"
        assert linhas[1]["outcome"] == "No"
        assert linhas[0]["outcome_index"] == 0
        assert not linhas[0]["token_id"].startswith("["), "token_id nao pode ser a string bruta"

    def test_captura_o_regime_de_taxa(self, market_real):
        linha = explode_market(market_real, market_real["events"][0], 3)[0]
        assert linha["fee_rate"] == 0.05
        assert linha["fee_rebate_rate"] == 0.15
        assert linha["fee_taker_only"] is True

    def test_registra_total_de_pernas_do_evento(self, market_real):
        """Sem isso a analise de negative-risk nao sabe se o evento esta completo."""
        linha = explode_market(market_real, market_real["events"][0], 3)[0]
        assert linha["event_n_outcomes"] == 3

    def test_mercado_sem_tokens_e_ignorado(self):
        assert explode_market({"question": "x"}) == []


class TestDeriveCategory:
    def test_esporte_vira_liga(self):
        assert derive_category("sports_fees_v2", "ucl-kai-omo-2026-07-29") == "Sports/UCL"

    def test_nao_esporte(self):
        assert derive_category(None, "presidential-election-2028") == "NaoEsporte"

    def test_slug_estranho_nao_quebra(self):
        assert derive_category("sports_fees_v2", None) == "Sports/outro"
        assert derive_category("sports_fees_v2", "2026-07-29-jogo") == "Sports/outro"


class TestFiltros:
    def _base(self, **over):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        linha = {"closed": False, "active": True, "accepting_orders": True,
                 "liquidity_usd": 10000.0, "end_date": now + timedelta(days=2),
                 "category": "Sports/UCL"}
        linha.update(over)
        return linha, {"min_liquidity_usd": 5000.0, "max_days_to_resolution": 14,
                       "categories": []}, now

    def test_aprova_mercado_normal(self):
        linha, cfg, now = self._base()
        assert passes_filters(linha, cfg, now)

    def test_rejeita_fechado_e_sem_ordens(self):
        for campo in ("closed",):
            linha, cfg, now = self._base(**{campo: True})
            assert not passes_filters(linha, cfg, now)
        linha, cfg, now = self._base(accepting_orders=False)
        assert not passes_filters(linha, cfg, now)

    def test_rejeita_ja_vencido(self):
        from datetime import timedelta
        linha, cfg, now = self._base()
        linha["end_date"] = now - timedelta(hours=1)
        assert not passes_filters(linha, cfg, now)

    def test_rejeita_horizonte_longo(self):
        from datetime import timedelta
        linha, cfg, now = self._base()
        linha["end_date"] = now + timedelta(days=90)
        assert not passes_filters(linha, cfg, now)


class TestTrades:
    def test_timestamp_convertido_de_segundos_para_ms(self, trade_real):
        """A Data API usa segundos; o resto do projeto usa milissegundos.

        Errar isso faria o atraso de visao dar ~56 anos em vez de ~5 minutos.
        """
        linha = to_row(trade_real, ts_seen=1785343999000, is_backfill=False)
        ts_trade = linha[9]
        assert ts_trade == 1785343928 * 1000
        assert 0 < (1785343999000 - ts_trade) / 1000 < 3600

    def test_notional_e_preco_vezes_tamanho(self, trade_real):
        linha = to_row(trade_real, ts_seen=0, is_backfill=False)
        size, price, notional = linha[6], linha[7], linha[8]
        assert abs(notional - size * price) < 1e-9
        assert abs(notional - 2.42) < 0.01, "fill real de ~$2,42"

    def test_uid_e_estavel_e_distingue_fills(self, trade_real):
        assert trade_uid(trade_real) == trade_uid(dict(trade_real))
        outro = dict(trade_real, size=3.15)
        assert trade_uid(trade_real) != trade_uid(outro), \
            "mesma transacao com fills diferentes tem que gerar uids diferentes"
