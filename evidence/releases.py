import csv
import io
import json
from datetime import datetime, timezone

from evidence.analysis import Analysis
from evidence.contracts import DomainError, canonical, digest, identifier
from evidence.store import Store


class Releases:
    def __init__(self, store: Store):
        self.store = store
        self.analysis = Analysis(store)

    def create(self, request):
        query = request.query.payload()
        request_digest = digest({"query": query, "epoch": request.expected_epoch, "label": request.label,
                                 "review_digest": request.review_digest,
                                 "acknowledgements": sorted(request.acknowledgements)})
        with self.store.transaction(write=True) as connection:
            existing = connection.execute("SELECT * FROM releases WHERE request_id=?", (request.request_id,)).fetchone()
            if existing:
                if existing["request_digest"] != request_digest:
                    raise DomainError("release_request_conflict", "发布请求标识已使用")
                return self.decode(existing)
            epoch = self.store.epoch(connection)
            if epoch != request.expected_epoch:
                raise DomainError("stale_analysis", "证据已变化，请重新分析后发布", details={"actual_epoch": epoch})
            result = self.analysis.in_transaction(connection, query)
            if request.review_digest is not None:
                from evidence.review import Review
                result["review"] = Review(self.store).validate(connection, query, request.review_digest, request.acknowledgements)
                result.pop("checksum")
                result["checksum"] = digest(result)
            elif request.acknowledgements:
                raise DomainError("review_missing", "确认项须绑定复核摘要", 422)
            release_id = "r-" + digest({"request_id": request.request_id, "digest": request_digest})[:24]
            created = datetime.now(timezone.utc).isoformat()
            connection.execute("INSERT INTO releases VALUES (?,?,?,?,?,?,?,?,?)",
                               (release_id, request.request_id, request_digest, epoch, request.label,
                                canonical(query), canonical(result), result["checksum"], created))
            self.store.audit(connection, epoch, "released", release_id,
                             {"checksum": result["checksum"], "label": request.label})
            row = connection.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            return self.decode(row)

    @staticmethod
    def decode(row):
        return {"release_id": row["release_id"], "epoch": row["epoch"], "label": row["label"],
                "created_at": row["created_at"], "checksum": row["checksum"],
                "result": json.loads(row["result"])}

    def get(self, release_id):
        identifier(release_id)
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM releases WHERE release_id=?", (release_id,)).fetchone()
            if row is None:
                raise DomainError("release_missing", "发布不存在", 404)
            result = self.decode(row)
            content = dict(result["result"])
            recorded = content.pop("checksum")
            if digest(content) != recorded or recorded != result["checksum"]:
                raise DomainError("release_corrupt", "发布证据校验失败", 500)
            return result

    def list(self, after="", limit=100):
        with self.store.transaction() as connection:
            rows = connection.execute("SELECT release_id,epoch,label,checksum,created_at FROM releases "
                                      "WHERE release_id>? ORDER BY release_id LIMIT ?", (after, limit)).fetchall()
            return [dict(row) for row in rows]

    def compare(self, first_id, second_id):
        first, second = self.get(first_id), self.get(second_id)
        result = self.analysis.compare_results(first["result"], second["result"])
        result["subjects"] = self.analysis.compare_subjects(first["result"], second["result"])
        return result

    def compare_current(self, release_id):
        first = self.get(release_id)["result"]
        with self.store.transaction() as connection:
            current = self.analysis.in_transaction(connection, first["query"])
        return {**self.analysis.compare_results(first, current),
                "subjects": self.analysis.compare_subjects(first, current)}

    def csv(self, release_id):
        release = self.get(release_id)
        stream = io.StringIO(newline="")
        fields = ["center", "subjects", "observation_seconds", "usage_seconds", "usage_fraction",
                  "observed_subject_days", "active_subject_days", "state"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in release["result"]["strata"]:
            writer.writerow(row)
        return stream.getvalue()

    def verify(self, release_id):
        release = self.get(release_id)
        result = release["result"]
        self.analysis.check_strata(result["total"], result["strata"])
        for person in result["subjects"]:
            self.analysis.exposure.check(person)
            evidence = dict(person)
            checksum = evidence.pop("evidence_digest")
            if digest(evidence) != checksum:
                raise DomainError("evidence_corrupt", "对象证据校验失败", 500)
        return {"release_id": release_id, "valid": True, "checksum": release["checksum"],
                "subjects_verified": len(result["subjects"]), "epoch": release["epoch"]}
