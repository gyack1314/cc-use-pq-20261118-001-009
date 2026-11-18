import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DomainError(Exception):
    def __init__(self, code, message, status=409, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须含时区")
    if value.microsecond:
        raise ValueError("时间精度为整秒")
    return int(value.timestamp())


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("标识符须为1至64位字母、数字、点、横线或下划线")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SubjectInput(StrictModel):
    subject_id: str
    center: str
    implanted_at: datetime
    ended_at: datetime | None = None
    reason: str = Field(min_length=1, max_length=400)
    expected_version: int = Field(ge=0)

    _ids = field_validator("subject_id", "center")(identifier)

    @field_validator("implanted_at", "ended_at")
    @classmethod
    def validate_time(cls, value):
        if value is not None:
            timestamp(value)
        return value

    @model_validator(mode="after")
    def valid_window(self):
        if self.ended_at is not None and self.ended_at <= self.implanted_at:
            raise ValueError("观察结束必须晚于植入时间")
        return self


class QualityInput(StrictModel):
    start: datetime
    end: datetime
    reason: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_range(self):
        if timestamp(self.start) >= timestamp(self.end):
            raise ValueError("质量排除区间必须为正")
        return self


class SessionInput(StrictModel):
    source: str
    session_id: str
    revision: int = Field(ge=1)
    subject_id: str
    start: datetime
    end: datetime
    mode: Literal["control", "training", "calibration"]
    quality: list[QualityInput] = Field(default_factory=list, max_length=200)
    withdrawn: bool = False
    note: str = Field(default="", max_length=400)

    _ids = field_validator("source", "session_id", "subject_id")(identifier)

    @model_validator(mode="after")
    def valid_session(self):
        start, end = timestamp(self.start), timestamp(self.end)
        if start >= end or end - start > 7 * 86400:
            raise ValueError("会话必须为正且不超过7天")
        for interval in self.quality:
            if timestamp(interval.start) < start or timestamp(interval.end) > end:
                raise ValueError("质量排除须包含在会话内")
        return self

    def payload(self):
        return {
            "source": self.source, "session_id": self.session_id,
            "revision": self.revision, "subject_id": self.subject_id,
            "start": timestamp(self.start), "end": timestamp(self.end),
            "mode": self.mode, "withdrawn": self.withdrawn, "note": self.note,
            "quality": sorted([
                {"start": timestamp(q.start), "end": timestamp(q.end), "reason": q.reason}
                for q in self.quality
            ], key=lambda q: (q["start"], q["end"], q["reason"])),
        }


class BatchInput(StrictModel):
    request_id: str
    sessions: list[SessionInput] = Field(min_length=1, max_length=500)

    _id = field_validator("request_id")(identifier)


class AnalysisInput(StrictModel):
    start: datetime
    end: datetime
    centers: list[str] = Field(default_factory=list, max_length=100)
    modes: list[Literal["control", "training", "calibration"]] = Field(default_factory=lambda: ["control"])
    active_day_seconds: int = Field(default=60, ge=1, le=86400)

    @model_validator(mode="after")
    def valid_query(self):
        start, end = timestamp(self.start), timestamp(self.end)
        if start >= end or end - start > 366 * 86400:
            raise ValueError("分析窗口须为正且不超过366天")
        if not self.modes or len(set(self.modes)) != len(self.modes):
            raise ValueError("模式不能为空或重复")
        if len(set(self.centers)) != len(self.centers):
            raise ValueError("中心不能重复")
        for center in self.centers:
            identifier(center)
        return self

    def payload(self):
        return {"start": timestamp(self.start), "end": timestamp(self.end),
                "centers": sorted(self.centers), "modes": sorted(self.modes),
                "active_day_seconds": self.active_day_seconds}


class ReleaseInput(StrictModel):
    request_id: str
    expected_epoch: int = Field(ge=0)
    query: AnalysisInput
    label: str = Field(min_length=1, max_length=120)
    review_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    acknowledgements: list[str] = Field(default_factory=list, max_length=200)

    _id = field_validator("request_id")(identifier)


class TablesInput(StrictModel):
    request_id: str
    sessions_csv: str = Field(max_length=4_000_000)
    quality_csv: str = Field(default="", max_length=4_000_000)

    _id = field_validator("request_id")(identifier)


class PeriodInput(StrictModel):
    before: AnalysisInput
    after: AnalysisInput
