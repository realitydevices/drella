"""Database tables."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # sha256(token); the token itself is never stored [19].
    lookup_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Per-token salt for deriving the credential key.
    salt: Mapped[bytes] = mapped_column(LargeBinary(16))
    # What the GitHub App tokens minted for this token's jobs may do: a name in
    # drella.github.PROFILES. Set by an admin with the CLI, never through the API.
    github_profile: Mapped[str] = mapped_column(String(32), default="reader")
    # What the token may do in Drella itself: MEMBER or OPERATOR (issue #74). An
    # operator token's jobs get a job-scoped API token. Set only with the CLI.
    role: Mapped[str] = mapped_column(String(16), default="member")
    created_at: Mapped[datetime] = mapped_column(default=_now)


MEMBER, OPERATOR = "member", "operator"
ROLES = (MEMBER, OPERATOR)


class Credential(Base):
    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("token_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
TERMINAL = (SUCCEEDED, FAILED)
JOB_STATUSES = (QUEUED, RUNNING, SUCCEEDED, FAILED)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"))
    prompt: Mapped[str] = mapped_column(Text)
    repo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The PR this job fixes (a job with a repo and a PR-opening profile), if any.
    pr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The job's model, snapshotted from the models table when the job was created
    # [27], so later changes to the model don't rewrite the job's history.
    model: Mapped[str] = mapped_column(String(200))  # the alias
    interface: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(500))  # None for claude-code
    model_id: Mapped[str] = mapped_column(String(200))
    credential: Mapped[str | None] = mapped_column(String(200))  # name, not the key
    context_length: Mapped[int | None]
    max_output_tokens: Mapped[int | None]
    cost_in_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    cost_out_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    # True: the job's GitHub token is minted by Drella's GitHub App at launch, with
    # the permissions of its token's GitHub profile (as it is at launch), because the
    # caller stored no `github` credential. The token itself is never stored.
    github_app: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=QUEUED, index=True)
    # What the job reported via POST /jobs/{id}/status. It becomes `status` once
    # the Kubernetes Job has finished and its log is archived.
    reported_status: Mapped[str | None] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(Text)
    # Token usage and cost, as the wrapper reported them with the final status [46].
    # All null when the job reported no usage. The cost is the agent's own figure, or
    # computed from the model snapshot's prices.
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger)
    peak_context_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    # sha256 of the job-scoped token.
    job_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # The job that queued this one with its API token (issue #74), if any.
    parent_job_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The GitHub profile of a job queued with an API token: reader or builder, chosen by
    # its parent. Null for other jobs, which use their Drella token's profile.
    github_profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # An operator token's job only: sha256 of its job-scoped API token (never the token)
    # and the caller's credential key, wrapped with a key derived from that API token, so
    # the job can create jobs that use the caller's credentials. The wrapped key is
    # erased when the job finishes.
    api_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    api_key_wrap: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now, index=True)
    # Transient launch failures so far, and the earliest time to try again.
    launch_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    not_before: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class Model(Base):
    """A model a token's jobs can use, by alias [27]. Models are per token until owners
    and roles exist (O2): only the token that registered a model can see, change, delete
    or use it, and two tokens can each have their own model with the same alias."""

    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("owner_token_id", "alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(63))
    interface: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(500))  # None for claude-code
    model_id: Mapped[str] = mapped_column(String(200))
    # Name of the caller's credential holding the API key, if the model needs one.
    credential: Mapped[str | None] = mapped_column(String(63))
    context_length: Mapped[int | None]
    max_output_tokens: Mapped[int | None]
    cost_in_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    cost_out_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    notes: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)
