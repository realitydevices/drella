"""Database tables."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, Text, UniqueConstraint
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
    created_at: Mapped[datetime] = mapped_column(default=_now)


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


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"))
    prompt: Mapped[str] = mapped_column(Text)
    repo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model: Mapped[str] = mapped_column(String(200))
    endpoint: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default=QUEUED, index=True)
    # What the job reported via POST /jobs/{id}/status. It becomes `status` once
    # the Kubernetes Job has finished and its log is archived.
    reported_status: Mapped[str | None] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(Text)
    # sha256 of the job-scoped token.
    job_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=_now, index=True)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
