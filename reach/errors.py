"""Typed failures. Every one of these is a decision REACH made on purpose."""


class ReachError(Exception):
    """Base class. ``kind`` is what gets recorded on a job or audit event."""

    kind = "REACH_ERROR"
    retryable = False


class PermissionDenied(ReachError):
    kind = "PERMISSION_DENIED"


class PolicyViolation(ReachError):
    """A provider policy or source-usage policy forbids this operation."""

    kind = "POLICY_VIOLATION"


class FirewallViolation(PolicyViolation):
    """Restricted provider data was about to reach a model or vector index."""

    kind = "FIREWALL_VIOLATION"


class FetchBlocked(ReachError):
    """The safe fetcher refused a URL: SSRF, robots, size, MIME or redirect."""

    kind = "FETCH_BLOCKED"

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


class ProviderNotConnected(ReachError):
    kind = "PROVIDER_NOT_CONNECTED"


class ProviderRateLimited(ReachError):
    kind = "PROVIDER_RATE_LIMITED"
    retryable = True

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class QuotaExceeded(ReachError):
    kind = "QUOTA_EXCEEDED"


class BudgetExceeded(ReachError):
    kind = "BUDGET_EXCEEDED"


class ApprovalInvalid(ReachError):
    """The approved payload no longer matches the payload being executed."""

    kind = "APPROVAL_INVALID"


class SendBlocked(ReachError):
    kind = "SEND_BLOCKED"


class ValidationError(ReachError):
    kind = "VALIDATION_ERROR"


class KillSwitchEngaged(ReachError):
    kind = "KILL_SWITCH"
