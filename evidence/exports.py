import csv
import io
import json
import zipfile

from evidence.contracts import canonical, digest
from evidence.lineage import Lineage
from evidence.releases import Releases


def csv_text(fields, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        safe = {}
        for field in fields:
            value = row.get(field)
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value
            safe[field] = value
        writer.writerow(safe)
    return stream.getvalue()


class Exports:
    def __init__(self, store):
        self.store = store
        self.releases = Releases(store)

    def subjects(self, release_id):
        result = self.releases.get(release_id)["result"]
        fields = ("subject_id", "center", "subject_version", "eligible", "observation_seconds",
                  "usage_seconds", "observed_days", "active_days", "quality_lost_seconds",
                  "overlap_seconds", "sessions_included", "evidence_digest")
        return csv_text(fields, result["subjects"])

    def daily(self, release_id):
        result = self.releases.get(release_id)["result"]
        rows = []
        for person in result["subjects"]:
            for day in person["days"]:
                rows.append({"subject_id": person["subject_id"], "center": person["center"], **day})
        return csv_text(("subject_id", "center", "date", "observation_seconds", "usage_seconds",
                         "active", "threshold_seconds"), rows)

    def decisions(self, release_id):
        result = self.releases.get(release_id)["result"]
        rows = []
        for person in result["subjects"]:
            for trace in person["traces"]:
                rows.append({"subject_id": person["subject_id"], **trace})
        return csv_text(("subject_id", "source", "session_id", "revision", "mode", "decision",
                         "start", "end", "valid_seconds", "payload_digest"), rows)

    def bundle(self, release_id):
        release = self.releases.get(release_id)
        lineage = Lineage(self.store)
        lineage.rebuild(release_id)
        audit = lineage.audit_release(release_id)
        files = {"release.json": canonical(release) + "\n",
                 "subjects.csv": self.subjects(release_id), "days.csv": self.daily(release_id),
                 "decisions.csv": self.decisions(release_id),
                 "centers.csv": self.releases.csv(release_id),
                 "lineage.json": canonical(lineage.release(release_id)) + "\n",
                 "audit.json": canonical(audit) + "\n"}
        import hashlib
        manifest = {"release_id": release_id, "checksum": release["checksum"], "files": {
            name: {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                   "bytes": len(content.encode("utf-8"))} for name, content in sorted(files.items())}}
        files["manifest.json"] = canonical(manifest) + "\n"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, content.encode("utf-8"))
        return stream.getvalue()

    @staticmethod
    def verify_bundle(content):
        import hashlib
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("包内含重复文件")
            manifest = json.loads(archive.read("manifest.json"))
            if set(names) != set(manifest["files"]) | {"manifest.json"}:
                raise ValueError("包内文件不符合清单")
            for name, expected in manifest["files"].items():
                if "/" in name or "\\" in name or name.startswith("."):
                    raise ValueError("非法包内路径")
                payload = archive.read(name)
                if len(payload) != expected["bytes"] or hashlib.sha256(payload).hexdigest() != expected["sha256"]:
                    raise ValueError("包内容摘要不符")
            release = json.loads(archive.read("release.json"))
            result = dict(release["result"])
            checksum = result.pop("checksum")
            if checksum != digest(result) or checksum != manifest["checksum"]:
                raise ValueError("结果摘要不符")
            if release["release_id"] != manifest["release_id"]:
                raise ValueError("发布编号不符")
        return {"valid": True, "release_id": manifest["release_id"], "files": len(names)}
