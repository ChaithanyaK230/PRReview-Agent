class PRReviewError(Exception):
    """Base class for all errors raised by the review pipeline."""


class TransientError(PRReviewError):
    """Temporary failure (GitHub 5xx, LLM 429, network timeout). Retried with backoff."""


class FatalError(PRReviewError):
    """Unrecoverable failure. The job is marked FAILED and the Check Run ends neutral."""

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message)
        # Shown on the Check Run, so it must never contain secrets or stack traces
        self.user_message = user_message or "The review could not be completed."


class CancelledJob(PRReviewError):
    """A newer push superseded this job, or the PR was closed."""


class BudgetExceeded(PRReviewError):
    """The job's LLM spend hit its cap. Comments accepted so far are still published."""


class LLMOutputError(PRReviewError):
    """The LLM returned invalid JSON twice. Only this call fails; the caller decides what to do."""

class StateConflict(PRReviewError):
    """The job was not in the expected state; another worker already moved it."""

class ReviewRejected(PRReviewError):
    """GitHub refused a review with 422, usually because an inline comment points at an invalid line."""
