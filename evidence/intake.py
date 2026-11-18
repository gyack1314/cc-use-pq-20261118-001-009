import json

from evidence.contracts import BatchInput, DomainError, canonical, digest, identifier, iso
from evidence.store import Store


class Intake:
    def __init__(self, store: Store):
        self.store = store

    def ingest(self, batch: BatchInput):
        payloads = [session.payload() for session in batch.sessions]
        request_digest = digest(payloads)
        with self.store.transaction(write=True) as connection:
            old = connection.execute("SELECT * FROM requests WHERE request_id=?", (batch.request_id,)).fetchone()
            if old:
                if old["digest"] != request_digest:
                    raise DomainError("request_conflict", "请求标识已用于不同批次")
                return json.loads(old["response"])
            keys = [(p["source"], p["session_id"]) for p in payloads]
            if len(set(keys)) != len(keys):
                raise DomainError("duplicate_key", "一个批次中会话键不能重复", 422)
            changes = []
            for payload in payloads:
                key = (payload["source"], payload["session_id"])
                head = connection.execute(
                    "SELECT v.* FROM session_heads h JOIN session_versions v "
                    "USING(source,session_id,revision) WHERE h.source=? AND h.session_id=?", key
                ).fetchone()
                known = connection.execute(
                    "SELECT digest FROM session_versions WHERE source=? AND session_id=? AND revision=?",
                    (*key, payload["revision"])
                ).fetchone()
                fingerprint = digest(payload)
                if known:
                    if known["digest"] != fingerprint:
                        raise DomainError("revision_conflict", "相同修订含不同内容", details={"key": list(key)})
                    changes.append((payload, "duplicate", fingerprint))
                    continue
                if head and payload["revision"] <= head["revision"]:
                    raise DomainError("stale_revision", "不能以旧修订替换当前会话", details={"key": list(key)})
                subject = connection.execute("SELECT 1 FROM subjects WHERE subject_id=?", (payload["subject_id"],)).fetchone()
                if not subject:
                    raise DomainError("subject_missing", "会话对象未登记", 422, {"subject_id": payload["subject_id"]})
                if head and payload["subject_id"] != head["subject_id"]:
                    raise DomainError("identity_conflict", "会话更正不能更换对象")
                changes.append((payload, "withdrawn" if payload["withdrawn"] else "accepted", fingerprint))
            changed = any(status != "duplicate" for _, status, _ in changes)
            epoch = self.store.advance(connection) if changed else self.store.epoch(connection)
            results = []
            for payload, status, fingerprint in changes:
                if status != "duplicate":
                    connection.execute("INSERT INTO session_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                       (payload["source"], payload["session_id"], payload["revision"],
                                        payload["subject_id"], payload["start"], payload["end"], payload["mode"],
                                        int(payload["withdrawn"]), canonical(payload), fingerprint, epoch))
                    connection.execute("INSERT INTO session_heads VALUES (?,?,?) ON CONFLICT(source,session_id) "
                                       "DO UPDATE SET revision=excluded.revision",
                                       (payload["source"], payload["session_id"], payload["revision"]))
                    self.store.audit(connection, epoch, "session_changed",
                                     payload["source"] + "/" + payload["session_id"],
                                     {"revision": payload["revision"], "status": status, "digest": fingerprint})
                results.append({"source": payload["source"], "session_id": payload["session_id"],
                                "revision": payload["revision"], "status": status})
            response = {"request_id": batch.request_id, "epoch": epoch, "results": results}
            connection.execute("INSERT INTO requests VALUES (?,?,?)",
                               (batch.request_id, request_digest, canonical(response)))
            return response

    @staticmethod
    def heads(connection, subject_ids, start, end):
        rows = connection.execute(
            "SELECT v.* FROM session_heads h JOIN session_versions v USING(source,session_id,revision) "
            "WHERE v.start<? AND v.end>? ORDER BY v.subject_id,v.source,v.session_id", (end, start)
        ).fetchall()
        selected = set(subject_ids)
        return [json.loads(row["payload"]) for row in rows if row["subject_id"] in selected]

    def get(self, source, session_id):
        identifier(source)
        identifier(session_id)
        with self.store.transaction() as connection:
            rows = connection.execute("SELECT * FROM session_versions WHERE source=? AND session_id=? ORDER BY revision",
                                      (source, session_id)).fetchall()
            if not rows:
                raise DomainError("session_missing", "会话不存在", 404)
            return {"source": source, "session_id": session_id, "head": rows[-1]["revision"],
                    "versions": [{"payload": json.loads(row["payload"]), "epoch": row["epoch"],
                                  "digest": row["digest"]} for row in rows]}

    def summarize(self):
        with self.store.transaction() as connection:
            rows = connection.execute(
                "SELECT v.source,COUNT(*) AS total,SUM(v.withdrawn) AS withdrawn FROM session_heads h "
                "JOIN session_versions v USING(source,session_id,revision) GROUP BY v.source ORDER BY v.source"
            ).fetchall()
            return [dict(row) for row in rows]

    def export(self, source, after="", limit=100):
        identifier(source)
        with self.store.transaction() as connection:
            rows = connection.execute(
                "SELECT v.* FROM session_heads h JOIN session_versions v USING(source,session_id,revision) "
                "WHERE v.source=? AND v.session_id>? ORDER BY v.session_id LIMIT ?", (source, after, limit)
            ).fetchall()
            return [{"payload": json.loads(row["payload"]), "epoch": row["epoch"],
                     "digest": row["digest"]} for row in rows]
