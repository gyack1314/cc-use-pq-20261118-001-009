from evidence.contracts import digest, iso
from evidence.intervals import day_parts, duration, intersect, merge, subtract
from evidence.registry import Registry


class Exposure:
    def evaluate(self, subject, sessions, query):
        window = Registry.eligible(subject, query["start"], query["end"])
        observation = [window] if window else []
        accepted = []
        raw = []
        quality_removed = []
        traces = []
        by_mode = {}
        for session in sessions:
            trace = self.session_evidence(session, observation, query["modes"])
            traces.append(trace)
            if trace["decision"] == "included":
                accepted.extend(trace["valid_intervals"])
                raw.extend(trace["observed_intervals"])
                quality_removed.extend(trace["excluded_intervals"])
                by_mode.setdefault(session["mode"], []).extend(trace["valid_intervals"])
        valid = merge(accepted)
        gross = merge(raw)
        daily_observation = day_parts(observation)
        daily_usage = day_parts(valid)
        days = []
        for label, seconds in daily_observation.items():
            used = daily_usage.get(label, 0)
            threshold = min(query["active_day_seconds"], seconds)
            days.append({"date": label, "observation_seconds": seconds,
                         "usage_seconds": used, "active": used >= threshold,
                         "threshold_seconds": threshold})
        included = [trace for trace in traces if trace["decision"] == "included"]
        result = {
            "subject_id": subject["subject_id"], "center": subject["center"],
            "subject_version": subject["version"], "eligible": bool(window),
            "observation_intervals": observation, "usage_intervals": valid,
            "observation_seconds": duration(observation), "usage_seconds": duration(valid),
            "gross_seconds": duration(gross), "quality_lost_seconds": duration(subtract(gross, valid)),
            "overlap_seconds": sum(trace["valid_seconds"] for trace in included) - duration(valid),
            "active_days": sum(day["active"] for day in days),
            "observed_days": len(days), "days": days,
            "sessions_included": len(included), "sessions_seen": len(traces),
            "by_mode": {mode: duration(intervals) for mode, intervals in sorted(by_mode.items())},
            "traces": sorted(traces, key=lambda trace: (trace["source"], trace["session_id"])),
        }
        self.check(result)
        result["evidence_digest"] = digest(result)
        return result

    @staticmethod
    def session_evidence(session, observation, modes):
        observed = intersect([(session["start"], session["end"])], observation)
        rejected = intersect([(q["start"], q["end"]) for q in session["quality"]], observed)
        valid = subtract(observed, rejected)
        if session["withdrawn"]:
            decision = "withdrawn"
        elif session["mode"] not in modes:
            decision = "mode_excluded"
        elif not observed:
            decision = "outside_observation"
        else:
            decision = "included"
        counted = valid if decision == "included" else []
        return {"source": session["source"], "session_id": session["session_id"],
                "revision": session["revision"], "payload_digest": digest(session),
                "mode": session["mode"], "decision": decision,
                "start": iso(session["start"]), "end": iso(session["end"]),
                "observed_intervals": observed, "excluded_intervals": rejected,
                "valid_intervals": counted, "valid_seconds": duration(counted),
                "quality_reasons": sorted(set(q["reason"] for q in session["quality"]))}

    @staticmethod
    def check(result):
        if not 0 <= result["usage_seconds"] <= result["observation_seconds"]:
            raise ValueError("使用时长超出可观察时长")
        if result["usage_seconds"] + result["quality_lost_seconds"] != result["gross_seconds"]:
            raise ValueError("质量排除无法对账")
        if result["active_days"] > result["observed_days"]:
            raise ValueError("活跃天数超出可观察天数")
        if sum(day["usage_seconds"] for day in result["days"]) != result["usage_seconds"]:
            raise ValueError("日明细与总使用时长不一致")
        if result["overlap_seconds"] < 0:
            raise ValueError("重叠扣除为负")

    @staticmethod
    def explain(result):
        return {
            "subject_id": result["subject_id"], "evidence_digest": result["evidence_digest"],
            "observation": [{"start": iso(a), "end": iso(b)} for a, b in result["observation_intervals"]],
            "usage": [{"start": iso(a), "end": iso(b)} for a, b in result["usage_intervals"]],
            "accounting": {key: result[key] for key in (
                "observation_seconds", "usage_seconds", "gross_seconds", "quality_lost_seconds",
                "overlap_seconds", "observed_days", "active_days")},
            "decisions": result["traces"], "days": result["days"],
        }

    def reconcile(self, before, after):
        fields = ("observation_seconds", "usage_seconds", "active_days", "observed_days",
                  "quality_lost_seconds", "overlap_seconds", "sessions_included")
        changes = {field: {"before": before[field], "after": after[field],
                           "delta": after[field] - before[field]} for field in fields
                   if before[field] != after[field]}
        old = {(t["source"], t["session_id"]): t for t in before["traces"]}
        new = {(t["source"], t["session_id"]): t for t in after["traces"]}
        changed_sessions = []
        for key in sorted(old.keys() | new.keys()):
            a, b = old.get(key), new.get(key)
            if a != b:
                changed_sessions.append({"source": key[0], "session_id": key[1],
                                         "before_revision": a["revision"] if a else None,
                                         "after_revision": b["revision"] if b else None,
                                         "before_decision": a["decision"] if a else None,
                                         "after_decision": b["decision"] if b else None})
        return {"metrics": changes, "sessions": changed_sessions,
                "subject_changed": before["subject_version"] != after["subject_version"],
                "center_changed": before["center"] != after["center"]}
