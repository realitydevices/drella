"""The HTTP API: the only way in [8]."""

import logging
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from drella import crypto
from drella.adapters import LLM_KEY_SECRET, Interface
from drella.config import Settings
from drella.db import make_engine, make_sessionmaker, migrate
from drella.github import GitHubApp
from drella.models import QUEUED, RUNNING, TERMINAL, Credential, Job, Model, Token
from drella.queue import Queue, archived_log, output_prefix
from drella.runner import JOB_TOKEN_KEY, KubernetesRunner, LogsUnavailable, Runner
from drella.storage import CHUNK, LocalStorage, NotFound, Storage

log = logging.getLogger(__name__)

# Credential names become Secret keys and file names under /creds.
NAME_RE = r"^[a-z0-9][a-z0-9._-]{0,62}$"
OUTPUT_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
REPO_RE = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
MAX_OUTPUT_BYTES = 100 * 1024 * 1024
GITHUB_CREDENTIAL = "github"
# Model rows end up in OpenCode's config, which substitutes {env:...} and {file:...}
# anywhere in its raw text, so "{" and "}" are never allowed (docs/v0.2.md#security).
# Both fields are printable ASCII only, an allow-list: no whitespace (ASCII or
# Unicode), control or format characters. Hostnames can be written in punycode.
# A base URL also can't hold userinfo ("@"), a query or a fragment ("?", "#"): they
# could carry secrets, and every job snapshot copies the URL. "[" and "]" are refused
# too: curl would read them as URL ranges in the preflight.
BASE_URL_RE = r"^https?://[A-Za-z0-9._~:/%!$&'()*+,;=-]+$"
MODEL_ID_RE = r"^[A-Za-z0-9._~:/%!$&'()*+,;=@-]+$"
# Models per token [27].
MAX_MODELS_PER_TOKEN = 200
# An API key is substituted into OpenCode's JSON config as raw text, so it can't hold
# anything that would break the JSON (and print the config, key included, to the log),
# nor "{" or "}", which OpenCode would substitute again ({file:/creds/github}).
BAD_KEY_CHARS = re.compile(r'[\x00-\x1f\x7f"\\{}]')
# Limits that fit the database columns (Integer, Numeric(12, 4)).
MAX_TOKENS = 10_000_000
MAX_COST = 100_000


# -- schemas -----------------------------------------------------------------


class CredentialIn(BaseModel):
    value: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def _strip(cls, v: str) -> str:
        # A key read from a file usually ends with a newline. Surrounding whitespace
        # is never part of a credential.
        v = v.strip()
        if not v:
            raise ValueError("the credential is empty")
        return v

    @field_validator("value")
    @classmethod
    def _no_null(cls, v):
        _no_null(v)
        return v


def _no_null(v: str) -> str:
    if "\x00" in v:
        raise ValueError("value must not contain NUL characters")
    return v


class JobIn(BaseModel):
    # Unknown fields are an error, so an old client still sending `endpoint` is told.
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    repo: str | None = Field(
        default=None,
        pattern=REPO_RE,
        description="GitHub repo as owner/name. Leave it out for a job with no repo (no PR).",
    )
    model: str = Field(pattern=NAME_RE, description="A registered model alias (PUT /models)")

    @field_validator("prompt")
    @classmethod
    def _no_null(cls, v):
        return _no_null(v)

    @field_validator("repo")
    @classmethod
    def _no_dot_names(cls, v: str | None) -> str | None:
        # "." and ".." are never owner or repo names; they'd end up in URL paths.
        if v is not None and any(part in (".", "..") for part in v.split("/")):
            raise ValueError("not a GitHub repo (owner/name)")
        return v


def check_model_credential(name: str | None) -> None:
    """A model's key can't be the GitHub credential: a model row would otherwise send
    the caller's GitHub token to its base URL."""
    if name == GITHUB_CREDENTIAL:
        raise ValueError(f"'{GITHUB_CREDENTIAL}' is reserved; it can't be a model's API key")


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interface: Interface
    # Required, except for claude-code models, which have none [28].
    base_url: str | None = Field(default=None, pattern=BASE_URL_RE, max_length=500)
    model_id: str = Field(pattern=MODEL_ID_RE, max_length=200)
    credential: str | None = Field(
        default=None,
        pattern=NAME_RE,
        description="Name of the caller's credential holding the API key, if needed",
    )
    context_length: int | None = Field(default=None, gt=0, le=MAX_TOKENS)
    max_output_tokens: int | None = Field(default=None, gt=0, le=MAX_TOKENS)
    cost_in_per_mtok: Decimal | None = Field(default=None, ge=0, le=MAX_COST, decimal_places=4)
    cost_out_per_mtok: Decimal | None = Field(default=None, ge=0, le=MAX_COST, decimal_places=4)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("notes")
    @classmethod
    def _no_null(cls, v):
        if v is not None:
            _no_null(v)
        return v

    @field_validator("base_url", mode="before")
    @classmethod
    def _no_trailing_slash(cls, v):
        # Normalised before it's checked, so what's stored is exactly what passed.
        return v.rstrip("/") if isinstance(v, str) else v

    @field_validator("credential")
    @classmethod
    def _not_reserved(cls, v: str | None) -> str | None:
        check_model_credential(v)
        return v

    @model_validator(mode="after")
    def _per_interface(self):
        if self.interface == Interface.CLAUDE_CODE:
            # Claude Code talks to Anthropic itself, with the OAuth token [28].
            if self.base_url is not None:
                raise ValueError("a claude-code model has no base_url (Claude Code talks to"
                                 " Anthropic itself)")
            if not self.credential:
                raise ValueError("a claude-code model needs a credential (the OAuth token from"
                                 " `claude setup-token`)")
            return self
        if self.base_url is None:
            raise ValueError(f"a {self.interface.value} model needs a base_url")
        # Never send a key in the clear.
        if self.credential and not self.base_url.startswith("https://"):
            raise ValueError("a model with a credential needs an https:// base_url")
        return self


class ModelOut(BaseModel):
    """One of the caller's models. Plain fields, no validation rules: a row that
    predates a rule is still listed rather than breaking GET /models."""

    alias: str
    interface: str
    base_url: str | None
    model_id: str
    credential: str | None
    context_length: int | None
    max_output_tokens: int | None
    cost_in_per_mtok: Decimal | None
    cost_out_per_mtok: Decimal | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class JobModelOut(BaseModel):
    """The job's model, as it was when the job was created."""

    alias: str
    interface: str
    base_url: str | None
    model_id: str
    credential: str | None
    context_length: int | None
    max_output_tokens: int | None
    cost_in_per_mtok: Decimal | None
    cost_out_per_mtok: Decimal | None


class JobOut(BaseModel):
    id: str
    status: str
    prompt: str
    repo: str | None
    model: JobModelOut
    message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    outputs: list[str] | None = None


class StatusIn(BaseModel):
    status: Literal["succeeded", "failed"]
    message: str | None = None

    @field_validator("message")
    @classmethod
    def _no_null(cls, v):
        if v is not None:
            _no_null(v)
        return v


def job_out(job: Job, outputs: list[str] | None = None) -> JobOut:
    return JobOut(
        id=job.id,
        status=job.status,
        prompt=job.prompt,
        repo=job.repo,
        model=JobModelOut(
            alias=job.model,
            interface=job.interface,
            base_url=job.base_url,
            model_id=job.model_id,
            credential=job.credential,
            context_length=job.context_length,
            max_output_tokens=job.max_output_tokens,
            cost_in_per_mtok=job.cost_in_per_mtok,
            cost_out_per_mtok=job.cost_out_per_mtok,
        ),
        message=job.message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        outputs=outputs,
    )


# -- app ---------------------------------------------------------------------


@dataclass
class Deps:
    settings: Settings
    sessions: sessionmaker
    runner: Runner
    storage: Storage
    queue: Queue
    # Drella's GitHub App, if configured: mints repo jobs' tokens (issue #50).
    github_app: GitHubApp | None = None


def create_app(
    settings: Settings | None = None,
    runner: Runner | None = None,
    storage: Storage | None = None,
    start_queue: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)
    storage = storage or LocalStorage(settings.storage_path)
    github_app = GitHubApp.from_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(engine)
        deps = app.state.deps
        if deps.runner is None:
            deps.runner = KubernetesRunner(settings)
            deps.queue.runner = deps.runner
        if start_queue:
            deps.queue.start()
        yield
        if start_queue:
            deps.queue.stop()

    app = FastAPI(title="Drella", version="0", lifespan=lifespan)
    queue = Queue(
        sessions,
        runner,
        storage,
        settings.queue_concurrency,
        settings.queue_interval,
        log_snapshot_seconds=settings.log_snapshot_seconds,
        stall_seconds=settings.queue_stall_seconds,
        github_app=github_app,
    )
    app.state.deps = Deps(settings, sessions, runner, storage, queue, github_app)
    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 echoes each bad value ("input"). Never do that: the value
        # may be a credential, and one that isn't valid text (a lone surrogate) can't
        # even be encoded, which turned the 422 into a 500.
        errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
    return app


# -- dependencies ------------------------------------------------------------


async def get_deps(request: Request) -> Deps:
    # Async, so /livez and anything else that needs only this doesn't wait for a free
    # worker thread (FastAPI runs plain `def` dependencies in the thread pool).
    return request.app.state.deps


def get_db(deps: Annotated[Deps, Depends(get_deps)]) -> Iterator[Session]:
    with deps.sessions() as s:
        yield s


def bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token", {"WWW-Authenticate": "Bearer"})
    return authorization.removeprefix("Bearer ").strip()


@dataclass
class Caller:
    token: Token
    secret: str  # the raw token: needed to derive the credential key


def caller(
    raw: Annotated[str, Depends(bearer)], db: Annotated[Session, Depends(get_db)]
) -> Caller:
    token = db.scalar(select(Token).where(Token.lookup_hash == crypto.lookup_hash(raw)))
    if token is None:
        raise HTTPException(401, "invalid token", {"WWW-Authenticate": "Bearer"})
    return Caller(token, raw)


def own_job(job_id: str, c: Caller, db: Session) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.token_id != c.token.id:
        raise HTTPException(404, "job not found")
    return job


def job_itself(
    job_id: str,
    raw: Annotated[str, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> Job:
    """The job's own pod, authenticated by its job-scoped token."""
    job = db.get(Job, job_id)
    if job is None or job.job_token_hash != crypto.lookup_hash(raw):
        raise HTTPException(401, "invalid job token")
    if job.status != RUNNING:
        raise HTTPException(409, f"job is {job.status}")
    return job


CallerDep = Annotated[Caller, Depends(caller)]
DbDep = Annotated[Session, Depends(get_db)]
DepsDep = Annotated[Deps, Depends(get_deps)]


# -- routes ------------------------------------------------------------------

router = APIRouter()


# The health checks are async, so they run on the event loop and still answer when
# every worker thread is busy (e.g. with log followers).
@router.get("/healthz")
async def healthz() -> dict:
    """Readiness: the API is serving."""
    return {"ok": True}


@router.get("/livez")
async def livez(deps: DepsDep) -> dict:
    """Liveness: fails if the queue loop has stalled, so Kubernetes restarts Drella."""
    if not deps.queue.alive():
        raise HTTPException(503, "the queue loop has stalled")
    return {"ok": True}


@router.put("/credentials/{name}", status_code=204)
def put_credential(
    name: Annotated[str, Path(pattern=NAME_RE)], body: CredentialIn, c: CallerDep, db: DbDep
) -> Response:
    ciphertext = crypto.encrypt(c.secret, c.token.salt, body.value)
    cred = db.scalar(
        select(Credential).where(Credential.token_id == c.token.id, Credential.name == name)
    )
    if cred is None:
        db.add(Credential(token_id=c.token.id, name=name, ciphertext=ciphertext))
    else:
        cred.ciphertext = ciphertext
    db.commit()
    return Response(status_code=204)


@router.delete("/credentials/{name}", status_code=204)
def delete_credential(
    name: Annotated[str, Path(pattern=NAME_RE)], c: CallerDep, db: DbDep
) -> Response:
    """E.g. a stored `github` credential, which otherwise always wins over Drella's
    GitHub App. Jobs already created keep the credentials they were given."""
    cred = db.scalar(
        select(Credential).where(Credential.token_id == c.token.id, Credential.name == name)
    )
    if cred is None:
        raise HTTPException(404, "no such credential")
    db.delete(cred)
    db.commit()
    return Response(status_code=204)


@router.get("/credentials")
def list_credentials(c: CallerDep, db: DbDep) -> list[str]:
    """Names only; values are never returned."""
    return list(
        db.scalars(
            select(Credential.name).where(Credential.token_id == c.token.id).order_by("name")
        )
    )


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, c: CallerDep, db: DbDep, deps: DepsDep) -> JobOut:
    # Only the caller's own models [27].
    model = own_model(body.model, c, db)
    if model is None:
        raise HTTPException(422, f"no model '{body.model}' (register it with PUT /models)")
    # The row is checked again with today's rules, so a row stored before a rule
    # existed can't launch.
    try:
        ModelIn(**model_fields(model))
    except ValueError:
        raise HTTPException(
            422,
            f"model '{model.alias}' no longer passes validation; register it again"
            " (PUT /models)",
        ) from None
    creds = {
        cred.name: cred
        for cred in db.scalars(select(Credential).where(Credential.token_id == c.token.id))
    }
    # Only a job with a repo needs git access: the caller's own `github` credential if
    # they stored one (it wins), otherwise a token from Drella's GitHub App, minted at
    # launch.
    github_app = bool(body.repo) and GITHUB_CREDENTIAL not in creds
    if github_app and deps.github_app is None:
        raise HTTPException(
            422,
            f"a job with a repo needs a '{GITHUB_CREDENTIAL}' credential (this Drella has no"
            " GitHub App to mint one)",
        )
    if model.credential:
        try:
            check_model_credential(model.credential)
        except ValueError as e:
            raise HTTPException(422, f"model '{model.alias}': {e}") from None
        if model.credential not in creds:
            raise HTTPException(
                422, f"model '{model.alias}' needs a '{model.credential}' credential (its API key)"
            )
    # The request carries the token, so this is when credentials are decrypted
    # [19]. They go straight into the job's Secret, never into the database. A job
    # gets only what it needs [10]: the GitHub credential for a repo job, and its
    # model's API key, if the model has one [27].
    def decrypt(name: str) -> str:
        # Stripped on read too, so credentials stored before stripping was added work.
        return crypto.decrypt(c.secret, c.token.salt, creds[name].ciphertext).strip()

    secrets = {}
    if body.repo and not github_app:
        secrets[GITHUB_CREDENTIAL] = decrypt(GITHUB_CREDENTIAL)
    if model.credential:
        key = decrypt(model.credential)
        # Never echo the key, not even in an error.
        if BAD_KEY_CHARS.search(key):
            raise HTTPException(
                422,
                f"the '{model.credential}' credential can't be used as an API key: it contains"
                " control characters, quotes, backslashes or braces",
            )
        secrets[LLM_KEY_SECRET] = key
    job_token = crypto.new_token(crypto.JOB_TOKEN_PREFIX)
    secrets[JOB_TOKEN_KEY] = job_token
    job = Job(
        token_id=c.token.id,
        prompt=body.prompt,
        repo=body.repo,
        # A snapshot: later changes to the model don't rewrite this job's history.
        model=model.alias,
        interface=model.interface,
        base_url=model.base_url,
        model_id=model.model_id,
        credential=model.credential,
        context_length=model.context_length,
        max_output_tokens=model.max_output_tokens,
        cost_in_per_mtok=model.cost_in_per_mtok,
        cost_out_per_mtok=model.cost_out_per_mtok,
        job_token_hash=crypto.lookup_hash(job_token),
        github_app=github_app,
    )
    db.add(job)
    db.flush()
    try:
        deps.runner.prepare(job.id, secrets)
    except Exception:
        log.exception("creating the Secret for job %s failed", job.id)
        db.rollback()
        raise HTTPException(503, "couldn't store the job's credentials in the cluster")
    try:
        db.commit()
    except Exception:
        # Don't leave decrypted credentials behind for a job that doesn't exist.
        try:
            deps.runner.cleanup(job.id)
        except Exception:
            log.exception("deleting the Secret for job %s failed", job.id)
        raise
    deps.queue.wake()
    return job_out(job)


# -- models [27] ---------------------------------------------------------------
# Models are per token until O2: a token sees, changes, deletes and uses only the
# models it registered. Another token's models don't exist for it (404). Whoever
# controls a model's endpoint controls the agent, which holds the job's GitHub token,
# so a model is never shared.

MODEL_FIELDS = (
    "interface",
    "base_url",
    "model_id",
    "credential",
    "context_length",
    "max_output_tokens",
    "cost_in_per_mtok",
    "cost_out_per_mtok",
    "notes",
)


def model_fields(m: Model) -> dict:
    return {k: getattr(m, k) for k in MODEL_FIELDS}


def own_model(alias: str, c: Caller, db: Session) -> Model | None:
    return db.scalar(
        select(Model).where(Model.owner_token_id == c.token.id, Model.alias == alias)
    )


def model_out(m: Model) -> ModelOut:
    return ModelOut(
        alias=m.alias,
        interface=m.interface,
        base_url=m.base_url,
        model_id=m.model_id,
        credential=m.credential,
        context_length=m.context_length,
        max_output_tokens=m.max_output_tokens,
        cost_in_per_mtok=m.cost_in_per_mtok,
        cost_out_per_mtok=m.cost_out_per_mtok,
        notes=m.notes,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


@router.put("/models/{alias}")
def put_model(
    alias: Annotated[str, Path(pattern=NAME_RE)], body: ModelIn, c: CallerDep, db: DbDep
) -> ModelOut:
    fields = body.model_dump()
    fields["interface"] = body.interface.value
    m = own_model(alias, c, db)
    if m is None:
        count = db.scalar(
            select(func.count()).select_from(Model).where(Model.owner_token_id == c.token.id)
        )
        if count >= MAX_MODELS_PER_TOKEN:
            raise HTTPException(
                422, f"a token can have at most {MAX_MODELS_PER_TOKEN} models; delete one first"
            )
        db.add(Model(alias=alias, owner_token_id=c.token.id, **fields))
        try:
            db.commit()
        except IntegrityError:
            # A concurrent request by the same token created it first: this becomes
            # an update of that row.
            db.rollback()
            m = own_model(alias, c, db)
            if m is None:
                raise
    if m is not None:
        for k, v in fields.items():
            setattr(m, k, v)
        db.commit()
    m = own_model(alias, c, db)
    db.refresh(m)
    return model_out(m)


@router.get("/models")
def list_models(c: CallerDep, db: DbDep) -> list[ModelOut]:
    """The caller's own models."""
    models = db.scalars(
        select(Model).where(Model.owner_token_id == c.token.id).order_by(Model.alias)
    )
    return [model_out(m) for m in models]


@router.get("/models/{alias}")
def get_model(alias: str, c: CallerDep, db: DbDep) -> ModelOut:
    m = own_model(alias, c, db)
    if m is None:
        raise HTTPException(404, "model not found")
    return model_out(m)


@router.delete("/models/{alias}", status_code=204)
def delete_model(alias: str, c: CallerDep, db: DbDep) -> Response:
    m = own_model(alias, c, db)
    if m is None:
        raise HTTPException(404, "model not found")
    db.delete(m)
    db.commit()
    return Response(status_code=204)


@router.get("/jobs")
def list_jobs(c: CallerDep, db: DbDep) -> list[JobOut]:
    jobs = db.scalars(
        select(Job).where(Job.token_id == c.token.id).order_by(Job.created_at.desc())
    )
    return [job_out(j) for j in jobs]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, c: CallerDep, db: DbDep, deps: DepsDep) -> JobOut:
    job = own_job(job_id, c, db)
    prefix = output_prefix(job.id)
    outputs = [k.removeprefix(prefix) for k in deps.storage.list(prefix)]
    return job_out(job, outputs)


@router.get("/jobs/{job_id}/logs")
def job_logs(
    job_id: str,
    c: CallerDep,
    db: DbDep,
    deps: DepsDep,
    follow: bool = False,
    accept: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Live pod logs while running; the archived log once finished [21].

    Plain text by default; server-sent events if the client accepts
    text/event-stream. With follow=1, a queued job is waited on until it starts.
    """
    job = own_job(job_id, c, db)
    if not follow:
        if job.status == QUEUED:
            raise HTTPException(409, "job is queued; it has no log yet")
        chunks = _log_source(deps, job.id, job.status, follow=False)
    else:
        chunks = _follow(deps, job.id)
    sse = accept is not None and "text/event-stream" in accept
    if sse:
        return StreamingResponse(_sse(chunks, deps, job.id), media_type="text/event-stream")
    return StreamingResponse(chunks, media_type="text/plain; charset=utf-8")


def _log_source(deps: Deps, job_id: str, status: str, follow: bool) -> Iterator[bytes]:
    if status in TERMINAL:
        try:
            return archived_log(deps.storage, job_id)
        except NotFound:
            raise HTTPException(404, "no archived log for this job") from None
    try:
        return deps.runner.logs(job_id, follow=follow)
    except LogsUnavailable as e:
        raise HTTPException(409, str(e)) from None


def _status(deps: Deps, job_id: str) -> str:
    with deps.sessions() as s:
        return s.get(Job, job_id).status


def _follow(deps: Deps, job_id: str, poll: float = 2) -> Iterator[bytes]:
    while (status := _status(deps, job_id)) == QUEUED:
        time.sleep(poll)
    try:
        yield from _log_source(deps, job_id, status, follow=True)
    except HTTPException as e:
        yield f"[drella] {e.detail}\n".encode()


def _sse(chunks: Iterator[bytes], deps: Deps, job_id: str) -> Iterator[bytes]:
    buf = b""
    for chunk in chunks:
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for line in lines:
            yield b"data: " + line + b"\n\n"
    if buf:
        yield b"data: " + buf + b"\n\n"
    # The pod's log ends a moment before the queue loop settles the final status.
    for _ in range(15):
        if (status := _status(deps, job_id)) in TERMINAL:
            break
        time.sleep(2)
    yield f"event: end\ndata: {status}\n\n".encode()


@router.get("/jobs/{job_id}/outputs/{name}")
def get_output(
    job_id: str, name: Annotated[str, Path(pattern=OUTPUT_RE)], c: CallerDep, db: DbDep, deps: DepsDep
) -> StreamingResponse:
    job = own_job(job_id, c, db)
    try:
        chunks = deps.storage.get(output_prefix(job.id) + name)
    except NotFound:
        raise HTTPException(404, "output not found") from None
    return StreamingResponse(
        chunks,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# -- job-scoped endpoints ------------------------------------------------------


@router.post("/jobs/{job_id}/status", status_code=204)
def report_status(
    body: StatusIn, job: Annotated[Job, Depends(job_itself)], db: DbDep
) -> Response:
    # Final status is settled by the queue loop once the pod has exited and its
    # log has been archived.
    job.reported_status = body.status
    job.message = body.message
    db.commit()
    return Response(status_code=204)


@router.put("/jobs/{job_id}/outputs/{name}", status_code=204)
async def put_output(
    name: Annotated[str, Path(pattern=OUTPUT_RE)],
    request: Request,
    job: Annotated[Job, Depends(job_itself)],
    deps: DepsDep,
) -> Response:
    # Spool the body, then hand it to the (synchronous) storage backend.
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as f:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_OUTPUT_BYTES:
                raise HTTPException(413, "output too large")
            f.write(chunk)
        f.seek(0)
        key = output_prefix(job.id) + name
        await run_in_threadpool(deps.storage.put, key, iter(lambda: f.read(CHUNK), b""))
    return Response(status_code=204)
