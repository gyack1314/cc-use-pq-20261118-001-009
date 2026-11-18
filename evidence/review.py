from evidence.analysis import Analysis
from evidence.contracts import DomainError, digest


class Review:
    def __init__(self, store):
        self.store = store
        self.analysis = Analysis(store)

    def prepare(self, query):
        result = self.analysis.run(query)
        checks = self.checks(result)
        return {"epoch": result["epoch"], "query": result["query"], "checksum": result["checksum"],
                "checks": checks, "requires_acknowledgement": [c["id"] for c in checks if c["acknowledge"]],
                "review_digest": digest({"checksum": result["checksum"], "checks": checks})}

    @staticmethod
    def checks(result):
        checks = []
        for center in result["strata"]:
            name = center["center"]
            if center["subjects"] == 0:
                checks.append({"id": "empty:" + name, "kind": "empty_observation", "center": name,
                               "acknowledge": True, "message": "中心没有可观察对象，率值不可用。"})
            elif center["sessions_included"] == 0:
                checks.append({"id": "no-sessions:" + name, "kind": "no_received_sessions", "center": name,
                               "acknowledge": True, "message": "中心没有纳入会话，零值仅表示已收到记录。"})
            if center["quality_lost_seconds"]:
                checks.append({"id": "quality:" + name, "kind": "quality_loss", "center": name,
                               "acknowledge": True, "seconds": center["quality_lost_seconds"],
                               "message": "有效使用已扣除没有其他有效会话覆盖的低质量时段。"})
            if center["overlap_seconds"]:
                checks.append({"id": "overlap:" + name, "kind": "overlap_deduplicated", "center": name,
                               "acknowledge": False, "seconds": center["overlap_seconds"],
                               "message": "同一对象的重叠有效时段已取并集。"})
        if result["total"]["subjects"] == 0:
            checks.append({"id": "empty:all", "kind": "empty_analysis", "acknowledge": True,
                           "message": "整个窗口无有效观察分母；不能解释为零使用率。"})
        return checks

    def validate(self, connection, query, review_digest, acknowledgements):
        result = self.analysis.in_transaction(connection, query)
        checks = self.checks(result)
        actual = digest({"checksum": result["checksum"], "checks": checks})
        if actual != review_digest:
            raise DomainError("review_stale", "复核后数据或口径发生变化")
        needed = {check["id"] for check in checks if check["acknowledge"]}
        provided = set(acknowledgements)
        if len(provided) != len(acknowledgements):
            raise DomainError("review_duplicate", "确认项不能重复", 422)
        if provided != needed:
            raise DomainError("review_incomplete", "须准确确认当前所有解释限制", 422,
                              {"missing": sorted(needed - provided), "unknown": sorted(provided - needed)})
        return {"digest": actual, "acknowledgements": sorted(provided), "checks": checks}

    def inspect_release(self, release):
        result = release["result"]
        checks = self.checks(result)
        interpretation = result["interpretation"]
        findings = []
        if not interpretation.get("missing"):
            findings.append("missing_interpretation")
        if result["total"]["state"] == "unavailable" and result["total"]["usage_fraction"] is not None:
            findings.append("invalid_empty_ratio")
        for center in result["strata"]:
            if center["subjects_with_usage"] > center["subjects"]:
                findings.append("subject_count:" + center["center"])
            if center["active_subject_days"] > center["observed_subject_days"]:
                findings.append("active_days:" + center["center"])
        return {"release_id": release["release_id"], "checks": checks,
                "findings": findings, "ok": not findings}
