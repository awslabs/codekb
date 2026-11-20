"""
Exception classes for the space refresh pipeline.

These exceptions are used by the RefreshOrchestrator and its collaborators
to signal pipeline failures with structured step/reason metadata.
"""


class RefreshError(Exception):
    """Base exception for refresh pipeline errors.

    Attributes:
        step: The pipeline step where the error occurred.
        reason: A machine-readable reason code for the failure.
        detail: A human-readable description of the failure.
    """

    def __init__(self, step: str, reason: str, detail: str = "") -> None:
        self.step = step
        self.reason = reason
        self.detail = detail
        msg = f"[refresh] {step}: {reason}"
        if detail:
            msg += f" — {detail}"
        super().__init__(msg)


class RefreshFailed(RefreshError):
    """Raised for unrecoverable pipeline failures.

    When raised, the orchestrator writes FAILED status and aborts the pipeline.
    The canonical knowledge base and vector index remain untouched.
    """


class GitCommandError(RuntimeError):
    """Raised when a git subprocess exits with a non-zero status.

    Attributes:
        command: The git command that failed (argv list as a string).
        exit_code: The process exit code.
        stderr: Captured stderr output.
    """

    def __init__(self, command: str, exit_code: int, stderr: str = "") -> None:
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr
        msg = f"git command failed (exit {exit_code}): {command}"
        if stderr:
            msg += f"\nstderr: {stderr}"
        super().__init__(msg)
