"""Utilitários compartilhados do laboratório.

O console do Windows abre em cp1252 e quebra ao imprimir tabelas do polars
(caracteres de moldura) ou acentos. Como todo script do projeto importa algo de
`core`, o ajuste fica aqui e vale para todos.
"""

from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
