import json

from evidence.contracts import DomainError, digest
from evidence.releases import Releases


class Lineage:
    def __init__(self, store):
        self.store = store
        self.releases = Releases(store)

    def release(self, release_id):
        release = self.releases.get(release_id)
        objects = []
        session_count = 0
        with self.store.transaction() as connection:
            for person in release["result"]["subjects"]:
                subject = connection.execute(
                    "SELECT * FROM subject_history WHERE subject_id=? AND version=?",
                    (person["subject_id"], person["subject_version"])
                ).fetchone()
                if subject is None or subject["epoch"] > release["epoch"]:
                    raise DomainError("lineage_missing", "无法定位发布时的对象资料", 500)
                subject_payload = json.loads(subject["payload"])
                expected_window = [
                    (max(subject_payload["implanted_at"], release["result"]["query"]["start"]),
                     min(subject_payload["ended_at"] if subject_payload["ended_at"] is not None else release["result"]["query"]["end"],
                         release["result"]["query"]["end"]))
                ]
                expected_window = [list(pair) for pair in expected_window if pair[0] < pair[1]]
                if expected_window != person["observation_intervals"]:
                    raise DomainError("lineage_conflict", "观察区间与发布时对象资料不符", 500)
                if subject_payload["center"] != person["center"]:
                    raise DomainError("lineage_conflict", "对象中心与发布证据不符", 500)
                sessions = []
                for trace in person["traces"]:
                    row = connection.execute(
                        "SELECT * FROM session_versions WHERE source=? AND session_id=? AND revision=?",
                        (trace["source"], trace["session_id"], trace["revision"])
                    ).fetchone()
                    if row is None or row["epoch"] > release["epoch"]:
                        raise DomainError("lineage_missing", "无法定位发布时的会话修订", 500)
                    payload = json.loads(row["payload"])
                    if row["digest"] != trace["payload_digest"] or digest(payload) != row["digest"]:
                        raise DomainError("lineage_conflict", "会话原文与发布证据不符", 500)
                    if payload["subject_id"] != person["subject_id"]:
                        raise DomainError("lineage_conflict", "会话归属与发布证据不符", 500)
                    sessions.append({"source": row["source"], "session_id": row["session_id"],
                                     "revision": row["revision"], "epoch": row["epoch"],
                                     "digest": row["digest"], "payload": payload,
                                     "decision": trace["decision"]})
                session_count += len(sessions)
                objects.append({"subject_id": person["subject_id"], "version": person["subject_version"],
                                "epoch": subject["epoch"], "payload": subject_payload,
                                "evidence_digest": person["evidence_digest"], "sessions": sessions})
        return {"release_id": release_id, "epoch": release["epoch"], "checksum": release["checksum"],
                "subjects": objects, "session_count": session_count}

    def rebuild(self, release_id):
        from evidence.analysis import Analysis
        from evidence.exposure import Exposure
        lineage = self.release(release_id)
        original = self.releases.get(release_id)
        query = original["result"]["query"]
        people = []
        for person in lineage["subjects"]:
            calculated = Exposure().evaluate(person["payload"], [s["payload"] for s in person["sessions"]], query)
            if calculated["evidence_digest"] != person["evidence_digest"]:
                raise DomainError("rebuild_mismatch", "历史原始证据无法复算对象结果", 500,
                                  {"subject_id": person["subject_id"]})
            people.append(calculated)
        total = Analysis.aggregate([p for p in people if p["eligible"]])
        if total != original["result"]["total"]:
            raise DomainError("rebuild_mismatch", "历史原始证据无法复算总体结果", 500)
        return {"release_id": release_id, "reproduced": True,
                "subjects": len(people), "sessions": lineage["session_count"], "total": total}

    def session_impact(self, source, session_id):
        impacted = []
        with self.store.transaction() as connection:
            rows = connection.execute("SELECT release_id,epoch,result FROM releases ORDER BY created_at,release_id").fetchall()
            for row in rows:
                result = json.loads(row["result"])
                for person in result["subjects"]:
                    for trace in person["traces"]:
                        if trace["source"] == source and trace["session_id"] == session_id:
                            impacted.append({"release_id": row["release_id"], "epoch": row["epoch"],
                                             "subject_id": person["subject_id"], "revision": trace["revision"],
                                             "decision": trace["decision"], "valid_seconds": trace["valid_seconds"]})
        return {"source": source, "session_id": session_id, "releases": impacted}

    def subject_impact(self, subject_id):
        impacted = []
        with self.store.transaction() as connection:
            rows = connection.execute("SELECT release_id,epoch,result FROM releases ORDER BY created_at,release_id").fetchall()
            for row in rows:
                for person in json.loads(row["result"])["subjects"]:
                    if person["subject_id"] == subject_id:
                        impacted.append({"release_id": row["release_id"], "epoch": row["epoch"],
                                         "subject_version": person["subject_version"], "center": person["center"],
                                         "observation_seconds": person["observation_seconds"],
                                         "usage_seconds": person["usage_seconds"]})
        return {"subject_id": subject_id, "releases": impacted}

    def audit_release(self, release_id):
        release = self.releases.get(release_id)
        with self.store.transaction() as connection:
            rows = connection.execute("SELECT * FROM audit WHERE kind='released' AND entity=? ORDER BY id",
                                      (release_id,)).fetchall()
            if len(rows) != 1:
                raise DomainError("audit_missing", "发布审计记录不唯一", 500)
            event = dict(rows[0])
            event["payload"] = json.loads(event["payload"])
            if event["epoch"] != release["epoch"] or event["payload"]["checksum"] != release["checksum"]:
                raise DomainError("audit_conflict", "发布审计与冻结证据不一致", 500)
            return event
