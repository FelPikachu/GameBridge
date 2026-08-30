import sys

import pytest

from gamebridge.process import ProcessError, SafeProcessRunner


@pytest.mark.asyncio
async def test_process_runner_preserves_argument_boundaries():
    runner = SafeProcessRunner()
    result = await runner.run(
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1])",
        "value with spaces;$(ignored)",
    )
    assert result.stdout.strip() == "value with spaces;$(ignored)"


@pytest.mark.asyncio
async def test_process_runner_reports_failure():
    runner = SafeProcessRunner()
    with pytest.raises(ProcessError):
        await runner.run(sys.executable, "-c", "raise SystemExit(7)")


@pytest.mark.asyncio
async def test_process_runner_redacts_sensitive_arguments_and_output():
    runner = SafeProcessRunner()
    sensitive_value = "authorization-" + "code-must-not-leak"
    result = await runner.run(
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1])",
        sensitive_value,
        sensitive_arguments=frozenset({2}),
    )
    assert sensitive_value not in repr(result)
    assert result.argv[-1] == "<redacted>"
    assert result.stdout.strip() == "<redacted>"


@pytest.mark.asyncio
async def test_process_runner_allows_a_larger_limit_for_one_bounded_command():
    runner = SafeProcessRunner(output_limit=4)
    with pytest.raises(RuntimeError, match="command output exceeded safety limit"):
        await runner.run(sys.executable, "-c", "print('12345')")

    result = await runner.run(
        sys.executable,
        "-c",
        "print('12345')",
        output_limit=16,
    )
    assert result.stdout.strip() == "12345"
