from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from chatvault.paths import canonical


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
    backup_dir_override: Path | None = None
    archive_dir_override: Path | None = None
    attachment_dir_override: Path | None = None

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
    def backup_dir(self) -> Path:
        if self.backup_dir_override:
            try:
                # If target is accessible (or creatable parent exists), use it
                self.backup_dir_override.mkdir(parents=True, exist_ok=True)
                return self.backup_dir_override
            except (OSError, PermissionError):
                pass
        return self.db_dir / "backups"

    @property
    def archive_dir(self) -> Path:
        if self.archive_dir_override:
            try:
                self.archive_dir_override.mkdir(parents=True, exist_ok=True)
                return self.archive_dir_override
            except (OSError, PermissionError):
                pass
        return self.root / "archive"

    @property
    def attachment_dir(self) -> Path:
        if self.attachment_dir_override:
            try:
                self.attachment_dir_override.mkdir(parents=True, exist_ok=True)
                return self.attachment_dir_override
            except (OSError, PermissionError):
                pass
        return self.root / "attachments"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def neo4j_dir(self) -> Path:
        return self.root / "neo4j"

    @property
    def manifest_path(self) -> Path:
        return self.raw_dir / "_manifest.jsonl"


def load_config() -> Config:
    _load_dotenv(Path.cwd() / ".env")
    # canonical(): a wrong-case CHATVAULT_ROOT (APFS accepts it) must not leak a
    # second spelling of the same directory into source_files.root_dir.
    root = Path(canonical(os.environ.get("CHATVAULT_ROOT", str(Path.cwd()))))
    backup_dir_str = os.environ.get("CHATVAULT_BACKUP_DIR")
    archive_dir_str = os.environ.get("CHATVAULT_ARCHIVE_DIR")
    attachment_dir_str = os.environ.get("CHATVAULT_ATTACHMENT_DIR")

    return Config(
        root=root,
        neo4j_uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        neo4j_user=os.environ.get("NEO4J_USER", "neo4j"),
        neo4j_password=os.environ.get("NEO4J_PASSWORD", ""),
        backup_dir_override=Path(backup_dir_str).expanduser().resolve() if backup_dir_str else None,
        archive_dir_override=Path(archive_dir_str).expanduser().resolve() if archive_dir_str else None,
        attachment_dir_override=Path(attachment_dir_str).expanduser().resolve() if attachment_dir_str else None,
    )
