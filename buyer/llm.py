"""LangChain chat-model factory, rate guard, and retry wrapper for every LLM
call any of the 17 agent surfaces makes.

STRUCTURALLY OFF THE MONEY PATH. Nothing in this module does arithmetic on a
paise value, verifies an Ed25519 signature, checks a mandate limit, calls
Razorpay, or writes to the ledger. It only ever hands back whatever text or
tool call a language model produced. Money moves through core/mandate.py,
core/ledger.py, merchant/quote.py and the merchant Gate — none of which
import this module, and none of which accept an LLM's word for anything.
An LLM misfiring here can at worst pick the wrong product or phrase a bad
negotiation offer; it can never authorize a payment, because nothing
downstream trusts LLM output for that decision.

Two problems this module exists to solve:

1. The Gemini free tier is 15 requests/minute and 1500/day. There are 17
   agent surfaces plus negotiation loops calling this repeatedly during a
   demo; without a shared guard the demo dies mid-run on a 429. `RateGuard`
   enforces both limits before a call is allowed to reach the network.
2. Every surface needs the same retry-with-backoff behaviour on transient
   errors, and every one of those retries still has to go through the guard
   above — a retry storm that skips the limiter is worse than the original
   failure. `LLMGateway.invoke` is the one funnel all of that goes through,
   and it also counts calls per calling surface (`purpose`) for the metrics
   agent.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass

import config

_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

DEFAULT_MAX_ATTEMPTS = config.LLM_MAX_ATTEMPTS
DEFAULT_BACKOFF_BASE_SECONDS = config.LLM_RETRY_BACKOFF_BASE_SECONDS

_VALID_PROVIDERS = ("gemini", "anthropic", "openai")

_SECONDS_PER_MINUTE = 60.0
_SECONDS_PER_DAY = 86400.0


# --- errors ------------------------------------------------------------------------


class LLMConfigError(Exception):
    """Base for mistakes caught at model-construction time.

    These must surface immediately, not three minutes into a demo when an
    agent loop finally makes its first call.
    """


class UnknownProviderError(LLMConfigError):
    def __init__(self, provider: str, valid: tuple[str, ...] = _VALID_PROVIDERS) -> None:
        super().__init__(
            f"unknown LLM provider {provider!r}; valid options are: {', '.join(valid)}"
        )
        self.provider = provider


class MissingAPIKeyError(LLMConfigError):
    def __init__(self, provider: str, env_var: str) -> None:
        super().__init__(f"{provider}: no API key configured; set {env_var}")
        self.provider = provider
        self.env_var = env_var


class DailyBudgetExceededError(Exception):
    """Raised, never blocked on. Sleeping until a daily quota resets is not a
    useful behaviour mid-demo; failing loudly and immediately is."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"daily LLM budget of {limit} requests is exhausted for today; "
            f"refusing to block until the quota resets"
        )
        self.limit = limit


class TransientLLMError(Exception):
    """Marker for a retryable failure. Tests raise this directly; real
    provider errors are recognised heuristically by `_is_transient` instead,
    since Gemini/Anthropic/OpenAI each raise their own exception types."""


class RateGuardStalledError(Exception):
    """Raised when the per-minute wait loop cannot make progress.

    `RateGuard.acquire` sleeps and re-checks the clock in a loop with no
    iteration bound; that is fine as long as `sleep_fn` actually advances
    time. A caller may inject a no-op `sleep_fn` to make the guard
    "non-blocking" for a test or a synchronous shim, and if the minute
    window happens to be full at that moment the loop would otherwise spin
    at 100% CPU forever instead of failing. This error fires the moment a
    sleep call is observed not to advance the clock at all, so the failure
    is loud and immediate instead of a hang.
    """

    def __init__(self) -> None:
        super().__init__(
            "rate guard cannot proceed: sleep_fn did not advance the clock, "
            "so the per-minute window would never free up; refusing to spin"
        )


class RetryBudgetExceededError(Exception):
    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(
            f"gave up after {attempts} attempt(s); last error was "
            f"{type(last_error).__name__}: {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


_TRANSIENT_MARKERS = (
    "429",
    "503",
    "rate limit",
    "ratelimit",
    "resource has been exhausted",
    "resourceexhausted",
    "service unavailable",
    "serviceunavailable",
    "overloaded",
    "timeout",
    "timed out",
)


def _is_transient(exc: BaseException) -> bool:
    """Best-effort classification of a caught exception as retryable.

    `TransientLLMError` is always retryable (that is its only purpose).
    Everything else is matched against known provider phrasing for rate
    limiting and transient service errors, so a genuine 429 from the real
    Gemini/Anthropic/OpenAI SDKs is retried without this module needing to
    import each SDK's private exception hierarchy.
    """
    if isinstance(exc, TransientLLMError):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


# --- rate guard ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RateGuardState:
    """A snapshot for the terminal UI: what the guard has used and when the
    per-minute window next has room."""

    requests_this_minute: int
    requests_today: int
    seconds_until_window_reopens: float


class RateGuard:
    """Enforces both a per-minute and a per-day request budget.

    The per-minute limit blocks: it sleeps until the sliding window has
    room, because a demo can afford to pause a few seconds. The daily limit
    raises instead of blocking: sleeping until tomorrow is never a useful
    thing for a demo to do.

    `time_fn` and `sleep_fn` are injected so tests can drive time forward
    instantly with a fake clock instead of actually sleeping.
    """

    def __init__(
        self,
        *,
        rpm_limit: int,
        daily_limit: int,
        time_fn=time.monotonic,
        sleep_fn=time.sleep,
    ) -> None:
        self.rpm_limit = rpm_limit
        self.daily_limit = daily_limit
        self._time_fn = time_fn
        self._sleep_fn = sleep_fn
        # Timestamps of accepted requests, oldest first. The day window is a
        # superset of the minute window (both are just "requests still young
        # enough to count"), so it is simplest to track them independently.
        self._minute_window: deque[float] = deque()
        self._day_window: deque[float] = deque()

    def _purge(self, now: float) -> None:
        while self._minute_window and now - self._minute_window[0] >= _SECONDS_PER_MINUTE:
            self._minute_window.popleft()
        while self._day_window and now - self._day_window[0] >= _SECONDS_PER_DAY:
            self._day_window.popleft()

    def acquire(self) -> None:
        """Block (if needed) until a request is allowed, then record it.

        Raises `DailyBudgetExceededError` instead of blocking once the daily
        cap is hit — including while this call is waiting out the per-minute
        window, so a demo never sits there for a full minute only to find
        the daily budget was already gone.
        """
        now = self._time_fn()
        self._purge(now)

        while True:
            if len(self._day_window) >= self.daily_limit:
                raise DailyBudgetExceededError(self.daily_limit)

            if len(self._minute_window) < self.rpm_limit:
                break

            wait_seconds = _SECONDS_PER_MINUTE - (now - self._minute_window[0])
            if wait_seconds > 0:
                before_sleep = now
                self._sleep_fn(wait_seconds)
                now = self._time_fn()
                if now <= before_sleep:
                    # sleep_fn returned without moving the clock forward (a
                    # no-op sleep_fn, most plausibly). The window can never
                    # free up under these conditions, so looping again would
                    # spin forever instead of failing.
                    raise RateGuardStalledError()
            else:
                now = self._time_fn()
            self._purge(now)

        self._minute_window.append(now)
        self._day_window.append(now)

    @property
    def state(self) -> RateGuardState:
        now = self._time_fn()
        self._purge(now)
        seconds_until_reopen = 0.0
        if self._minute_window:
            seconds_until_reopen = max(
                0.0, _SECONDS_PER_MINUTE - (now - self._minute_window[0])
            )
        return RateGuardState(
            requests_this_minute=len(self._minute_window),
            requests_today=len(self._day_window),
            seconds_until_window_reopens=seconds_until_reopen,
        )


def default_rate_guard() -> RateGuard:
    """The guard every agent surface shares unless a test injects its own."""
    return RateGuard(
        rpm_limit=config.GEMINI_RPM_LIMIT,
        daily_limit=config.GEMINI_DAILY_LIMIT,
    )


# --- chat model factory ----------------------------------------------------------------


def get_chat_model(**overrides):
    """Build a LangChain chat model wired from `config.LLM_PROVIDER`.

    `overrides` may set `provider`, `model`, or `temperature`; anything else
    is passed straight through to the underlying LangChain constructor.
    Raises before any network call is made: an unknown provider or a missing
    API key fails here, not three calls deep inside an agent loop.
    """
    provider = overrides.pop("provider", config.LLM_PROVIDER)
    if provider not in _VALID_PROVIDERS:
        raise UnknownProviderError(provider)

    model_name = overrides.pop("model", config.MODELS[provider])
    temperature = overrides.pop("temperature", config.TEMPERATURE)

    if provider == "gemini":
        if not config.GEMINI_API_KEY:
            raise MissingAPIKeyError("gemini", "GEMINI_API_KEY")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=config.GEMINI_API_KEY,
            temperature=temperature,
            **overrides,
        )

    if provider == "anthropic":
        api_key = config.ANTHROPIC_API_KEY
        if not api_key:
            raise MissingAPIKeyError("anthropic", _ANTHROPIC_API_KEY_ENV)
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model_name, api_key=api_key, temperature=temperature, **overrides
        )

    # provider == "openai": the only remaining option in _VALID_PROVIDERS.
    api_key = config.OPENAI_API_KEY
    if not api_key:
        raise MissingAPIKeyError("openai", _OPENAI_API_KEY_ENV)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model_name, api_key=api_key, temperature=temperature, **overrides)


# --- the instrumented entry point --------------------------------------------------------


class LLMGateway:
    """The one door every agent surface calls through.

    Wraps a chat model with the rate guard and retry-with-backoff, and counts
    calls per `purpose` (the name of the calling agent surface) so a metrics
    agent can report where the shared Gemini quota went.
    """

    def __init__(
        self,
        model=None,
        *,
        guard: RateGuard | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        sleep_fn=time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
        self._model = model if model is not None else get_chat_model()
        self._guard = guard if guard is not None else default_rate_guard()
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._sleep_fn = sleep_fn
        self._purpose_counts: Counter[str] = Counter()

    def invoke(self, messages, *, purpose: str):
        """Call the underlying model, guarded and retried.

        Every attempt — including retries — goes back through the rate
        guard first: a retry storm that skips the limiter would defeat the
        whole point of having one. A `DailyBudgetExceededError` from the
        guard is never retried; it propagates immediately.
        """
        last_error: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._guard.acquire()
            self._purpose_counts[purpose] += 1
            try:
                return self._model.invoke(messages)
            except DailyBudgetExceededError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                if not _is_transient(exc):
                    raise
                last_error = exc
                if attempt < self._max_attempts:
                    delay = self._backoff_base_seconds * (2 ** (attempt - 1))
                    self._sleep_fn(delay)

        assert last_error is not None  # the loop always sets this before exiting
        raise RetryBudgetExceededError(self._max_attempts, last_error)

    @property
    def purpose_counts(self) -> dict[str, int]:
        return dict(self._purpose_counts)

    @property
    def rate_guard_state(self) -> RateGuardState:
        return self._guard.state


_default_gateway: LLMGateway | None = None


def invoke(messages, *, purpose: str):
    """Module-level convenience: the entry point every agent surface calls.

    Lazily builds a shared `LLMGateway` (and, with it, the shared chat model
    and rate guard) on first use, so importing this module never requires an
    API key — only actually calling it does.
    """
    global _default_gateway
    if _default_gateway is None:
        _default_gateway = LLMGateway()
    return _default_gateway.invoke(messages, purpose=purpose)
