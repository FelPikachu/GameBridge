import pytest

from gamebridge.database import Database
from gamebridge.jobs import InstallJobStore
from gamebridge.models import JobState


@pytest.fixture
def jobs(tmp_path):
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    return InstallJobStore(database)


def test_job_is_persistent_and_advances(jobs):
    job = jobs.create("demo", "sample", {"installPath": "/games/sample"})
    updated = jobs.transition(job.id, JobState.VALIDATING)
    assert updated.state == JobState.VALIDATING
    assert updated.payload["installPath"] == "/games/sample"


def test_job_rejects_skipped_phase(jobs):
    job = jobs.create("demo", "sample", {})
    with pytest.raises(ValueError, match="invalid transition"):
        jobs.transition(job.id, JobState.DOWNLOADING_GAME)


def test_cancelled_job_is_terminal(jobs):
    job = jobs.create("demo", "sample", {})
    jobs.transition(job.id, JobState.CANCELLED)
    with pytest.raises(ValueError, match="terminal"):
        jobs.transition(job.id, JobState.VALIDATING)


def test_latest_job_can_be_restored_after_reentering_details(jobs):
    first = jobs.create("epic", "sample", {})
    second = jobs.create("epic", "sample", {"phase": "latest"})
    restored = jobs.latest_for_game("epic", "sample")
    assert restored is not None
    assert restored.id == second.id
    assert restored.id != first.id
