from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chatvault.config import Config
from chatvault.db import connect, migrate

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def vault(tmp_path) -> Config:
    config = Config(
        root=tmp_path,
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password="",
    )
    for d in (config.raw_dir, config.db_dir, config.exports_dir):
        d.mkdir(parents=True, exist_ok=True)
    migrate(config)
    return config


@pytest.fixture()
def conn(vault: Config) -> sqlite3.Connection:
    c = connect(vault)
    yield c
    c.close()
