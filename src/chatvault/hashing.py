from __future__ import annotations

import hashlib

_US = "\x1f"  # unit separator: field joiner within one record
_RS = "\x1e"  # record separator: joiner between hashes of child records


def block_hash(type_: str, tool_name: str | None, text: str) -> str:
    payload = _US.join([type_, tool_name or "", text])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def message_hash(role: str, block_hashes: list[str]) -> str:
    payload = role + _US + _RS.join(block_hashes)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def conversation_content_hash(message_hashes: list[str]) -> str:
    return hashlib.sha256(_RS.join(message_hashes).encode("utf-8")).hexdigest()


def conversation_head_hash(message_hashes: list[str], n: int = 3) -> str:
    return hashlib.sha256(_RS.join(message_hashes[:n]).encode("utf-8")).hexdigest()


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_uid(platform: str, native_id: str | None, content_hash: str) -> str:
    if native_id:
        return f"{platform}:{native_id}"
    return f"{platform}:h:{content_hash}"


def normalize_note(note: str) -> str:
    return " ".join(note.strip().split())


def note_hash(note: str) -> str:
    return hashlib.sha256(normalize_note(note).encode("utf-8")).hexdigest()
