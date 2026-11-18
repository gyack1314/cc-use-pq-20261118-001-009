import copy
import csv
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

from evidence.analysis import Analysis
from evidence.api import create_app
from evidence.contracts import AnalysisInput, BatchInput, DomainError, ReleaseInput, SessionInput, SubjectInput
from evidence.exports import Exports
from evidence.inspection import Inspection
from evidence.intake import Intake
from evidence.lineage import Lineage
from evidence.periods import Periods
from evidence.registry import Registry
from evidence.releases import Releases
from evidence.review import Review
from evidence.store import Store
from evidence.tabular import Tabular


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data.sqlite3"
        self.store = Store(self.path)
        self.registry = Registry(self.store)
        self.intake = Intake(self.store)
        self.analysis = Analysis(self.store)
        self.releases = Releases(self.store)
        self.counter = 0
        self.subject()

    def subject(self, subject_id="p1", center="A", start="2026-10-01T00:00:00Z", end=None, version=0):
        return self.registry.upsert(SubjectInput(subject_id=subject_id, center=center,
                                               implanted_at=start, ended_at=end,
                                               reason="合成资料", expected_version=version))

    def session(self, session_id="s1", start="2026-10-01T01:00:00Z", end="2026-10-01T03:00:00Z", **kw):
        return SessionInput.model_validate({"source": "device", "session_id": session_id, "revision": 1,
                                            "subject_id": "p1", "start": start, "end": end,
                                            "mode": "control", **kw})

    def ingest(self, *sessions, request=None):
        self.counter += 1
        return self.intake.ingest(BatchInput(request_id=request or f"req-{self.counter}", sessions=list(sessions)))

    def query(self, **kw):
        return AnalysisInput.model_validate({"start": "2026-10-01T00:00:00Z",
                                             "end": "2026-10-02T00:00:00Z", **kw})

    def release(self, request="publish", query=None, **kw):
        return self.releases.create(ReleaseInput(request_id=request, label="合成结果",
                                               expected_epoch=self.store.status()["epoch"],
                                               query=query or self.query(), **kw))

    def test_overlapping_quality_correction_propagates_to_release(self):
        first = self.session(quality=[{"start": "2026-10-01T01:00:00Z",
                                       "end": "2026-10-01T01:30:00Z", "reason": "噪声"}])
        second = self.session("s2", "2026-10-01T02:00:00Z", "2026-10-01T04:00:00Z")
        self.ingest(first, second)
        before = self.analysis.run(self.query())
        self.assertEqual(before["total"]["usage_seconds"], 9000)
        self.assertEqual(before["total"]["quality_lost_seconds"], 1800)
        self.assertEqual(before["total"]["overlap_seconds"], 3600)
        frozen = self.release()
        self.ingest(self.session(revision=2, withdrawn=True))
        after = self.analysis.run(self.query())
        self.assertEqual(after["total"]["usage_seconds"], 7200)
        self.assertEqual(after["total"]["quality_lost_seconds"], 0)
        self.assertEqual(self.releases.get(frozen["release_id"]), frozen)
        delta = self.releases.compare_current(frozen["release_id"])
        self.assertEqual(delta["changes"]["usage_seconds"]["delta"], -1800)
        self.assertEqual(delta["subjects"][0]["sessions"][0]["after_decision"], "withdrawn")
        self.assertTrue(Lineage(self.store).rebuild(frozen["release_id"])["reproduced"])

    def test_registry_shortening_changes_denominator_and_usage(self):
        self.ingest(self.session())
        frozen = self.release()
        self.subject(end="2026-10-01T02:00:00Z", version=1)
        current = self.analysis.run(self.query())
        self.assertEqual(current["total"]["observation_seconds"], 7200)
        self.assertEqual(current["total"]["usage_seconds"], 3600)
        difference = self.releases.compare_current(frozen["release_id"])
        self.assertTrue(difference["subjects"][0]["subject_changed"])
        self.assertEqual(difference["changes"]["observation_seconds"]["before"], 86400)
        self.assertTrue(Lineage(self.store).rebuild(frozen["release_id"])["reproduced"])
        self.assertEqual(Lineage(self.store).release(frozen["release_id"])["subjects"][0]["version"], 1)
        self.assertTrue(self.releases.verify(frozen["release_id"])["valid"])

    def test_center_correction_moves_whole_denominator(self):
        self.subject("p2", "B")
        self.ingest(self.session())
        first = self.release()
        self.subject(center="B", version=1)
        second = self.release("second")
        self.assertEqual(second["result"]["strata"][0]["subjects"], 2)
        difference = self.releases.compare(first["release_id"], second["release_id"])
        self.assertTrue(difference["subjects"][0]["center_changed"])
        self.assertEqual(difference["changes"], {})

    def test_idempotent_batch_does_not_advance_epoch(self):
        value = self.session()
        first = self.ingest(value, request="same")
        second = self.ingest(value, request="same")
        self.assertEqual(first, second)
        third = self.ingest(value)
        self.assertEqual(third["epoch"], first["epoch"])
        self.assertEqual(third["results"][0]["status"], "duplicate")
        self.assertEqual(len(self.intake.get("device", "s1")["versions"]), 1)

    def test_request_conflict_rolls_back(self):
        self.ingest(self.session(), request="same")
        with self.assertRaises(DomainError) as raised:
            self.ingest(self.session("s2"), request="same")
        self.assertEqual(raised.exception.code, "request_conflict")
        self.assertEqual(self.store.status()["session_heads"], 1)
        self.assertEqual(self.store.status()["epoch"], 2)

    def test_atomic_batch_with_unknown_subject(self):
        with self.assertRaises(DomainError):
            self.ingest(self.session(), self.session("bad", subject_id="missing"))
        self.assertEqual(self.store.status()["session_heads"], 0)
        self.assertEqual(self.store.status()["epoch"], 1)
        self.assertEqual(len(self.store.audit_log()), 1)

    def test_atomic_batch_with_revision_conflict(self):
        self.ingest(self.session())
        with self.assertRaises(DomainError) as raised:
            self.ingest(self.session("s2"), self.session(note="changed"))
        self.assertEqual(raised.exception.code, "revision_conflict")
        self.assertEqual(self.store.status()["session_heads"], 1)
        self.assertEqual(self.store.status()["epoch"], 2)

    def test_historical_duplicate_does_not_replace_head(self):
        original = self.session()
        self.ingest(original)
        self.ingest(self.session(revision=3, withdrawn=True))
        replay = self.ingest(original)
        self.assertEqual(replay["results"][0]["status"], "duplicate")
        self.assertEqual(self.intake.get("device", "s1")["head"], 3)
        self.assertEqual(self.analysis.run(self.query())["total"]["usage_seconds"], 0)

    def test_unknown_stale_revision_refused(self):
        self.ingest(self.session(revision=3))
        with self.assertRaises(DomainError) as raised:
            self.ingest(self.session(revision=2))
        self.assertEqual(raised.exception.code, "stale_revision")
        self.assertEqual(self.intake.get("device", "s1")["head"], 3)

    def test_identity_cannot_change_in_revision(self):
        self.subject("p2")
        self.ingest(self.session())
        with self.assertRaises(DomainError) as raised:
            self.ingest(self.session(revision=2, subject_id="p2"))
        self.assertEqual(raised.exception.code, "identity_conflict")
        self.assertEqual(self.intake.get("device", "s1")["head"], 1)

    def test_duplicate_batch_keys_refused(self):
        with self.assertRaises(DomainError) as raised:
            self.ingest(self.session(), self.session(revision=2))
        self.assertEqual(raised.exception.code, "duplicate_key")
        self.assertEqual(self.store.status()["epoch"], 1)

    def test_subject_version_cas(self):
        with self.assertRaises(DomainError) as raised:
            self.subject(center="B", version=0)
        self.assertEqual(raised.exception.details["actual_version"], 1)
        self.assertEqual(self.registry.get("p1")["center"], "A")
        self.assertEqual(len(self.registry.history("p1")), 1)

    def test_zero_usage_and_no_observation_are_distinct(self):
        self.subject("p2", "B", start="2026-11-01T00:00:00Z")
        result = self.analysis.run(self.query())
        strata = {s["center"]: s for s in result["strata"]}
        self.assertEqual(strata["A"]["usage_fraction"], 0)
        self.assertIsNone(strata["B"]["usage_fraction"])
        self.assertEqual(strata["B"]["state"], "unavailable")
        self.assertIn("no_sessions:A", result["warnings"])
        self.assertIn("empty_center:B", result["warnings"])

    def test_quality_is_local_to_its_session(self):
        quality = [{"start": "2026-10-01T01:00:00Z", "end": "2026-10-01T03:00:00Z", "reason": "bad"}]
        self.ingest(self.session(quality=quality), self.session("s2"))
        total = self.analysis.run(self.query())["total"]
        self.assertEqual(total["usage_seconds"], 7200)
        self.assertEqual(total["quality_lost_seconds"], 0)
        self.assertEqual(total["overlap_seconds"], 0)

    def test_overlapping_quality_is_deduplicated(self):
        quality = [{"start": "2026-10-01T01:00:00Z", "end": "2026-10-01T02:00:00Z", "reason": "a"},
                   {"start": "2026-10-01T01:30:00Z", "end": "2026-10-01T02:30:00Z", "reason": "b"}]
        self.ingest(self.session(quality=quality))
        result = self.analysis.run(self.query())
        self.assertEqual(result["total"]["usage_seconds"], 1800)
        self.assertEqual(result["total"]["quality_lost_seconds"], 5400)
        self.assertEqual(result["subjects"][0]["traces"][0]["quality_reasons"], ["a", "b"])

    def test_midnight_split_and_partial_day_threshold(self):
        self.subject(start="2026-10-01T23:59:30Z", end="2026-10-02T00:00:20Z", version=1)
        self.ingest(self.session(start="2026-10-01T23:59:00Z", end="2026-10-02T00:01:00Z"))
        query = self.query(end="2026-10-03T00:00:00Z")
        result = self.analysis.run(query)
        self.assertEqual(result["total"]["usage_seconds"], 50)
        self.assertEqual(result["total"]["active_subject_days"], 2)
        self.assertEqual([d["usage_seconds"] for d in result["total"]["daily"]], [30, 20])
        self.assertEqual(result["total"]["observation_seconds"], 50)

    def test_touching_boundary_not_counted(self):
        self.ingest(self.session(start="2026-09-30T23:00:00Z", end="2026-10-01T00:00:00Z"))
        result = self.analysis.run(self.query())
        self.assertEqual(result["total"]["usage_seconds"], 0)
        self.assertEqual(result["subjects"][0]["sessions_seen"], 0)

    def test_mode_union_and_filter(self):
        self.ingest(self.session(), self.session("training", mode="training"))
        control = self.analysis.run(self.query())
        combined = self.analysis.run(self.query(modes=["control", "training"]))
        self.assertEqual(control["total"]["usage_seconds"], 7200)
        self.assertEqual(combined["total"]["usage_seconds"], 7200)
        self.assertEqual(combined["subjects"][0]["by_mode"], {"control": 7200, "training": 7200})
        self.assertEqual(combined["total"]["overlap_seconds"], 7200)
        self.assertEqual(control["subjects"][0]["traces"][1]["decision"], "mode_excluded")

    def test_unknown_center_not_silently_empty(self):
        with self.assertRaises(DomainError) as raised:
            self.analysis.run(self.query(centers=["typo"]))
        self.assertEqual(raised.exception.code, "center_missing")
        self.assertEqual(raised.exception.status, 422)

    def test_center_totals_are_weighted(self):
        self.subject("p2", "B", end="2026-10-01T02:00:00Z")
        self.ingest(self.session(subject_id="p2", start="2026-10-01T00:00:00Z", end="2026-10-01T02:00:00Z"))
        result = self.analysis.run(self.query())
        self.assertAlmostEqual(result["total"]["usage_fraction"], 7200 / 93600, places=7)
        self.assertEqual(result["total"]["median_subject_usage_fraction"], 0.5)
        self.assertEqual(result["total"]["subjects"], 2)

    def test_publish_cas_and_idempotence(self):
        query = self.query()
        epoch = self.analysis.run(query)["epoch"]
        self.ingest(self.session())
        with self.assertRaises(DomainError) as raised:
            self.releases.create(ReleaseInput(request_id="old", expected_epoch=epoch, query=query, label="x"))
        self.assertEqual(raised.exception.code, "stale_analysis")
        first = self.release()
        self.assertEqual(self.release(), first)
        with self.assertRaises(DomainError):
            self.releases.create(ReleaseInput(request_id="publish", expected_epoch=epoch, query=query, label="different"))
        self.assertEqual(self.store.status()["releases"], 1)

    def test_release_query_mismatch(self):
        first = self.release()
        second = self.release("second", self.query(modes=["training"]))
        with self.assertRaises(DomainError) as raised:
            self.releases.compare(first["release_id"], second["release_id"])
        self.assertEqual(raised.exception.code, "query_mismatch")

    def test_review_acknowledgements_bound_to_evidence(self):
        review = Review(self.store).prepare(self.query())
        self.assertEqual(review["requires_acknowledgement"], ["no-sessions:A"])
        with self.assertRaises(DomainError) as raised:
            self.release(review_digest=review["review_digest"])
        self.assertEqual(raised.exception.code, "review_incomplete")
        published = self.release(review_digest=review["review_digest"], acknowledgements=["no-sessions:A"])
        self.assertEqual(published["result"]["review"]["acknowledgements"], ["no-sessions:A"])
        self.assertTrue(self.releases.verify(published["release_id"])["valid"])
        self.ingest(self.session())
        with self.assertRaises(DomainError) as raised:
            self.release("stale-review", review_digest=review["review_digest"], acknowledgements=["no-sessions:A"])
        self.assertEqual(raised.exception.code, "review_stale")

    def test_release_bundle_is_reproducible_and_verifiable(self):
        self.ingest(self.session())
        published = self.release()
        export = Exports(self.store)
        first = export.bundle(published["release_id"])
        second = export.bundle(published["release_id"])
        self.assertEqual(first, second)
        self.assertTrue(export.verify_bundle(first)["valid"])
        self.assertEqual(export.verify_bundle(first)["files"], 8)
        subjects = list(csv.DictReader(io.StringIO(export.subjects(published["release_id"]))))
        self.assertEqual(subjects[0]["usage_seconds"], "7200")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(export.daily(published["release_id"]))))), 1)
        self.assertIn("included", export.decisions(published["release_id"]))

    def test_lineage_session_and_subject_impact(self):
        self.ingest(self.session())
        first = self.release()
        self.ingest(self.session(revision=2, withdrawn=True))
        second = self.release("second")
        lineage = Lineage(self.store)
        impacts = lineage.session_impact("device", "s1")["releases"]
        self.assertEqual([p["revision"] for p in impacts], [1, 2])
        self.assertEqual([p["decision"] for p in impacts], ["included", "withdrawn"])
        self.assertEqual(len(lineage.subject_impact("p1")["releases"]), 2)
        self.assertEqual(lineage.audit_release(first["release_id"])["epoch"], 2)
        self.assertEqual(lineage.rebuild(second["release_id"])["total"]["usage_seconds"], 0)

    def test_reopen_database_preserves_releases(self):
        self.ingest(self.session())
        frozen = self.release()
        reopened = Store(self.path)
        self.assertEqual(Releases(reopened).get(frozen["release_id"]), frozen)
        self.assertTrue(reopened.integrity()["ok"])
        self.assertEqual(Registry(reopened).get("p1")["version"], 1)
        self.assertEqual(Intake(reopened).get("device", "s1")["head"], 1)

    def test_concurrent_same_revision_has_one_winner(self):
        def submit(note):
            try:
                self.intake.ingest(BatchInput(request_id="parallel-" + note, sessions=[self.session(note=note)]))
                return "accepted"
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, ["one", "two"]))
        self.assertEqual(sorted(outcomes), ["accepted", "revision_conflict"])
        self.assertEqual(self.store.status()["epoch"], 2)
        self.assertEqual(len(self.intake.get("device", "s1")["versions"]), 1)

    def test_concurrent_subject_cas_one_winner(self):
        def update(center):
            try:
                self.subject(center=center, version=1)
                return "accepted"
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, ["B", "C"]))
        self.assertEqual(sorted(outcomes), ["accepted", "subject_version"])
        self.assertEqual(len(self.registry.history("p1")), 2)
        self.assertEqual(self.store.status()["epoch"], 2)

    def test_csv_roundtrip_preview_and_import(self):
        value = self.session(quality=[{"start": "2026-10-01T01:00:00Z", "end": "2026-10-01T02:00:00Z", "reason": "a,b"}])
        self.ingest(value)
        tables = Tabular.export_rows(self.intake.export("device"))
        tabular = Tabular(self.store)
        parsed = tabular.parse(**tables)
        self.assertEqual(parsed[0].payload(), value.payload())
        preview = tabular.preview(**tables)
        self.assertTrue(preview["can_import"])
        self.assertEqual(preview["duplicate"], 1)
        self.assertEqual(tabular.ingest("csv-replay", **tables)["results"][0]["status"], "duplicate")
        self.assertIn('"a,b"', tables["quality_csv"])

    def test_csv_orphan_quality_and_header_rejected(self):
        tabular = Tabular(self.store)
        tables = Tabular.export_rows([{"payload": self.session().payload()}])
        with self.assertRaises(DomainError) as raised:
            tabular.parse(tables["sessions_csv"].replace("source,", "source,source,"), "")
        self.assertEqual(raised.exception.code, "table_header")
        quality = "source,session_id,revision,start,end,reason\ndevice,other,1,2026-10-01T01:00:00Z,2026-10-01T02:00:00Z,bad\n"
        with self.assertRaises(DomainError) as raised:
            tabular.parse(tables["sessions_csv"], quality)
        self.assertEqual(raised.exception.code, "orphan_quality")
        self.assertEqual(self.store.status()["session_heads"], 0)

    def test_period_comparison_tracks_population_and_paired_rates(self):
        self.subject("p2", "A", start="2026-10-02T00:00:00Z")
        self.ingest(self.session(), self.session("s2", "2026-10-02T01:00:00Z", "2026-10-02T02:00:00Z"))
        periods = Periods(self.store)
        result = periods.compare(self.query(), self.query(start="2026-10-02T00:00:00Z", end="2026-10-03T00:00:00Z"))
        self.assertEqual(result["retained"], ["p1"])
        self.assertEqual(result["entered"], ["p2"])
        self.assertEqual(result["left"], [])
        self.assertLess(result["paired_mean_delta"], 0)
        self.assertEqual(periods.subject_changes(result)["decreased"], 1)
        self.assertEqual(len(periods.daily_series(result)), 2)

    def test_period_incompatible_filter_and_overlap_rejected(self):
        periods = Periods(self.store)
        with self.assertRaises(DomainError) as raised:
            periods.compare(self.query(), self.query())
        self.assertEqual(raised.exception.code, "period_overlap")
        with self.assertRaises(DomainError) as raised:
            periods.compare(self.query(), self.query(start="2026-10-02T00:00:00Z", end="2026-10-03T00:00:00Z", modes=["training"]))
        self.assertEqual(raised.exception.code, "period_definition")

    def test_inspection_is_advisory_and_reconciles(self):
        self.ingest(self.session(), self.session("s2"))
        inspection = Inspection(self.store)
        result = inspection.run(self.query())
        self.assertEqual(result["decisions"], {"included": 2})
        self.assertEqual(result["issues"][0]["kind"], "overlapping_valid_sessions")
        self.assertEqual(result["centers"]["A"]["with_overlap"], 1)
        release = self.release()
        self.assertTrue(inspection.reconcile_release(release)["ok"])
        self.assertTrue(Review(self.store).inspect_release(release)["ok"])

    def test_http_flow_and_validation(self):
        with TestClient(create_app(self.path)) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["subjects"], 1)
            bad = client.post("/analysis", json={"start": "2026-10-01", "end": "2026-10-02"})
            self.assertEqual(bad.status_code, 422)
            ingest = client.post("/sessions/batches", json={"request_id": "http", "sessions": [self.session().model_dump(mode="json")]})
            self.assertEqual(ingest.status_code, 200)
            result = client.post("/analysis", json=self.query().model_dump(mode="json"))
            self.assertEqual(result.json()["total"]["usage_seconds"], 7200)
            publish = client.post("/releases", json={"request_id": "http-publish", "expected_epoch": result.json()["epoch"],
                                                   "label": "HTTP", "query": self.query().model_dump(mode="json")})
            self.assertEqual(publish.status_code, 201)
            release_id = publish.json()["release_id"]
            self.assertTrue(client.get(f"/releases/{release_id}/rebuild").json()["reproduced"])
            self.assertEqual(client.get(f"/releases/{release_id}/bundle.zip").status_code, 200)
            self.assertEqual(client.get("/subjects/unknown").status_code, 404)
            self.assertEqual(client.get("/audit?limit=0").status_code, 422)
            self.assertTrue(client.get("/integrity").json()["ok"])

    def test_withdrawal_then_reinstatement_preserves_revision_chain(self):
        self.ingest(self.session())
        self.ingest(self.session(revision=2, withdrawn=True))
        withdrawn = self.release()
        self.ingest(self.session(revision=3, end="2026-10-01T02:00:00Z"))
        current = self.analysis.run(self.query())
        self.assertEqual(current["total"]["usage_seconds"], 3600)
        versions = self.intake.get("device", "s1")["versions"]
        self.assertEqual([v["payload"]["withdrawn"] for v in versions], [False, True, False])
        self.assertEqual(withdrawn["result"]["total"]["usage_seconds"], 0)
        self.assertTrue(Lineage(self.store).rebuild(withdrawn["release_id"])["reproduced"])

    def test_different_sources_share_time_but_not_identity_key(self):
        self.ingest(self.session(), self.session(source="other"))
        result = self.analysis.run(self.query())
        self.assertEqual(result["total"]["usage_seconds"], 7200)
        self.assertEqual(result["total"]["sessions_included"], 2)
        self.assertEqual(result["total"]["overlap_seconds"], 7200)
        self.assertEqual([s["source"] for s in self.intake.summarize()], ["device", "other"])
        self.assertEqual(self.store.status()["session_heads"], 2)

    def test_stale_csv_preview_rechecked_at_import(self):
        tables = Tabular.export_rows([{"payload": self.session().payload()}])
        tabular = Tabular(self.store)
        self.assertTrue(tabular.preview(**tables)["can_import"])
        self.ingest(self.session(note="concurrent"))
        self.assertFalse(tabular.preview(**tables)["can_import"])
        with self.assertRaises(DomainError) as raised:
            tabular.ingest("stale-csv", **tables)
        self.assertEqual(raised.exception.code, "revision_conflict")
        self.assertEqual(self.store.status()["epoch"], 2)

    def test_release_content_corruption_detected(self):
        published = self.release()
        with self.store.transaction(write=True) as connection:
            content = copy.deepcopy(published["result"])
            content["total"]["usage_seconds"] = 42
            connection.execute("UPDATE releases SET result=? WHERE release_id=?",
                               (json.dumps(content), published["release_id"]))
        with self.assertRaises(DomainError) as raised:
            self.releases.get(published["release_id"])
        self.assertEqual(raised.exception.code, "release_corrupt")

    def test_source_pagination_is_complete(self):
        self.ingest(self.session("a"), self.session("b"), self.session("c"))
        first = self.intake.export("device", limit=2)
        second = self.intake.export("device", after=first[-1]["payload"]["session_id"], limit=2)
        self.assertEqual([r["payload"]["session_id"] for r in first + second], ["a", "b", "c"])
        self.assertEqual(len(self.registry.list(limit=1)), 1)
        self.assertEqual(self.registry.list(after="p1"), [])

    def test_failed_write_transaction_restores_epoch(self):
        before = self.store.status()["epoch"]
        with self.assertRaises(RuntimeError):
            with self.store.transaction(write=True) as connection:
                self.store.advance(connection)
                self.store.audit(connection, before + 1, "test", "test", {"x": 1})
                raise RuntimeError("interrupted")
        self.assertEqual(self.store.status()["epoch"], before)
        self.assertEqual(len(self.store.audit_log()), 1)
        self.assertTrue(self.store.integrity()["ok"])

    def test_read_snapshot_excludes_concurrent_new_epoch(self):
        with self.store.transaction() as connection:
            first = self.analysis.in_transaction(connection, self.query().payload())
            self.ingest(self.session())
            second = self.analysis.in_transaction(connection, self.query().payload())
        self.assertEqual(first, second)
        current = self.analysis.run(self.query())
        self.assertGreater(current["epoch"], second["epoch"])
        self.assertEqual(second["total"]["usage_seconds"], 0)
        self.assertEqual(current["total"]["usage_seconds"], 7200)

    def test_review_duplicate_and_unknown_acknowledgement(self):
        review = Review(self.store).prepare(self.query())
        with self.assertRaises(DomainError) as raised:
            self.release(review_digest=review["review_digest"], acknowledgements=["no-sessions:A", "no-sessions:A"])
        self.assertEqual(raised.exception.code, "review_duplicate")
        with self.assertRaises(DomainError) as raised:
            self.release(review_digest=review["review_digest"], acknowledgements=["unknown"])
        self.assertEqual(raised.exception.details["missing"], ["no-sessions:A"])
        self.assertEqual(raised.exception.details["unknown"], ["unknown"])
        self.assertEqual(self.store.status()["releases"], 0)

    def test_validation_rejects_invalid_intervals_and_ids(self):
        for changes in ({"start": "2026-10-01T04:00:00Z"}, {"subject_id": "with space"},
                        {"start": "2026-10-01T01:00:00"}, {"end": "2026-10-01T03:00:00.001Z"},
                        {"quality": [{"start": "2026-10-01T00:00:00Z", "end": "2026-10-01T02:00:00Z", "reason": "bad"}]}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    self.session(**changes)
        with self.assertRaises(ValidationError):
            self.query(modes=[])
        with self.assertRaises(ValidationError):
            self.query(centers=["A", "A"])


if __name__ == "__main__":
    unittest.main()
