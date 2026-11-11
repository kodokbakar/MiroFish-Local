"""Local, best-effort cache for successful structured NER extraction.

Entries live under the ignored backend/uploads directory. Each entry is at most
1 MiB, expires after 30 days, and the directory keeps at most 500 entries.
Cache failures never prevent a fresh extraction.
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


CACHE_SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_MAX_ENTRIES = 500
MAX_ENTRY_BYTES = 1024 * 1024


def is_valid_extraction(result: Any) -> bool:
    """Reject incomplete or malformed entries before use or persistence."""
    if not isinstance(result, dict) or set(result) != {"entities", "relations"}:
        return False
    entities = result["entities"]
    relations = result["relations"]
    if not isinstance(entities, list) or not isinstance(relations, list):
        return False
    for entity in entities:
        if not isinstance(entity, dict):
            return False
        if not all(isinstance(entity.get(key), str) and entity[key].strip() for key in ("name", "type")):
            return False
        if not isinstance(entity.get("attributes"), dict):
            return False
    for relation in relations:
        if not isinstance(relation, dict):
            return False
        if not all(isinstance(relation.get(key), str) and relation[key].strip() for key in ("source", "target", "type", "fact")):
            return False
    return True


class ExtractionCache:
    """Atomic disk cache with safe per-instance hit/miss counters."""

    def __init__(self, directory: str | Path, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 max_entries: int = DEFAULT_MAX_ENTRIES):
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def stats(self) -> dict[str, int]:
        """Return counters only; never expose keys or document text."""
        with self._lock:
            return {"hits": self._hits, "misses": self._misses}

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("Invalid cache key")
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict | None:
        """Return only complete, compatible, unexpired entries."""
        result = None
        try:
            path = self._path(key)
            if path.stat().st_size <= MAX_ENTRY_BYTES:
                entry = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(entry, dict)
                    and entry.get("schema") == CACHE_SCHEMA_VERSION
                    and entry.get("key") == key
                    and isinstance(entry.get("created_at"), (int, float))
                    and 0 <= time.time() - entry["created_at"] <= self.ttl_seconds
                    and is_valid_extraction(entry.get("result"))
                ):
                    result = entry["result"]
        except (OSError, ValueError, TypeError, UnicodeError):
            pass
        with self._lock:
            if result is None:
                self._misses += 1
            else:
                self._hits += 1
        return result

    def put(self, key: str, result: dict) -> None:
        """Atomically publish a valid entry; concurrent writers cannot leave partial JSON."""
        if not is_valid_extraction(result):
            return
        entry = {"schema": CACHE_SCHEMA_VERSION, "key": key,
                 "created_at": time.time(), "result": result}
        try:
            self._path(key)
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            return
        if len(encoded) > MAX_ENTRY_BYTES:
            return
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.directory, 0o700)
            temporary = self.directory / f".{key}.{uuid.uuid4().hex}.tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(key))
            temporary = None
            self._prune()
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prune(self) -> None:
        """Remove expired files and oldest excess entries; races are harmless misses."""
        try:
            files = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
            now = time.time()
            live = []
            for path in files:
                if now - path.stat().st_mtime > self.ttl_seconds:
                    path.unlink(missing_ok=True)
                else:
                    live.append(path)
            for path in live[:-self.max_entries]:
                path.unlink(missing_ok=True)
        except OSError:
            pass
