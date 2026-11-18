from statistics import median

from evidence.contracts import DomainError, digest, iso
from evidence.exposure import Exposure
from evidence.intake import Intake
from evidence.registry import Registry
from evidence.store import Store


def ratio(numerator, denominator):
    return round(numerator / denominator, 8) if denominator else None


class Analysis:
    def __init__(self, store: Store):
        self.store = store
        self.exposure = Exposure()

    def run(self, query):
        with self.store.transaction() as connection:
            return self.in_transaction(connection, query.payload())

    def in_transaction(self, connection, query):
        subjects = Registry.snapshot(connection, query["centers"])
        sessions = Intake.heads(connection, [s["subject_id"] for s in subjects], query["start"], query["end"])
        grouped = {}
        for session in sessions:
            grouped.setdefault(session["subject_id"], []).append(session)
        people = [self.exposure.evaluate(s, grouped.get(s["subject_id"], []), query) for s in subjects]
        eligible = [person for person in people if person["eligible"]]
        centers = sorted(set(query["centers"]) | {s["center"] for s in subjects})
        strata = []
        for center in centers:
            selected = [person for person in eligible if person["center"] == center]
            strata.append({"center": center, **self.aggregate(selected)})
        total = self.aggregate(eligible)
        self.check_strata(total, strata)
        warnings = []
        if not eligible:
            warnings.append("no_eligible_observation")
        for stratum in strata:
            if stratum["subjects"] == 0:
                warnings.append("empty_center:" + stratum["center"])
            elif stratum["sessions_included"] == 0:
                warnings.append("no_sessions:" + stratum["center"])
        result = {
            "epoch": self.store.epoch(connection), "query": query,
            "window": {"start": iso(query["start"]), "end": iso(query["end"]), "boundary": "[start,end)"},
            "total": total, "strata": strata, "subjects": people,
            "warnings": warnings, "interpretation": {
                "usage": "每个对象的有效会话先扣本会话质量排除，再取时间并集；不同对象相加。",
                "observation": "植入至结束与查询窗口相交的秒数；不足整日仍按实际秒数计。",
                "active_day": "UTC日内有效使用达到min(设定阈值,该日可观察秒数)。",
                "missing": "没有会话不证明未使用；当前结果只表述已收到的记录。",
                "modes": "模式明细可能重叠，不可直接相加作为去重后的总时长。",
            },
        }
        result["checksum"] = digest(result)
        return result

    @staticmethod
    def aggregate(people):
        observation = sum(p["observation_seconds"] for p in people)
        usage = sum(p["usage_seconds"] for p in people)
        days = sum(p["observed_days"] for p in people)
        active = sum(p["active_days"] for p in people)
        daily = {}
        for person in people:
            for day in person["days"]:
                item = daily.setdefault(day["date"], {"date": day["date"], "observed_subjects": 0,
                                                      "active_subjects": 0, "usage_seconds": 0,
                                                      "observation_seconds": 0})
                item["observed_subjects"] += 1
                item["active_subjects"] += int(day["active"])
                item["usage_seconds"] += day["usage_seconds"]
                item["observation_seconds"] += day["observation_seconds"]
        rates = [p["usage_seconds"] / p["observation_seconds"] for p in people]
        return {
            "state": "available" if observation else "unavailable",
            "subjects": len(people), "subjects_with_usage": sum(p["usage_seconds"] > 0 for p in people),
            "observation_seconds": observation, "usage_seconds": usage,
            "observation_days": round(observation / 86400, 8), "usage_hours": round(usage / 3600, 8),
            "usage_fraction": ratio(usage, observation), "observed_subject_days": days,
            "active_subject_days": active, "active_day_fraction": ratio(active, days),
            "median_subject_usage_fraction": round(median(rates), 8) if rates else None,
            "quality_lost_seconds": sum(p["quality_lost_seconds"] for p in people),
            "overlap_seconds": sum(p["overlap_seconds"] for p in people),
            "sessions_included": sum(p["sessions_included"] for p in people),
            "daily": [daily[date] for date in sorted(daily)],
        }

    @staticmethod
    def check_strata(total, strata):
        for key in ("subjects", "subjects_with_usage", "observation_seconds", "usage_seconds",
                    "observed_subject_days", "active_subject_days", "quality_lost_seconds",
                    "overlap_seconds", "sessions_included"):
            if total[key] != sum(s[key] for s in strata):
                raise ValueError("中心汇总不守恒：" + key)

    def subject(self, query, subject_id):
        result = self.run(query)
        for person in result["subjects"]:
            if person["subject_id"] == subject_id:
                return {"epoch": result["epoch"], **self.exposure.explain(person)}
        raise DomainError("subject_not_selected", "对象不在本次筛选内", 404)

    @staticmethod
    def compare_results(first, second):
        if first["query"] != second["query"]:
            raise DomainError("query_mismatch", "不同口径不能直接比较")
        fields = ("subjects", "observation_seconds", "usage_seconds", "active_subject_days",
                  "observed_subject_days", "quality_lost_seconds", "overlap_seconds")
        return {"before_epoch": first["epoch"], "after_epoch": second["epoch"],
                "changes": {field: {"before": first["total"][field], "after": second["total"][field],
                                     "delta": second["total"][field] - first["total"][field]}
                            for field in fields if first["total"][field] != second["total"][field]},
                "same_evidence": first["checksum"] == second["checksum"]}

    def compare_subjects(self, first, second):
        before = {p["subject_id"]: p for p in first["subjects"]}
        after = {p["subject_id"]: p for p in second["subjects"]}
        changes = []
        for subject_id in sorted(before.keys() | after.keys()):
            a, b = before.get(subject_id), after.get(subject_id)
            if a is None or b is None:
                changes.append({"subject_id": subject_id, "membership": "added" if a is None else "removed"})
            elif a["evidence_digest"] != b["evidence_digest"]:
                changes.append({"subject_id": subject_id, **self.exposure.reconcile(a, b)})
        return changes
