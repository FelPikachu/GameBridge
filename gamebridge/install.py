from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from pathlib import Path

from .jobs import InstallJobStore
from .models import JobState
from .providers.epic import EpicProvider
from .storage import approved_install_path

PROGRESS_PATTERN = re.compile(r"Progress:\s*([0-9.]+)%.*ETA:\s*([0-9:]+)")
DOWNLOAD_PATTERN = re.compile(r"Downloaded:\s*([0-9.]+) MiB")
SPEED_PATTERN = re.compile(r"Download\s*-\s*([0-9.]+) MiB/s")


class EpicInstallManager:
    def __init__(self, provider: EpicProvider, jobs: InstallJobStore) -> None:
        self.provider = provider
        self.jobs = jobs
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self._shutdown_jobs: set[str] = set()

    def recover_interrupted_jobs(self) -> None:
        """Turn orphaned active records into resumable downloads after a reload/crash."""
        for job in self.jobs.active():
            if job.state == JobState.PAUSED:
                continue
            self.jobs.update_progress(
                job.id,
                job.progress,
                {"resumeState": str(JobState.DOWNLOADING_GAME)},
            )
            self.jobs.transition(job.id, JobState.PAUSED)

    def start(
        self,
        external_game_id: str,
        title: str,
        base_path: str | Path,
        *,
        operation: str = "install",
    ) -> str:
        for job_id, task in self.tasks.items():
            if not task.done() and self.jobs.get(job_id).external_game_id == external_game_id:
                return job_id
        install_root = Path(base_path).expanduser().resolve()
        if not approved_install_path(install_root):
            raise ValueError("install.path_unapproved")
        install_root.mkdir(parents=True, exist_ok=True)
        job = self.jobs.create(
            "epic",
            external_game_id,
            {
                "title": title,
                "installRoot": os.fspath(install_root),
                "phase": "install.preparing",
                "downloadedMiB": 0,
                "speedMiBs": 0,
                "eta": "--:--:--",
                "operation": operation,
            },
        )
        self.tasks[job.id] = asyncio.create_task(
            self._run(job.id, external_game_id, install_root, operation=operation)
        )
        return job.id

    async def _run(
        self,
        job_id: str,
        app_name: str,
        install_root: Path,
        *,
        resume_existing: bool = False,
        operation: str = "install",
    ) -> None:
        try:
            executable = self.provider.executable()
            if executable is None:
                raise RuntimeError("legendary.not_installed")
            await self._stop_orphaned_install(executable, app_name)
            if resume_existing:
                self.jobs.transition(job_id, JobState.DOWNLOADING_GAME)
            else:
                self.jobs.transition(job_id, JobState.VALIDATING)
                self.jobs.transition(job_id, JobState.WAITING_FOR_SPACE)
                self.jobs.transition(job_id, JobState.DOWNLOADING_INSTALLER)
                self.jobs.transition(job_id, JobState.VERIFYING_INSTALLER)
                self.jobs.transition(job_id, JobState.PREPARING_PREFIX)
                self.jobs.transition(job_id, JobState.INSTALLING_LAUNCHER)
                self.jobs.transition(job_id, JobState.WAITING_FOR_LOGIN)
                self.jobs.transition(job_id, JobState.DOWNLOADING_GAME)
            environment = {
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                **self.provider.environment(),
            }
            command = "update" if operation == "update" else "install"
            arguments = [
                os.fspath(executable), "-y", command, app_name,
            ]
            if operation != "update":
                arguments.extend(["--base-path", os.fspath(install_root)])
            arguments.extend(["--skip-sdl", "--skip-dlcs"])
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
            self.processes[job_id] = process
            assert process.stdout is not None  # noqa: S101
            async for raw_line in process.stdout:
                self._consume_line(job_id, raw_line.decode("utf-8", errors="replace"))
            return_code = await process.wait()
            if return_code:
                raise RuntimeError(f"Legendary exited with code {return_code}")
            if not self.provider.is_installed(app_name):
                raise RuntimeError(
                    "Legendary exited without registering the installation; "
                    "a previous download process may still be shutting down"
                )
            if operation == "update":
                self.provider.mark_updated(app_name)
            self.jobs.transition(job_id, JobState.VERIFYING_GAME)
            self.jobs.transition(job_id, JobState.CONFIGURING_RUNTIME)
            self.jobs.transition(job_id, JobState.CREATING_SHORTCUT)
            self.jobs.transition(job_id, JobState.COMPLETED)
        except asyncio.CancelledError:
            process = self.processes.get(job_id)
            if process and process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                # A stopped process cannot handle SIGTERM until it is resumed.
                os.killpg(process.pid, signal.SIGCONT)
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            if job_id not in self._shutdown_jobs:
                try:
                    self.jobs.transition(job_id, JobState.CANCELLED)
                except ValueError:
                    pass
            raise
        except Exception as exc:
            self.jobs.fail(job_id, str(exc), retryable=True)
        finally:
            self.processes.pop(job_id, None)
            self.tasks.pop(job_id, None)
            self._shutdown_jobs.discard(job_id)

    async def _stop_orphaned_install(self, executable: Path, app_name: str) -> None:
        own_processes = {process.pid for process in self.processes.values()}
        orphan_groups = await asyncio.to_thread(
            self._find_orphaned_install_groups, executable, app_name, own_processes
        )
        for process_group in orphan_groups:
            try:
                os.killpg(process_group, signal.SIGTERM)
                os.killpg(process_group, signal.SIGCONT)
            except ProcessLookupError:
                continue
        if not orphan_groups:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(not self._process_group_exists(group) for group in orphan_groups):
                return
            await asyncio.sleep(0.1)
        for process_group in orphan_groups:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @staticmethod
    def _find_orphaned_install_groups(
        executable: Path, app_name: str, own_processes: set[int]
    ) -> set[int]:
        process_root = Path("/proc")
        orphan_groups: set[int] = set()
        for entry in process_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid in own_processes:
                continue
            try:
                command = (entry / "cmdline").read_bytes().split(b"\0")
                parent_pid = int((entry / "stat").read_text().split()[3])
                arguments = [item.decode("utf-8", errors="replace") for item in command if item]
            except (OSError, ValueError, IndexError):
                continue
            if parent_pid != 1 or os.fspath(executable) not in arguments:
                continue
            # Updates are downloads too.  After Decky/Steam reloads, a detached
            # Legendary update must be reclaimed just like an install; otherwise
            # a second process can race the original one and leave the UI stuck.
            if not ({"install", "update"} & set(arguments)) or app_name not in arguments:
                continue
            try:
                orphan_groups.add(os.getpgid(pid))
            except ProcessLookupError:
                continue
        return orphan_groups

    @staticmethod
    def _process_group_exists(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _consume_line(self, job_id: str, line: str) -> None:
        job = self.jobs.get(job_id)
        update: dict[str, object] = {}
        progress = job.progress
        if match := PROGRESS_PATTERN.search(line):
            progress = float(match.group(1)) / 100
            update.update({"phase": "install.downloading", "eta": match.group(2)})
        if match := DOWNLOAD_PATTERN.search(line):
            update["downloadedMiB"] = float(match.group(1))
        if match := SPEED_PATTERN.search(line):
            update["speedMiBs"] = float(match.group(1))
        if update:
            self.jobs.update_progress(job_id, progress, update)

    async def cancel(self, job_id: str) -> None:
        task = self.tasks.get(job_id)
        if task is None:
            raise KeyError(f"install job is not running: {job_id}")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def stop_game(self, external_game_id: str) -> None:
        matching = [
            job_id
            for job_id, task in self.tasks.items()
            if not task.done() and self.jobs.get(job_id).external_game_id == external_game_id
        ]
        for job_id in matching:
            await self.cancel(job_id)
        remaining = [
            job for job in self.jobs.active() if job.external_game_id == external_game_id
        ]
        if remaining:
            executable = self.provider.executable()
            if executable is not None:
                await self._stop_orphaned_install(executable, external_game_id)
            for job in remaining:
                try:
                    self.jobs.transition(job.id, JobState.CANCELLED)
                except ValueError:
                    pass

    def pause(self, job_id: str) -> None:
        process = self.processes.get(job_id)
        if process is None or process.returncode is not None:
            raise KeyError(f"install job is not running: {job_id}")
        job = self.jobs.get(job_id)
        if job.state == JobState.PAUSED:
            return
        self.jobs.update_progress(job_id, job.progress, {"resumeState": str(job.state)})
        os.killpg(process.pid, signal.SIGSTOP)
        self.jobs.transition(job_id, JobState.PAUSED)

    def resume(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job.state != JobState.PAUSED:
            return
        process = self.processes.get(job_id)
        if process is None or process.returncode is not None:
            install_root = job.payload.get("installRoot")
            if not isinstance(install_root, str) or not install_root:
                raise ValueError("install.paused_path_missing")
            self.tasks[job_id] = asyncio.create_task(
                self._run(
                    job_id,
                    job.external_game_id,
                    Path(install_root).expanduser().resolve(),
                    resume_existing=True,
                    operation=str(job.payload.get("operation", "install")),
                )
            )
            return
        raw_resume_state = job.payload.get("resumeState", JobState.DOWNLOADING_GAME)
        try:
            resume_state = JobState(str(raw_resume_state))
        except ValueError:
            resume_state = JobState.DOWNLOADING_GAME
        os.killpg(process.pid, signal.SIGCONT)
        self.jobs.transition(job_id, resume_state)

    async def shutdown(self) -> None:
        for job_id in list(self.tasks):
            job = self.jobs.get(job_id)
            if job.state != JobState.PAUSED:
                self.jobs.update_progress(
                    job_id,
                    job.progress,
                    {"resumeState": str(JobState.DOWNLOADING_GAME)},
                )
                self.jobs.transition(job_id, JobState.PAUSED)
            self._shutdown_jobs.add(job_id)
            task = self.tasks.get(job_id)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
