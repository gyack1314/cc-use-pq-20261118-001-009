import csv
import io
from collections import OrderedDict

from pydantic import ValidationError

from evidence.contracts import BatchInput, DomainError, SessionInput, digest
from evidence.intake import Intake


SESSION_COLUMNS = ("source", "session_id", "revision", "subject_id", "start", "end", "mode", "withdrawn", "note")
QUALITY_COLUMNS = ("source", "session_id", "revision", "start", "end", "reason")


def read_table(content, columns, maximum):
    if len(content.encode("utf-8")) > 4_000_000:
        raise DomainError("table_size", "表格超过4MB", 422)
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff"), newline=""), strict=True)
    if reader.fieldnames is None:
        raise DomainError("table_header", "表格缺少表头", 422)
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise DomainError("table_header", "表头不能重复", 422)
    if set(reader.fieldnames) != set(columns):
        raise DomainError("table_header", "表头与契约不符", 422,
                          {"expected": list(columns), "actual": reader.fieldnames})
    result = []
    try:
        for number, row in enumerate(reader, start=2):
            if len(result) >= maximum:
                raise DomainError("table_rows", "表格行数超限", 422)
            if None in row or any(value is None for value in row.values()):
                raise DomainError("table_shape", "行列数与表头不一致", 422, {"row": number})
            result.append((number, {key: value.strip() for key, value in row.items()}))
    except csv.Error as exc:
        raise DomainError("table_syntax", "CSV语法错误", 422, {"line": reader.line_num}) from exc
    return result


def integer(text, row, field):
    if not text.isascii() or not text.isdecimal() or int(text) < 1:
        raise DomainError("table_integer", "修订号必须为正整数", 422, {"row": row, "field": field})
    return int(text)


class Tabular:
    def __init__(self, store):
        self.store = store
        self.intake = Intake(store)

    def parse(self, sessions_csv, quality_csv):
        rows = read_table(sessions_csv, SESSION_COLUMNS, 500)
        if not rows:
            raise DomainError("table_empty", "会话表不能为空", 422)
        sessions = OrderedDict()
        locations = {}
        for number, row in rows:
            revision = integer(row["revision"], number, "revision")
            key = (row["source"], row["session_id"], revision)
            if key in sessions:
                raise DomainError("table_duplicate", "会话表含重复会话修订", 422,
                                  {"row": number, "previous_row": locations[key]})
            if row["withdrawn"] not in ("true", "false"):
                raise DomainError("table_boolean", "withdrawn只接受true或false", 422, {"row": number})
            row["revision"] = revision
            row["withdrawn"] = row["withdrawn"] == "true"
            row["quality"] = []
            sessions[key] = row
            locations[key] = number
        if quality_csv:
            quality_rows = read_table(quality_csv, QUALITY_COLUMNS, 10000)
            for number, row in quality_rows:
                revision = integer(row["revision"], number, "revision")
                key = (row["source"], row["session_id"], revision)
                if key not in sessions:
                    raise DomainError("orphan_quality", "质量区间未对应本次导入会话", 422, {"row": number})
                sessions[key]["quality"].append({"start": row["start"], "end": row["end"], "reason": row["reason"]})
        validated = []
        for key, row in sessions.items():
            try:
                validated.append(SessionInput.model_validate(row))
            except ValidationError as exc:
                raise DomainError("table_validation", "会话内容不符合契约", 422,
                                  {"row": locations[key], "errors": exc.errors(include_url=False, include_context=False)}) from exc
        return validated

    def preview(self, sessions_csv, quality_csv=""):
        values = self.parse(sessions_csv, quality_csv)
        issues = []
        new = corrected = duplicate = 0
        with self.store.transaction() as connection:
            epoch = self.store.epoch(connection)
            for value in values:
                payload = value.payload()
                subject = connection.execute("SELECT 1 FROM subjects WHERE subject_id=?", (value.subject_id,)).fetchone()
                head = connection.execute("SELECT v.* FROM session_heads h JOIN session_versions v "
                                          "USING(source,session_id,revision) WHERE h.source=? AND h.session_id=?",
                                          (value.source, value.session_id)).fetchone()
                known = connection.execute("SELECT digest FROM session_versions WHERE source=? AND session_id=? AND revision=?",
                                           (value.source, value.session_id, value.revision)).fetchone()
                if not subject:
                    issues.append({"session_id": value.session_id, "code": "subject_missing"})
                elif known and known["digest"] == digest(payload):
                    duplicate += 1
                elif known:
                    issues.append({"session_id": value.session_id, "code": "revision_conflict"})
                elif head and value.subject_id != head["subject_id"]:
                    issues.append({"session_id": value.session_id, "code": "identity_conflict"})
                elif head and value.revision <= head["revision"]:
                    issues.append({"session_id": value.session_id, "code": "stale_revision"})
                elif head:
                    corrected += 1
                else:
                    new += 1
        keys = [(value.source, value.session_id) for value in values]
        if len(keys) != len(set(keys)):
            issues.append({"code": "duplicate_key", "message": "批次不能包含同一会话的多个修订"})
        return {"epoch": epoch, "rows": len(values), "new": new, "corrected": corrected,
                "duplicate": duplicate, "issues": issues, "can_import": not issues,
                "input_digest": digest([value.payload() for value in values]),
                "notice": "预览不占用会话修订；正式导入会重新核验当前状态。"}

    def ingest(self, request_id, sessions_csv, quality_csv=""):
        values = self.parse(sessions_csv, quality_csv)
        return self.intake.ingest(BatchInput(request_id=request_id, sessions=values))

    @staticmethod
    def export_rows(records):
        from evidence.contracts import iso
        sessions = io.StringIO(newline="")
        quality = io.StringIO(newline="")
        writer = csv.DictWriter(sessions, fieldnames=SESSION_COLUMNS, lineterminator="\n")
        quality_writer = csv.DictWriter(quality, fieldnames=QUALITY_COLUMNS, lineterminator="\n")
        writer.writeheader()
        quality_writer.writeheader()
        for record in records:
            payload = record["payload"]
            row = {key: payload[key] for key in SESSION_COLUMNS}
            row["start"] = iso(row["start"])
            row["end"] = iso(row["end"])
            row["withdrawn"] = "true" if row["withdrawn"] else "false"
            writer.writerow(row)
            for interval in payload["quality"]:
                quality_writer.writerow({"source": payload["source"], "session_id": payload["session_id"],
                                         "revision": payload["revision"], "start": iso(interval["start"]),
                                         "end": iso(interval["end"]), "reason": interval["reason"]})
        return {"sessions_csv": sessions.getvalue(), "quality_csv": quality.getvalue()}
