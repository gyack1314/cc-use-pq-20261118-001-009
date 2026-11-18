import sqlite3
from contextlib import contextmanager
from pathlib import Path

from evidence.contracts import DomainError, canonical


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO meta VALUES ('schema', 1);
INSERT OR IGNORE INTO meta VALUES ('epoch', 0);
CREATE TABLE IF NOT EXISTS subjects (
 subject_id TEXT PRIMARY KEY, center TEXT NOT NULL,
 implanted_at INTEGER NOT NULL, ended_at INTEGER,
 version INTEGER NOT NULL CHECK(version>0), reason TEXT NOT NULL,
 CHECK(ended_at IS NULL OR ended_at>implanted_at)
);
CREATE TABLE IF NOT EXISTS subject_history (
 subject_id TEXT NOT NULL, version INTEGER NOT NULL, epoch INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(subject_id,version)
);
CREATE TABLE IF NOT EXISTS session_versions (
 source TEXT NOT NULL, session_id TEXT NOT NULL, revision INTEGER NOT NULL,
 subject_id TEXT NOT NULL REFERENCES subjects(subject_id),
 start INTEGER NOT NULL, end INTEGER NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('control','training','calibration')),
 withdrawn INTEGER NOT NULL CHECK(withdrawn IN (0,1)),
 payload TEXT NOT NULL, digest TEXT NOT NULL, epoch INTEGER NOT NULL,
 PRIMARY KEY(source,session_id,revision), CHECK(end>start)
);
CREATE INDEX IF NOT EXISTS session_subject ON session_versions(subject_id,start,end);
CREATE TABLE IF NOT EXISTS session_heads (
 source TEXT NOT NULL, session_id TEXT NOT NULL, revision INTEGER NOT NULL,
 PRIMARY KEY(source,session_id),
 FOREIGN KEY(source,session_id,revision) REFERENCES session_versions(source,session_id,revision)
);
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
 release_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
 request_digest TEXT NOT NULL, epoch INTEGER NOT NULL, label TEXT NOT NULL,
 query TEXT NOT NULL, result TEXT NOT NULL, checksum TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, epoch INTEGER NOT NULL,
 kind TEXT NOT NULL, entity TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("请使用文件数据库以共享事务连接")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            version = connection.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
            if version != 1:
                raise ValueError("不支持的数据库模式")
        finally:
            connection.close()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @contextmanager
    def transaction(self, write=False):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "locked" in str(exc).lower():
                raise DomainError("busy", "数据库繁忙，请重试", 503) from exc
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def epoch(connection):
        return connection.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]

    @staticmethod
    def advance(connection):
        connection.execute("UPDATE meta SET value=value+1 WHERE key='epoch'")
        return Store.epoch(connection)

    @staticmethod
    def audit(connection, epoch, kind, entity, payload):
        connection.execute(
            "INSERT INTO audit(epoch,kind,entity,payload) VALUES (?,?,?,?)",
            (epoch, kind, entity, canonical(payload)),
        )

    def status(self):
        with self.transaction() as connection:
            return {
                "schema": 1, "epoch": self.epoch(connection),
                "subjects": connection.execute("SELECT COUNT(*) FROM subjects").fetchone()[0],
                "session_heads": connection.execute("SELECT COUNT(*) FROM session_heads").fetchone()[0],
                "releases": connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0],
            }

    def audit_log(self, after=0, limit=100):
        import json
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM audit WHERE id>? ORDER BY id LIMIT ?", (after, limit)
            ).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def integrity(self):
        with self.transaction() as connection:
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            check = connection.execute("PRAGMA integrity_check").fetchone()[0]
            return {"ok": not foreign and check == "ok", "sqlite": check,
                    "foreign_key_errors": [list(row) for row in foreign]}
