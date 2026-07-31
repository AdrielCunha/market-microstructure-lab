"""Carregamento de configuração. Fonte única de verdade: config.toml na raiz."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    @property
    def db_path(self) -> Path:
        p = Path(self.section("db").get("path", "data/polymarket.duckdb"))
        return p if p.is_absolute() else ROOT / p

    def __getitem__(self, name: str) -> dict[str, Any]:
        return self.section(name)


@lru_cache(maxsize=1)
def load(path: Path | None = None) -> Config:
    path = path or ROOT / "config.toml"
    with open(path, "rb") as fh:
        return Config(tomllib.load(fh))
