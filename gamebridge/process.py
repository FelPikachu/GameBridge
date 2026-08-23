from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ProcessError(RuntimeError):
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        super().__init__(message[:500])


class SafeProcessRunner:
    """Runs argument arrays without a shell and with bounded output/time."""

    def __init__(self, timeout: float = 30, output_limit: int = 4 * 1024 * 1024) -> None:
        self.timeout = timeout
        self.output_limit = output_limit

    async def run(
        self,
        executable: str | Path,
        *arguments: str,
        environment: dict[str, str] | None = None,
        check: bool = True,
        sensitive_arguments: frozenset[int] = frozenset(),
    ) -> ProcessResult:
        executable_path = Path(executable).expanduser().resolve()  # noqa: ASYNC240
        if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
            raise FileNotFoundError(executable_path)
        argv = (os.fspath(executable_path), *arguments)
        child_environment = {
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            **(environment or {}),
        }
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_environment,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError(f"command timed out after {self.timeout:g}s") from None
        if len(stdout_bytes) > self.output_limit or len(stderr_bytes) > self.output_limit:
            raise RuntimeError("command output exceeded safety limit")
        sensitive_values = tuple(
            arguments[index]
            for index in sensitive_arguments
            if 0 <= index < len(arguments) and arguments[index]
        )
        public_argv = (
            argv[0],
            *(
                "<redacted>" if index in sensitive_arguments else value
                for index, value in enumerate(arguments)
            ),
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        for secret in sensitive_values:
            stdout = stdout.replace(secret, "<redacted>")
            stderr = stderr.replace(secret, "<redacted>")
        result = ProcessResult(
            public_argv,
            process.returncode or 0,
            stdout,
            stderr,
        )
        if check and result.returncode:
            raise ProcessError(result)
        return result
