from evidence.analysis import Analysis, ratio
from evidence.contracts import digest


class Inspection:
    def __init__(self, store):
        self.analysis = Analysis(store)

    def run(self, query):
        result = self.analysis.run(query)
        issues = []
        decisions = {}
        centers = {}
        for person in result["subjects"]:
            center = centers.setdefault(person["center"], {"observed": 0, "without_sessions": 0,
                                                           "with_overlap": 0, "quality_affected": 0})
            if person["eligible"]:
                center["observed"] += 1
                if not person["sessions_included"]:
                    center["without_sessions"] += 1
                    issues.append({"subject_id": person["subject_id"], "kind": "no_received_usage",
                                   "severity": "notice", "seconds": person["observation_seconds"]})
            if person["overlap_seconds"]:
                center["with_overlap"] += 1
                issues.append({"subject_id": person["subject_id"], "kind": "overlapping_valid_sessions",
                               "severity": "information", "seconds": person["overlap_seconds"]})
            if person["quality_lost_seconds"]:
                center["quality_affected"] += 1
                issues.append({"subject_id": person["subject_id"], "kind": "quality_excluded",
                               "severity": "information", "seconds": person["quality_lost_seconds"]})
            for trace in person["traces"]:
                key = trace["decision"]
                decisions[key] = decisions.get(key, 0) + 1
                if key == "outside_observation":
                    issues.append({"subject_id": person["subject_id"], "kind": key,
                                   "source": trace["source"], "session_id": trace["session_id"],
                                   "severity": "notice"})
        return {"epoch": result["epoch"], "analysis_checksum": result["checksum"],
                "decisions": dict(sorted(decisions.items())), "centers": centers,
                "issues": issues,
                "limits": ["无会话是数据缺口提示，不代表设备未使用或故障。",
                           "重叠可能来自同步记录；统计已去重，不自动删除来源。",
                           "质量损失按所有有效会话并集计算，不是逐条排除时长求和。"]}

    def reconcile_release(self, release):
        result = release["result"]
        subject_sum = {}
        daily_sum = {}
        for person in result["subjects"]:
            if not person["eligible"]:
                continue
            for key in ("observation_seconds", "usage_seconds", "quality_lost_seconds", "overlap_seconds"):
                subject_sum[key] = subject_sum.get(key, 0) + person[key]
            for day in person["days"]:
                daily_sum[day["date"]] = daily_sum.get(day["date"], 0) + day["usage_seconds"]
        checks = []
        for key, observed in subject_sum.items():
            expected = result["total"][key]
            checks.append({"metric": key, "expected": expected, "observed": observed, "ok": expected == observed})
        for day in result["total"]["daily"]:
            checks.append({"metric": "daily:" + day["date"], "expected": day["usage_seconds"],
                           "observed": daily_sum.get(day["date"], 0),
                           "ok": day["usage_seconds"] == daily_sum.get(day["date"], 0)})
        return {"release_id": release["release_id"], "checks": checks,
                "ok": all(check["ok"] for check in checks)}
