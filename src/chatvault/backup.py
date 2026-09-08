from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from chatvault.config import Config


@dataclass
class BackupInfo:
    path: Path
    label: str | None
    created_at: str
    bytes: int
    conversation_count: int
    tag_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(label: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in label)


def _backups_dir(config: Config, out_dir: Path | None) -> Path:
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    if config.backup_dir_override:
        try:
            config.backup_dir_override.mkdir(parents=True, exist_ok=True)
            return config.backup_dir_override
        except (OSError, PermissionError) as exc:
            import sys
            print(f"[warning] CHATVAULT_BACKUP_DIR ({config.backup_dir_override}) unreachable ({exc}); falling back to local db/backups", file=sys.stderr)

    d = config.backup_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _zstd_available() -> bool:
    return shutil.which("zstd") is not None


def create_backup(config: Config, label: str | None = None, out_dir: Path | None = None) -> BackupInfo:
    """Uses the SQLite Online Backup API from a separate read connection to
    config.db_path, so it's safe against an open WAL writer — unlike a plain
    file copy, which can capture a torn state mid-write."""
    src = sqlite3.connect(config.db_path)
    try:
        conversation_count = src.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        tag_count = src.execute("SELECT COUNT(*) FROM tags").fetchone()[0]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_db = Path(tmp) / "backup.sqlite"
            dst = sqlite3.connect(tmp_db)
            try:
                src.backup(dst)
            finally:
                dst.close()

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
            suffix = f"-{_slug(label)}" if label else ""
            backups_dir = _backups_dir(config, out_dir)

            if _zstd_available():
                final_path = backups_dir / f"chatvault-{timestamp}{suffix}.sqlite.zst"
                subprocess.run(
                    ["zstd", "-q", "-f", str(tmp_db), "-o", str(final_path)],
                    check=True,
                )
            else:
                final_path = backups_dir / f"chatvault-{timestamp}{suffix}.sqlite.gz"
                with open(tmp_db, "rb") as fh_in, gzip.open(final_path, "wb") as fh_out:
                    shutil.copyfileobj(fh_in, fh_out)
    finally:
        src.close()

    info = BackupInfo(
        path=final_path,
        label=label,
        created_at=_now(),
        bytes=final_path.stat().st_size,
        conversation_count=conversation_count,
        tag_count=tag_count,
    )
    sidecar = final_path.with_suffix(final_path.suffix + ".json")
    sidecar.write_text(json.dumps({**asdict(info), "path": str(info.path)}, indent=2))
    return info


def list_backups(config: Config, out_dir: Path | None = None) -> list[BackupInfo]:
    backups_dir = _backups_dir(config, out_dir)
    infos = []
    for sidecar in sorted(backups_dir.glob("*.json")):
        try:
            data = json.loads(sidecar.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        data["path"] = Path(data["path"])
        infos.append(BackupInfo(**data))
    return infos


def _decompress(backup_file: Path, dest: Path) -> None:
    if backup_file.suffix == ".zst" or backup_file.name.endswith(".sqlite.zst"):
        subprocess.run(["zstd", "-q", "-f", "-d", str(backup_file), "-o", str(dest)], check=True)
    elif backup_file.name.endswith(".sqlite.gz"):
        with gzip.open(backup_file, "rb") as fh_in, open(dest, "wb") as fh_out:
            shutil.copyfileobj(fh_in, fh_out)
    else:
        raise ValueError(f"unrecognised backup file extension: {backup_file}")


def restore_backup(config: Config, backup_file: Path, to: Path | None = None, force: bool = False) -> Path:
    target = to or config.db_path

    if target.exists():
        if not force:
            raise FileExistsError(f"{target} already exists — use --force to overwrite (a pre-restore backup is always taken first)")
        create_backup(config, label="pre-restore")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "restored.sqlite"
        _decompress(backup_file, tmp_db)

        check_conn = sqlite3.connect(tmp_db)
        try:
            result = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check_conn.close()
        if result != "ok":
            raise RuntimeError(f"restored database failed integrity_check: {result}")

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_db), str(target))

    return target


def prune_backups(config: Config, keep: int, out_dir: Path | None = None) -> list[Path]:
    """Prunes backups keeping the most recent `keep` backups PLUS any backups
    with a 'pre-*' label, since pre-* labels mark critical checkpoints/decisions."""
    infos = sorted(list_backups(config, out_dir), key=lambda i: i.created_at, reverse=True)
    deleted = []

    # Candidates for pruning after the top `keep`
    candidates = infos[keep:]
    for info in candidates:
        # Preserve any pre-* label (e.g. pre-migrate, pre-curation-import, pre-dedupe, etc.)
        if info.label and (info.label.startswith("pre-") or info.label.startswith("pre_")):
            continue
        info.path.unlink(missing_ok=True)
        sidecar = info.path.with_suffix(info.path.suffix + ".json")
        sidecar.unlink(missing_ok=True)
        deleted.append(info.path)
    return deleted
