import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger, DateTime, Enum, ForeignKey, Index, MetaData, Numeric, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ---------- Enumerations (LLD §4.2) ----------

class JobState(StrEnum):
    QUEUED = "QUEUED"
    FETCHING = "FETCHING"
    SANDBOX_READY = "SANDBOX_READY"
    ANALYZED = "ANALYZED"
    DRAFTED = "DRAFTED"
    REFLECTING = "REFLECTING"
    PUBLISHING = "PUBLISHING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED})


class JobTrigger(StrEnum):
    OPENED = "opened"
    SYNCHRONIZE = "synchronize"
    REOPENED = "reopened"
    READY_FOR_REVIEW = "ready_for_review"
    COMMAND = "command"


class ToolSide(StrEnum):
    BASE = "base"
    HEAD = "head"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    VERIFIED = "verified"
    DOWNGRADED = "downgraded"
    DROPPED = "dropped"


class FeedbackKind(StrEnum):
    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"
    RESOLVED = "resolved"
    SUGGESTION_COMMITTED = "suggestion_committed"
    REPLIED = "replied"


class LLMPurpose(StrEnum):
    DRAFT = "draft"
    REVISE = "revise"
    PROOF_TEST = "proof_test"
    SUMMARY = "summary"


def _enum(e: type[StrEnum]) -> Enum:
    # Stored as VARCHAR (not a native PG enum) so new values need no migration
    return Enum(e, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x])


# ---------- Base ----------

class Base(DeclarativeBase):
    # Stable constraint names so Alembic autogenerate produces clean migrations
    metadata = MetaData(naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


# ---------- Tables (LLD §4.1) ----------

class Installation(Base):
    __tablename__ = "installations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # GitHub's ID
    account_login: Mapped[str] = mapped_column(String(255))
    account_type: Mapped[str] = mapped_column(String(32))
    plan: Mapped[str] = mapped_column(String(32), default="free")
    daily_quota: Mapped[int] = mapped_column(default=30)
    suspended: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = _now()


class Repo(Base):
    __tablename__ = "repos"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # GitHub's ID
    installation_id: Mapped[int] = mapped_column(ForeignKey("installations.id", ondelete="CASCADE"))
    full_name: Mapped[str] = mapped_column(String(255))
    private: Mapped[bool] = mapped_column(default=False)
    config_cache: Mapped[dict[str, Any] | None]
    enabled: Mapped[bool] = mapped_column(default=True)


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    __table_args__ = (
        UniqueConstraint("repo_id", "pr_number", "head_sha"),
        Index("ix_review_jobs_latest", "repo_id", "pr_number", text("created_at DESC")),
        Index("ix_review_jobs_active", "state",
              postgresql_where=text("state NOT IN ('DONE', 'FAILED', 'CANCELLED')")),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    pr_number: Mapped[int]
    base_sha: Mapped[str] = mapped_column(String(40))
    head_sha: Mapped[str] = mapped_column(String(40))
    state: Mapped[JobState] = mapped_column(_enum(JobState), default=JobState.QUEUED)
    trigger: Mapped[JobTrigger] = mapped_column(_enum(JobTrigger))
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0"))
    check_run_id: Mapped[int | None] = mapped_column(BigInteger)
    summary_comment_id: Mapped[int | None] = mapped_column(BigInteger)
    timings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    attempts: Mapped[int] = mapped_column(default=0)  # not in LLD: sweeper re-enqueues once
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())  # not in LLD: sweeper
    finished_at: Mapped[datetime | None]


class ToolRun(Base):
    __tablename__ = "tool_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("review_jobs.id", ondelete="CASCADE"), index=True)
    tool: Mapped[str] = mapped_column(String(32))
    side: Mapped[ToolSide] = mapped_column(_enum(ToolSide))
    exit_code: Mapped[int | None]
    duration_ms: Mapped[int] = mapped_column(default=0)
    timed_out: Mapped[bool] = mapped_column(default=False)
    log_uri: Mapped[str | None] = mapped_column(String(512))


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (Index("ix_findings_run_new", "tool_run_id", "is_new"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tool_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tool_runs.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(40))
    rule: Mapped[str] = mapped_column(String(128))
    path: Mapped[str] = mapped_column(String(1024))
    line: Mapped[int]
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    is_new: Mapped[bool] = mapped_column(default=False)


class CandidateComment(Base):
    __tablename__ = "candidate_comments"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("review_jobs.id", ondelete="CASCADE"), index=True)
    payload: Mapped[dict[str, Any]]
    history: Mapped[list[Any]] = mapped_column(default=list)
    iterations: Mapped[int] = mapped_column(default=0)
    final_status: Mapped[CandidateStatus] = mapped_column(_enum(CandidateStatus), default=CandidateStatus.PENDING)
    verified: Mapped[bool] = mapped_column(default=False)
    score: Mapped[float | None]


class CriticResult(Base):
    __tablename__ = "critic_results"
    __table_args__ = (Index("ix_critic_results_candidate_iter", "candidate_id", "iteration"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidate_comments.id", ondelete="CASCADE"))
    iteration: Mapped[int]
    critic: Mapped[str] = mapped_column(String(32))
    passed: Mapped[bool]
    feedback: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(default=0)


class PostedComment(Base):
    __tablename__ = "posted_comments"
    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_comments.id", ondelete="CASCADE"), unique=True)  # at most one per candidate
    github_comment_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    review_id: Mapped[int] = mapped_column(BigInteger)
    commit_sha: Mapped[str] = mapped_column(String(40))
    claim_hash: Mapped[str] = mapped_column(String(40), index=True)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[uuid.UUID] = _uuid_pk()
    posted_comment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posted_comments.id", ondelete="CASCADE"), index=True)
    kind: Mapped[FeedbackKind] = mapped_column(_enum(FeedbackKind))
    actor: Mapped[str] = mapped_column(String(255))
    at: Mapped[datetime] = _now()


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("review_jobs.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[LLMPurpose] = mapped_column(_enum(LLMPurpose))
    model: Mapped[str] = mapped_column(String(128))  # not in LLD: we switch between models
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64))


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event: Mapped[str] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = _now()