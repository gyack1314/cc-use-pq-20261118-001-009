from evidence.analysis import Analysis, ratio
from evidence.contracts import DomainError, digest


class Periods:
    def __init__(self, store):
        self.store = store
        self.analysis = Analysis(store)

    def compare(self, first, second):
        left, right = first.payload(), second.payload()
        if left["end"] > right["start"]:
            raise DomainError("period_overlap", "前期须结束于后期开始之前", 422)
        for field in ("centers", "modes", "active_day_seconds"):
            if left[field] != right[field]:
                raise DomainError("period_definition", "期间比较须使用相同筛选与日阈值", 422, {"field": field})
        with self.store.transaction() as connection:
            before = self.analysis.in_transaction(connection, left)
            after = self.analysis.in_transaction(connection, right)
        a = {s["subject_id"]: s for s in before["subjects"] if s["eligible"]}
        b = {s["subject_id"]: s for s in after["subjects"] if s["eligible"]}
        retained = sorted(a.keys() & b.keys())
        entered = sorted(b.keys() - a.keys())
        left_ids = sorted(a.keys() - b.keys())
        moved = [subject_id for subject_id in retained if a[subject_id]["center"] != b[subject_id]["center"]]
        paired = []
        for subject_id in retained:
            first_rate = a[subject_id]["usage_seconds"] / a[subject_id]["observation_seconds"]
            second_rate = b[subject_id]["usage_seconds"] / b[subject_id]["observation_seconds"]
            paired.append({"subject_id": subject_id, "before_fraction": round(first_rate, 8),
                           "after_fraction": round(second_rate, 8), "delta": round(second_rate - first_rate, 8),
                           "before_seconds": a[subject_id]["observation_seconds"],
                           "after_seconds": b[subject_id]["observation_seconds"]})
        strata_a = {s["center"]: s for s in before["strata"]}
        strata_b = {s["center"]: s for s in after["strata"]}
        strata = []
        for center in sorted(strata_a.keys() | strata_b.keys()):
            x, y = strata_a.get(center), strata_b.get(center)
            comparable = bool(x and y and x["observation_seconds"] and y["observation_seconds"])
            first_rate = x["usage_fraction"] if x else None
            second_rate = y["usage_fraction"] if y else None
            strata.append({"center": center, "state": "descriptive" if comparable else "unavailable",
                           "before_fraction": first_rate, "after_fraction": second_rate,
                           "delta": round(second_rate - first_rate, 8) if comparable else None,
                           "before_subjects": x["subjects"] if x else 0,
                           "after_subjects": y["subjects"] if y else 0})
        mean_delta = sum(p["delta"] for p in paired) / len(paired) if paired else None
        result = {"epoch": before["epoch"], "before_checksum": before["checksum"],
                  "after_checksum": after["checksum"], "before_query": left, "after_query": right,
                  "before_total": before["total"], "after_total": after["total"],
                  "retained": retained, "entered": entered, "left": left_ids, "moved_center": moved,
                  "paired": paired, "paired_mean_delta": round(mean_delta, 8) if mean_delta is not None else None,
                  "strata": strata, "state": "descriptive" if paired else "no_paired_observation",
                  "interpretation": "期间差异为已收到使用记录的描述比较；总体变化可能受观察人群构成影响，不作疗效或因果判断。"}
        result["checksum"] = digest(result)
        return result

    @staticmethod
    def daily_series(result):
        before = {day["date"]: day for day in result["before_total"]["daily"]}
        after = {day["date"]: day for day in result["after_total"]["daily"]}
        rows = []
        for phase, values in (("before", before), ("after", after)):
            for date, day in sorted(values.items()):
                rows.append({"phase": phase, "date": date, **day,
                             "usage_fraction": ratio(day["usage_seconds"], day["observation_seconds"]),
                             "active_fraction": ratio(day["active_subjects"], day["observed_subjects"])})
        return rows

    @staticmethod
    def subject_changes(result):
        increases = [p for p in result["paired"] if p["delta"] > 0]
        decreases = [p for p in result["paired"] if p["delta"] < 0]
        unchanged = [p for p in result["paired"] if p["delta"] == 0]
        return {"increased": len(increases), "decreased": len(decreases), "unchanged": len(unchanged),
                "paired": len(result["paired"]), "entered": len(result["entered"]), "left": len(result["left"]),
                "warning": "方向按各人可观察秒数归一；不能据人数方向推断总体时长方向。"}
