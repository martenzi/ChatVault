from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    root: Path
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def db_dir(self) -> Path:
        return self.root / "db"

    @property
    def db_path(self) -> Path:
        return self.db_dir / "chatvault.sqlite"

    @property
    def brain_path(self) -> Path:
        """docs/42_SPEC_brain_db.md — a separate, small, portable database.
        Deliberately a sibling of chatvault.sqlite, not nested inside it or
        derived from it: renaming/moving chatvault.sqlite never affects this
        file, and copying this one file alone is enough to carry the brain
        to another machine (acceptance criteria: self-sufficiency, portability)."""
        return self.db_dir / "brain.sqlite"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def neo4j_dir(self) -> Path:
        return self.root / "neo4j"

    @property
    def manifest_path(self) -> Path:
        return self.raw_dir / "_manifest.jsonl"


def load_config() -> Config:
    _load_dotenv(Path.cwd() / ".env")
    root = Path(os.environ.get("CHATVAULT_ROOT", str(Path.cwd()))).expanduser().resolve()
    return Config(
        root=root,
        neo4j_uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        neo4j_user=os.environ.get("NEO4J_USER", "neo4j"),
        neo4j_password=os.environ.get("NEO4J_PASSWORD", ""),
    )
