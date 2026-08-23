from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .database import Database
from .models import JobState

_LINEAR = (
    JobState.CREATED,
    JobState.VALIDATING,
    JobState.WAITING_FOR_SPACE,
    JobState.DOWNLOADING_INSTALLER,
    JobState.VERIFYING_INSTALLER,
    JobState.PREPARING_PREFIX,
    JobState.INSTALLING_LAUNCHER,
    JobState.WAITING_FOR_LOGIN,
    JobState.DOWNLOADING_GAME,
    JobState.VERIFYING_GAME,
    JobState.CONFIGURING_RUNTIME,
    JobState.CREATING_SHORTCUT,
    JobState.COMPLETED,
)
_NEXT = dict(zip(_LINEAR, _LINEAR[1:], strict=False))
_TERMINAL = {
    JobState.COMPLETED,
    JobState.CANCELLED,
    JobState.FAILED_PERMANENT,
    JobState.BLOCKED_BY_COMPATIBILITY,
}


@dataclass(frozen=True, slots=True)
class InstallJob:
    id: str
    provider_id: str
    external_game_id: str
    state: JobState
    progress: float
    payload: dict[str, Any]


class InstallJobStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self, provider_id: str, external_game_id: str, payload: dict[str, Any]
    ) -> InstallJob:
        job = InstallJob(str(uuid4()), provider_id, external_game_id, JobState.CREATED, 0, payload)
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO install_jobs(id, provider_id, external_game_id, state, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (job.id, provider_id, external_game_id, job.state, Database.encode(payload)),
            )
            db.execute(
                "INSERT INTO install_job_events(job_id, to_state) VALUES (?, ?)",
                (job.id, job.state),
            )
        return job

    def get(self, job_id: str) -> InstallJob:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM install_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown install job: {job_id}")
        return InstallJob(
            row["id"], row["provider_id"], row["external_game_id"],
            JobState(row["state"]), row["progress"], Database.decode(row["payload_json"]),
        )

    def transition(
        self, job_id: str, target: JobState, detail: dict[str, Any] | None = None
    ) -> InstallJob:
        job = self.get(job_id)
        if job.state in _TERMINAL:
            raise ValueError(f"terminal job cannot transition: {job.state}")
        valid = target in {JobState.PAUSED, JobState.CANCELLED, JobState.FAILED_RETRYABLE,
                           JobState.FAILED_PERMANENT, JobState.BLOCKED_BY_COMPATIBILITY}
        valid = valid or _NEXT.get(job.state) == target
        if job.state in {JobState.PAUSED, JobState.FAILED_RETRYABLE}:
            valid = target not in {JobState.CREATED, JobState.COMPLETED}
        if not valid:
            raise ValueError(f"invalid transition: {job.state} -> {target}")
        progress = 1.0 if target == JobState.COMPLETED else job.progress
        with self.database.connect() as db:
            db.execute(
                "UPDATE install_jobs SET state=?, progress=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (target, progress, job_id),
            )
            db.execute(
                "INSERT INTO install_job_events"
                "(job_id, from_state, to_state, detail_json) VALUES(?,?,?,?)",
                (job_id, job.state, target, Database.encode(detail or {})),
            )
        return self.get(job_id)

    def active(self) -> list[InstallJob]:
        terminal = tuple(str(item) for item in _TERMINAL)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id FROM install_jobs WHERE state NOT IN (?,?,?,?) ORDER BY created_at",
                terminal,
            ).fetchall()
        return [self.get(row["id"]) for row in rows]

    def update_progress(
        self, job_id: str, progress: float, payload_update: dict[str, Any] | None = None
    ) -> InstallJob:
        job = self.get(job_id)
        payload = {**job.payload, **(payload_update or {})}
        progress = max(0.0, min(1.0, float(progress)))
        with self.database.connect() as db:
            db.execute(
                "UPDATE install_jobs SET progress=?, payload_json=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (progress, Database.encode(payload), job_id),
            )
        return self.get(job_id)

    def fail(self, job_id: str, message: str, retryable: bool = True) -> InstallJob:
        target = JobState.FAILED_RETRYABLE if retryable else JobState.FAILED_PERMANENT
        job = self.transition(job_id, target, {"message": message[:500]})
        with self.database.connect() as db:
            db.execute(
                "UPDATE install_jobs SET error_message=? WHERE id=?", (message[:500], job_id)
            )
        return job

    def latest_for_game(self, provider_id: str, external_game_id: str) -> InstallJob | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id FROM install_jobs WHERE provider_id=? AND external_game_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (provider_id, external_game_id),
            ).fetchone()
        return self.get(row["id"]) if row else None
