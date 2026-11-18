import json

from evidence.contracts import DomainError, SubjectInput, canonical, identifier, iso, timestamp
from evidence.store import Store


class Registry:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def encode(row):
        return {"subject_id": row["subject_id"], "center": row["center"],
                "implanted_at": iso(row["implanted_at"]),
                "ended_at": iso(row["ended_at"]) if row["ended_at"] is not None else None,
                "version": row["version"], "reason": row["reason"]}

    def upsert(self, value: SubjectInput):
        with self.store.transaction(write=True) as connection:
            current = connection.execute("SELECT * FROM subjects WHERE subject_id=?", (value.subject_id,)).fetchone()
            version = current["version"] if current else 0
            if value.expected_version != version:
                raise DomainError("subject_version", "对象资料已改变", details={"actual_version": version})
            payload = {"subject_id": value.subject_id, "center": value.center,
                       "implanted_at": timestamp(value.implanted_at),
                       "ended_at": timestamp(value.ended_at) if value.ended_at is not None else None,
                       "version": version + 1, "reason": value.reason}
            epoch = self.store.advance(connection)
            connection.execute(
                "INSERT INTO subjects VALUES (:subject_id,:center,:implanted_at,:ended_at,:version,:reason) "
                "ON CONFLICT(subject_id) DO UPDATE SET center=excluded.center,implanted_at=excluded.implanted_at,"
                "ended_at=excluded.ended_at,version=excluded.version,reason=excluded.reason", payload
            )
            connection.execute("INSERT INTO subject_history VALUES (?,?,?,?)",
                               (value.subject_id, version + 1, epoch, canonical(payload)))
            self.store.audit(connection, epoch, "subject_changed", value.subject_id, payload)
            return {"epoch": epoch, "subject": self.encode(payload)}

    def get(self, subject_id):
        identifier(subject_id)
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM subjects WHERE subject_id=?", (subject_id,)).fetchone()
            if row is None:
                raise DomainError("subject_missing", "对象不存在", 404)
            return self.encode(row)

    def list(self, center=None, after="", limit=100):
        with self.store.transaction() as connection:
            if center:
                identifier(center)
                rows = connection.execute("SELECT * FROM subjects WHERE center=? AND subject_id>? ORDER BY subject_id LIMIT ?",
                                          (center, after, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM subjects WHERE subject_id>? ORDER BY subject_id LIMIT ?",
                                          (after, limit)).fetchall()
            return [self.encode(row) for row in rows]

    def history(self, subject_id):
        self.get(subject_id)
        with self.store.transaction() as connection:
            rows = connection.execute("SELECT * FROM subject_history WHERE subject_id=? ORDER BY version",
                                      (subject_id,)).fetchall()
            return [{"epoch": row["epoch"], "subject": self.encode(json.loads(row["payload"]))} for row in rows]

    @staticmethod
    def eligible(subject, start, end):
        left = max(subject["implanted_at"], start)
        right = min(subject["ended_at"] if subject["ended_at"] is not None else end, end)
        return (left, right) if left < right else None

    @staticmethod
    def snapshot(connection, centers):
        rows = connection.execute("SELECT * FROM subjects ORDER BY subject_id").fetchall()
        known = {row["center"] for row in rows}
        missing = set(centers) - known
        if missing:
            raise DomainError("center_missing", "筛选含未知中心", 422, {"centers": sorted(missing)})
        return [dict(row) for row in rows if not centers or row["center"] in centers]
