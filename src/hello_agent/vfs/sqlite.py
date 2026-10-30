"""A small persistent filesystem in one SQLite database, with per-volume quotas."""

import sqlite3
from collections.abc import Mapping
from contextlib import closing, contextmanager
from pathlib import Path

from .core import Entry, Limits, Usage, VFSError, normalize_path
from .editing import apply_edits

# Do not mask SQL mistakes, constraint violations, or sqlite ProgrammingError.
_STORAGE_ERRORS = {
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
    sqlite3.SQLITE_READONLY,
    sqlite3.SQLITE_IOERR,
    sqlite3.SQLITE_CORRUPT,
    sqlite3.SQLITE_FULL,
    sqlite3.SQLITE_CANTOPEN,
    sqlite3.SQLITE_NOTADB,
    sqlite3.SQLITE_PERM,
}


class SQLiteFileSystem:
    """Lazy connections; file mutations and their quota checks are atomic.

    The initial limits are persisted. Reopening without explicit limits reuses
    them; conflicting explicit limits are rejected rather than silently changed.
    The root counts as one directory. Files are byte strings, not host files.
    """

    backend_type = "sqlite"
    read_only = False

    def __init__(
        self,
        database: Path,
        *,
        limits: Limits | None = None,
        initial_files: Mapping[str, bytes] | None = None,
    ):
        self.database = Path(database)
        self._limits = limits
        self._initial_files = dict(initial_files or {})
        for name, content in self._initial_files.items():
            if not name or "/" in name or name in (".", ".."):
                raise ValueError("Initial files must have root-level filenames")
            normalize_path("/" + name)
            if not isinstance(content, bytes):
                raise TypeError("Initial file content must be bytes")

    @contextmanager
    def _transaction(self):
        try:
            self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with closing(
                sqlite3.connect(self.database, isolation_level=None, timeout=30)
            ) as db:
                self.database.chmod(0o600)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys = ON")
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute("""CREATE TABLE IF NOT EXISTS volume_limits (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        max_file_bytes INTEGER NOT NULL, max_entries INTEGER NOT NULL
                    )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS entries (
                        path TEXT PRIMARY KEY, parent TEXT REFERENCES entries(path),
                        name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('file','directory')),
                        data BLOB,
                        CHECK ((kind = 'directory' AND data IS NULL) OR
                               (kind = 'file' AND data IS NOT NULL))
                    )""")
                    db.execute(
                        "CREATE INDEX IF NOT EXISTS entries_parent ON entries(parent)"
                    )
                    row = db.execute(
                        "SELECT * FROM volume_limits WHERE id=1"
                    ).fetchone()
                    if row is None:
                        limits = self._limits or Limits()
                        db.execute(
                            "INSERT INTO volume_limits VALUES (1,?,?)",
                            (limits.max_file_bytes, limits.max_entries),
                        )
                        db.execute(
                            "INSERT INTO entries VALUES ('/',NULL,'/','directory',NULL)"
                        )
                        if len(self._initial_files) + 1 > limits.max_entries:
                            raise VFSError(
                                "entry_limit",
                                "Initial files exceed the volume entry limit",
                            )
                        for name, content in self._initial_files.items():
                            if len(content) > limits.max_file_bytes:
                                raise VFSError(
                                    "file_too_large",
                                    "Initial file exceeds the volume file limit",
                                )
                            db.execute(
                                "INSERT INTO entries VALUES (?,'/',?,'file',?)",
                                ("/" + name, name, content),
                            )
                    else:
                        limits = Limits(row["max_file_bytes"], row["max_entries"])
                        if self._limits is not None and self._limits != limits:
                            raise VFSError(
                                "limits_mismatch",
                                "Volume already exists with different limits",
                            )
                    # Volume creation is durable even if the first file operation
                    # is rejected. Quota checks and mutations remain atomic below.
                    db.commit()
                    db.execute("BEGIN IMMEDIATE")
                    yield db, limits
                    db.commit()
                finally:
                    if db.in_transaction:
                        db.rollback()
        except OSError as exc:
            raise VFSError(
                "storage_error", f"Cannot access VFS storage: {exc.strerror or exc}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF not in _STORAGE_ERRORS:
                raise
            raise VFSError("storage_error", f"VFS storage failure: {exc}") from exc

    @staticmethod
    def _row(db, path: str):
        row = db.execute(
            "SELECT name, kind, length(data) AS size FROM entries WHERE path=?", (path,)
        ).fetchone()
        if row is None:
            raise VFSError("not_found", f"Path does not exist: {path!r}")
        return row

    @classmethod
    def _directory(cls, db, path: str):
        row = cls._row(db, path)
        if row["kind"] != "directory":
            raise VFSError("not_directory", f"Not a directory: {path!r}")
        return row

    @staticmethod
    def _entry(row) -> Entry:
        return Entry(row["name"], row["kind"], row["size"] or 0)

    @classmethod
    def _check_new_entry(cls, db, path: str, limits: Limits) -> tuple[str, str]:
        parent, name = path.rsplit("/", 1)
        parent = parent or "/"
        cls._directory(db, parent)
        if (
            db.execute("SELECT count(*) FROM entries").fetchone()[0]
            >= limits.max_entries
        ):
            raise VFSError(
                "entry_limit",
                f"Volume limit is {limits.max_entries} files and directories including root",
            )
        return parent, name

    def stat(self, path: str) -> Entry:
        path = normalize_path(path)
        with self._transaction() as (db, _):
            return self._entry(self._row(db, path))

    def listdir(self, path: str) -> list[Entry]:
        path = normalize_path(path)
        with self._transaction() as (db, _):
            self._directory(db, path)
            rows = db.execute(
                "SELECT name, kind, length(data) AS size FROM entries WHERE parent=? ORDER BY name",
                (path,),
            )
            return [self._entry(row) for row in rows]

    def read_file(self, path: str) -> bytes:
        path = normalize_path(path)
        with self._transaction() as (db, _):
            row = self._row(db, path)
            if row["kind"] != "file":
                raise VFSError("is_directory", f"Cannot read a directory: {path!r}")
            return bytes(
                db.execute("SELECT data FROM entries WHERE path=?", (path,)).fetchone()[
                    0
                ]
            )

    def write_file(self, path: str, content: bytes) -> None:
        path = normalize_path(path)
        if not isinstance(content, bytes):
            raise TypeError("File content must be bytes")
        with self._transaction() as (db, limits):
            if len(content) > limits.max_file_bytes:
                raise VFSError(
                    "file_too_large",
                    f"File limit is {limits.max_file_bytes} bytes; received {len(content)}",
                )
            existing = db.execute(
                "SELECT kind FROM entries WHERE path=?", (path,)
            ).fetchone()
            if existing:
                if existing["kind"] != "file":
                    raise VFSError(
                        "is_directory", f"Cannot overwrite a directory: {path!r}"
                    )
                db.execute("UPDATE entries SET data=? WHERE path=?", (content, path))
            else:
                parent, name = self._check_new_entry(db, path, limits)
                db.execute(
                    "INSERT INTO entries VALUES (?,?,?,'file',?)",
                    (path, parent, name, content),
                )

    def append_file(self, path: str, content: bytes) -> int:
        """Append, creation, and quota checks share one SQLite transaction."""
        path = normalize_path(path)
        if not isinstance(content, bytes):
            raise TypeError("File content must be bytes")
        with self._transaction() as (db, limits):
            row = db.execute(
                "SELECT kind, data FROM entries WHERE path=?", (path,)
            ).fetchone()
            if row is not None and row["kind"] != "file":
                raise VFSError("is_directory", f"Cannot append to directory: {path!r}")
            original = bytes(row["data"]) if row is not None else b""
            size = len(original) + len(content)
            if size > limits.max_file_bytes:
                raise VFSError(
                    "file_too_large", f"File limit is {limits.max_file_bytes} bytes"
                )
            if row is None:
                parent, name = self._check_new_entry(db, path, limits)
                db.execute(
                    "INSERT INTO entries VALUES (?,?,?,'file',?)",
                    (path, parent, name, content),
                )
            else:
                db.execute(
                    "UPDATE entries SET data=? WHERE path=?", (original + content, path)
                )
            return size

    def edit_file(self, path: str, edits: list[dict]) -> int:
        path = normalize_path(path)
        with self._transaction() as (db, limits):
            row = self._row(db, path)
            if row["kind"] != "file":
                raise VFSError("is_directory", f"Cannot edit a directory: {path!r}")
            original = bytes(
                db.execute("SELECT data FROM entries WHERE path=?", (path,)).fetchone()[
                    0
                ]
            )
            updated = apply_edits(original, edits)
            if len(updated) > limits.max_file_bytes:
                raise VFSError(
                    "file_too_large",
                    f"Edited file exceeds {limits.max_file_bytes} bytes",
                )
            db.execute("UPDATE entries SET data=? WHERE path=?", (updated, path))
            return len(updated)

    def mkdir(self, path: str) -> None:
        path = normalize_path(path)
        with self._transaction() as (db, limits):
            if db.execute("SELECT 1 FROM entries WHERE path=?", (path,)).fetchone():
                raise VFSError("already_exists", f"Path already exists: {path!r}")
            parent, name = self._check_new_entry(db, path, limits)
            db.execute(
                "INSERT INTO entries VALUES (?,?,?,'directory',NULL)",
                (path, parent, name),
            )

    def remove(self, path: str) -> None:
        path = normalize_path(path)
        if path == "/":
            raise VFSError("busy", "Cannot remove the volume root")
        with self._transaction() as (db, _):
            row = self._row(db, path)
            if (
                row["kind"] == "directory"
                and db.execute(
                    "SELECT 1 FROM entries WHERE parent=?", (path,)
                ).fetchone()
            ):
                raise VFSError("not_empty", f"Directory is not empty: {path!r}")
            db.execute("DELETE FROM entries WHERE path=?", (path,))

    def usage(self) -> Usage:
        with self._transaction() as (db, limits):
            count, size = db.execute(
                "SELECT count(*), coalesce(sum(length(data)),0) FROM entries"
            ).fetchone()
            return Usage(count, size, limits)
