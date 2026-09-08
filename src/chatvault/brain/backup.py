from __future__ import annotations

import gzip
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from chatvault.config import Config


@dataclass
class BrainBackupInfo:
    path: Path
    label: str | None
    created_at: str
    bytes: int
    item_count: int


def _zstd_available() -> bool:
    return shutil.which("zstd") is not None


def create_brain_backup(config: Config, label: str | None = None) -> BrainBackupInfo:
    """The Brain has no raw/ to rebuild from (BRAIN_FAQ.md §11) — everything
    in it is typed once and exists nowhere else, so every destructive
    operation (`remove entry --confirm`, an import, a migration) backs it up
    first. SQLite online backup API, same as backup.create_backup for the
    vault, so it's safe against an open WAL writer."""
    if not config.brain_path.exists():
        raise FileNotFoundError(f"{config.brain_path} does not exist yet")

    src = sqlite3.connect(str(config.brain_path))
    try:
        item_count = src.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_db = Path(tmp) / "backup.sqlite"
            dst = sqlite3.connect(tmp_db)
            try:
                src.backup(dst)
            finally:
                dst.close()

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            suffix = f"-{label}" if label else ""
            backups_dir = config.backup_dir
            backups_dir.mkdir(parents=True, exist_ok=True)

            if _zstd_available():
                final_path = backups_dir / f"brain-{timestamp}{suffix}.sqlite.zst"
                subprocess.run(["zstd", "-q", "-f", str(tmp_db), "-o", str(final_path)], check=True)
            else:
                final_path = backups_dir / f"brain-{timestamp}{suffix}.sqlite.gz"
                with open(tmp_db, "rb") as fh_in, gzip.open(final_path, "wb") as fh_out:
                    shutil.copyfileobj(fh_in, fh_out)
    finally:
        src.close()

    return BrainBackupInfo(
        path=final_path,
        label=label,
        created_at=datetime.now(timezone.utc).isoformat(),
        bytes=final_path.stat().st_size,
        item_count=item_count,
    )
