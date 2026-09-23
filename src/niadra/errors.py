"""Exceptions raised by the SDK.

With the default `strict=False`, public client methods never raise: they log and return a
safe value. These exceptions surface only with `strict=True`, or from code you call
directly, such as `ApiKey.parse()` or the handle helpers.
"""

from __future__ import annotations

from niadra.models.common import Problem


class NiadraError(Exception):
    """Base class of every exception the SDK raises."""


class ConfigurationError(NiadraError):
    """The client cannot be built as configured: a missing or malformed key, a bad base URL."""


class APIConnectionError(NiadraError):
    """The request never got an HTTP answer: DNS, TCP, TLS, or a dropped connection."""


class APITimeoutError(APIConnectionError):
    """The method's own time budget ran out before an answer arrived."""


class APIError(NiadraError):
    """The API answered with an error status. `problem` holds the RFC 9457 body when there is one."""

    status_code: int

    def __init__(self, status_code: int, problem: Problem | None = None) -> None:
        self.status_code = status_code
        self.problem = problem
        code = problem.code if problem else "unknown"
        super().__init__(f"HTTP {status_code} ({code})")

    @property
    def code(self) -> str:
        return self.problem.code if self.problem else "unknown"

    @property
    def request_id(self) -> str | None:
        return self.problem.request_id if self.problem else None


class BadRequestError(APIError):
    pass


class AuthenticationError(APIError):
    """401: the key is unknown, revoked or malformed."""


class PermissionDeniedError(APIError):
    """403: the key is valid but lacks the scope, or the source's access was cut."""


class NotFoundError(APIError):
    pass


class ConflictError(APIError):
    """409: typically the same `Idempotency-Key` sent with a different body."""


class UnprocessableEntityError(APIError):
    pass


class WrongCellError(APIError):
    """421: the space is moving between cells. The SDK retries on a fresh connection."""


class RateLimitError(APIError):
    def __init__(
        self, status_code: int, problem: Problem | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(status_code, problem)
        self.retry_after = retry_after


class ServerError(APIError):
    pass


_BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    421: WrongCellError,
    422: UnprocessableEntityError,
}


def error_for_status(status_code: int, problem: Problem | None, retry_after: float | None = None) -> APIError:
    if status_code == 429:
        return RateLimitError(status_code, problem, retry_after)
    if status_code >= 500:
        return ServerError(status_code, problem)
    return _BY_STATUS.get(status_code, APIError)(status_code, problem)
