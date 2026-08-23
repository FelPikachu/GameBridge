import asyncio

import pytest

from gamebridge.database import Database
from gamebridge.install import EpicInstallManager
from gamebridge.jobs import InstallJobStore
from gamebridge.models import JobState


def test_legendary_progress_lines_update_persistent_job(tmp_path):
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    jobs = InstallJobStore(database)
    job = jobs.create("epic", "sample", {"phase": "starting"})

    class Provider:
        pass

    manager = EpicInstallManager(Provider(), jobs)  # type: ignore[arg-type]
    manager._consume_line(job.id, "= Progress: 42.50% (42/100), ETA: 00:01:23")
    manager._consume_line(job.id, " - Downloaded: 512.25 MiB, Written: 600.00 MiB")
    manager._consume_line(job.id, " + Download - 12.75 MiB/s (raw)")
    updated = jobs.get(job.id)
    assert updated.progress == 0.425
    assert updated.payload["eta"] == "00:01:23"
    assert updated.payload["downloadedMiB"] == 512.25
    assert updated.payload["speedMiBs"] == 12.75


@pytest.mark.asyncio
async def test_paused_job_restarts_after_plugin_reload(tmp_path, monkeypatch):
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    jobs = InstallJobStore(database)
    job = jobs.create(
        "epic",
        "hogwarts",
        {"installRoot": str(tmp_path / "games"), "resumeState": "downloading_game"},
    )
    jobs.transition(job.id, JobState.PAUSED)

    class Provider:
        pass

    manager = EpicInstallManager(Provider(), jobs)  # type: ignore[arg-type]
    called = asyncio.Event()

    async def fake_run(
        job_id, app_name, install_root, *, resume_existing=False, operation="install"
    ):
        assert job_id == job.id
        assert app_name == "hogwarts"
        assert install_root == (tmp_path / "games").resolve()
        assert resume_existing is True
        assert operation == "install"
        called.set()

    monkeypatch.setattr(manager, "_run", fake_run)
    manager.resume(job.id)
    await asyncio.wait_for(called.wait(), timeout=1)
    await manager.tasks[job.id]


def test_interrupted_job_becomes_resumable_after_reload(tmp_path):
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    jobs = InstallJobStore(database)
    job = jobs.create("epic", "sample", {"installRoot": str(tmp_path / "games")})
    jobs.transition(job.id, JobState.VALIDATING)

    class Provider:
        pass

    manager = EpicInstallManager(Provider(), jobs)  # type: ignore[arg-type]
    manager.recover_interrupted_jobs()
    recovered = jobs.get(job.id)
    assert recovered.state == JobState.PAUSED
    assert recovered.payload["resumeState"] == "downloading_game"


@pytest.mark.asyncio
async def test_stop_game_cancels_orphaned_paused_record(tmp_path):
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    jobs = InstallJobStore(database)
    job = jobs.create("epic", "sample", {"installRoot": str(tmp_path / "games")})
    jobs.transition(job.id, JobState.PAUSED)

    class Provider:
        @staticmethod
        def executable():
            return None

    manager = EpicInstallManager(Provider(), jobs)  # type: ignore[arg-type]
    await manager.stop_game("sample")
    assert jobs.get(job.id).state == JobState.CANCELLED


class _FakeOutput:
    def __init__(self, lines):
        self._lines = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeProcess:
    pid = 4242

    def __init__(self, lines, return_code=0):
        self.stdout = _FakeOutput(lines)
        self.returncode = return_code

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_isolated_update_simulation_runs_update_and_completes(tmp_path, monkeypatch):
    """Exercise the whole update state machine without touching a real game."""
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    jobs = InstallJobStore(database)
    job = jobs.create(
        "epic",
        "sample",
        {
            "installRoot": str(tmp_path / "games"),
            "operation": "update",
            "phase": "install.preparing",
        },
    )

    class Provider:
        updated = []

        @staticmethod
        def executable():
            return tmp_path / "legendary"

        @staticmethod
        def environment():
            return {}

        @staticmethod
        def is_installed(app_name):
            return app_name == "sample"

        def mark_updated(self, app_name):
            self.updated.append(app_name)

    provider = Provider()
    manager = EpicInstallManager(provider, jobs)  # type: ignore[arg-type]
    invoked = []

    async def fake_stop(*_args):
        return None

    async def fake_subprocess(*args, **_kwargs):
        invoked.append(args)
        return _FakeProcess(
            [
                b"= Progress: 37.50% (3/8), ETA: 00:00:12\n",
                b" - Downloaded: 128.00 MiB, Written: 150.00 MiB\n",
            ]
        )

    monkeypatch.setattr(manager, "_stop_orphaned_install", fake_stop)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)

    await manager._run(job.id, "sample", tmp_path / "games", operation="update")

    assert invoked
    command = invoked[0]
    assert command[1:4] == ("-y", "update", "sample")
    assert "--base-path" not in command
    assert provider.updated == ["sample"]
    completed = jobs.get(job.id)
    assert completed.state == JobState.COMPLETED
    assert completed.payload["downloadedMiB"] == 128.0


@pytest.mark.asyncio
async def test_paused_update_resumes_as_update_after_plugin_reload(tmp_path, monkeypatch):
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    jobs = InstallJobStore(database)
    job = jobs.create(
        "epic",
        "sample",
        {
            "installRoot": str(tmp_path / "games"),
            "resumeState": "downloading_game",
            "operation": "update",
        },
    )
    jobs.transition(job.id, JobState.PAUSED)

    class Provider:
        pass

    manager = EpicInstallManager(Provider(), jobs)  # type: ignore[arg-type]
    called = asyncio.Event()

    async def fake_run(
        job_id, app_name, install_root, *, resume_existing=False, operation="install"
    ):
        assert job_id == job.id
        assert app_name == "sample"
        assert install_root == (tmp_path / "games").resolve()
        assert resume_existing is True
        assert operation == "update"
        called.set()

    monkeypatch.setattr(manager, "_run", fake_run)
    manager.resume(job.id)
    await asyncio.wait_for(called.wait(), timeout=1)
    await manager.tasks[job.id]
