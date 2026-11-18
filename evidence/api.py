import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from evidence.analysis import Analysis
from evidence.contracts import AnalysisInput, BatchInput, DomainError, ReleaseInput, SubjectInput, TablesInput, PeriodInput
from evidence.intake import Intake
from evidence.registry import Registry
from evidence.releases import Releases
from evidence.store import Store


def create_app(path=None):
    database_path = path or os.environ.get("EVIDENCE_DB", "data/evidence.sqlite3")

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(database_path)
        yield

    app = FastAPI(title="植入设备使用证据核算", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exception: DomainError):
        return JSONResponse(status_code=exception.status, content={"error": exception.as_dict()})

    def store(request):
        return request.app.state.store

    @app.get("/health")
    def health(request: Request):
        return {"status": "ok", **store(request).status()}

    @app.get("/integrity")
    def integrity(request: Request):
        return store(request).integrity()

    @app.put("/subjects/{subject_id}")
    def register(subject_id: str, value: SubjectInput, request: Request):
        if subject_id != value.subject_id:
            raise DomainError("path_mismatch", "路径和正文对象不一致", 422)
        return Registry(store(request)).upsert(value)

    @app.get("/subjects")
    def subjects(request: Request, center: str | None = None,
                 after: str = "", limit: int = Query(default=100, ge=1, le=500)):
        return {"subjects": Registry(store(request)).list(center, after, limit)}

    @app.get("/subjects/{subject_id}")
    def subject(subject_id: str, request: Request):
        return Registry(store(request)).get(subject_id)

    @app.get("/subjects/{subject_id}/history")
    def subject_history(subject_id: str, request: Request):
        return {"versions": Registry(store(request)).history(subject_id)}

    @app.post("/sessions/batches")
    def ingest(value: BatchInput, request: Request):
        return Intake(store(request)).ingest(value)

    @app.get("/sessions/sources")
    def sources(request: Request):
        return {"sources": Intake(store(request)).summarize()}

    @app.get("/sessions/sources/{source}")
    def source_sessions(source: str, request: Request, after: str = "",
                        limit: int = Query(default=100, ge=1, le=500)):
        return {"sessions": Intake(store(request)).export(source, after, limit)}

    @app.get("/sessions/{source}/{session_id}")
    def session(source: str, session_id: str, request: Request):
        return Intake(store(request)).get(source, session_id)

    @app.post("/analysis")
    def analyze(value: AnalysisInput, request: Request):
        return Analysis(store(request)).run(value)

    @app.post("/analysis/subjects/{subject_id}")
    def explain(subject_id: str, value: AnalysisInput, request: Request):
        return Analysis(store(request)).subject(value, subject_id)

    @app.post("/analysis/inspection")
    def inspect(value: AnalysisInput, request: Request):
        from evidence.inspection import Inspection
        return Inspection(store(request)).run(value)

    @app.post("/releases", status_code=201)
    def release(value: ReleaseInput, request: Request):
        return Releases(store(request)).create(value)

    @app.get("/releases")
    def releases(request: Request, after: str = "", limit: int = Query(default=100, ge=1, le=500)):
        return {"releases": Releases(store(request)).list(after, limit)}

    @app.get("/releases/compare")
    def compare(request: Request, first: str, second: str):
        return Releases(store(request)).compare(first, second)

    @app.get("/releases/{release_id}")
    def get_release(release_id: str, request: Request):
        return Releases(store(request)).get(release_id)

    @app.get("/releases/{release_id}/verify")
    def verify_release(release_id: str, request: Request):
        return Releases(store(request)).verify(release_id)

    @app.get("/releases/{release_id}/current-difference")
    def difference(release_id: str, request: Request):
        return Releases(store(request)).compare_current(release_id)

    @app.get("/releases/{release_id}/centers.csv")
    def csv(release_id: str, request: Request):
        content = Releases(store(request)).csv(release_id)
        return Response(content, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="centers.csv"'})

    @app.get("/audit")
    def audit(request: Request, after: int = Query(default=0, ge=0),
              limit: int = Query(default=100, ge=1, le=500)):
        return {"events": store(request).audit_log(after, limit)}

    @app.post("/tables/preview")
    def preview_tables(value: TablesInput, request: Request):
        from evidence.tabular import Tabular
        return Tabular(store(request)).preview(value.sessions_csv, value.quality_csv)

    @app.post("/tables/import")
    def import_tables(value: TablesInput, request: Request):
        from evidence.tabular import Tabular
        return Tabular(store(request)).ingest(value.request_id, value.sessions_csv, value.quality_csv)

    @app.get("/tables/export/{source}")
    def export_tables(source: str, request: Request, after: str = "", limit: int = Query(default=100, ge=1, le=500)):
        from evidence.tabular import Tabular
        return Tabular.export_rows(Intake(store(request)).export(source, after, limit))

    @app.post("/analysis/periods")
    def compare_periods(value: PeriodInput, request: Request):
        from evidence.periods import Periods
        periods = Periods(store(request))
        result = periods.compare(value.before, value.after)
        return {"comparison": result, "daily": periods.daily_series(result),
                "directions": periods.subject_changes(result)}

    @app.post("/analysis/review")
    def review(value: AnalysisInput, request: Request):
        from evidence.review import Review
        return Review(store(request)).prepare(value)

    @app.get("/releases/{release_id}/reconciliation")
    def reconciliation(release_id: str, request: Request):
        from evidence.inspection import Inspection
        from evidence.review import Review
        database = store(request)
        release = Releases(database).get(release_id)
        return {"accounting": Inspection(database).reconcile_release(release),
                "interpretation": Review(database).inspect_release(release)}

    @app.get("/releases/{release_id}/lineage")
    def lineage(release_id: str, request: Request):
        from evidence.lineage import Lineage
        return Lineage(store(request)).release(release_id)

    @app.get("/releases/{release_id}/rebuild")
    def rebuild(release_id: str, request: Request):
        from evidence.lineage import Lineage
        return Lineage(store(request)).rebuild(release_id)

    @app.get("/releases/{release_id}/bundle.zip")
    def bundle(release_id: str, request: Request):
        from evidence.exports import Exports
        return Response(Exports(store(request)).bundle(release_id), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="evidence.zip"'})

    @app.get("/impact/sessions/{source}/{session_id}")
    def session_impact(source: str, session_id: str, request: Request):
        from evidence.lineage import Lineage
        return Lineage(store(request)).session_impact(source, session_id)

    @app.get("/impact/subjects/{subject_id}")
    def subject_impact(subject_id: str, request: Request):
        from evidence.lineage import Lineage
        return Lineage(store(request)).subject_impact(subject_id)

    return app


app = create_app()
