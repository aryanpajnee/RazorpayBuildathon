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

_NVIDIA_API_KEY_ENV = "NVIDIA_API_KEY"
_GROQ_API_KEY_ENV = "GROQ_API_KEY"
_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"

DEFAULT_MAX_ATTEMPTS = config.LLM_MAX_ATTEMPTS
DEFAULT_BACKOFF_BASE_SECONDS = config.LLM_RETRY_BACKOFF_BASE_SECONDS

_VALID_PROVIDERS = ("gemini", "nvidia", "groq", "anthropic", "openai")

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


def _provider_limits(provider: str) -> tuple[int, int]:
    """(rpm_limit, daily_limit) for a provider's free tier. A provider guards
    against ITS OWN limits, so whichever one leads (LLM_PROVIDER) or catches
    (FALLBACK_LLM_PROVIDER) is throttled correctly after a swap. Providers with
    no published free-tier number fall back to Gemini's conservative limits."""
    limits = {
        "gemini": (config.GEMINI_RPM_LIMIT, config.GEMINI_DAILY_LIMIT),
        "groq": (config.GROQ_RPM_LIMIT, config.GROQ_DAILY_LIMIT),
    }
    return limits.get(provider, (config.GEMINI_RPM_LIMIT, config.GEMINI_DAILY_LIMIT))


def _fallback_api_key(provider: str) -> str:
    """The key the fallback lane uses for a given provider. Groq's fallback is
    the DEDICATED second key (GROQ_API_KEY_FALLBACK), never the one the intent
    step uses, so the front and fallback lanes never share a key; every other
    provider uses its normal configured key."""
    if provider == "groq":
        return config.GROQ_API_KEY_FALLBACK
    return {
        "gemini": config.GEMINI_API_KEY,
        "nvidia": config.NVIDIA_API_KEY,
        "anthropic": config.ANTHROPIC_API_KEY,
        "openai": config.OPENAI_API_KEY,
    }.get(provider, "")


def default_rate_guard() -> RateGuard:
    """The guard every agent surface shares unless a test injects its own —
    tuned to the FRONT provider's own limits (config.LLM_PROVIDER)."""
    rpm, daily = _provider_limits(config.LLM_PROVIDER)
    return RateGuard(rpm_limit=rpm, daily_limit=daily)


def default_fallback_guard() -> RateGuard:
    """The guard the fallback lane uses — SEPARATE from the primary guard on
    purpose. When we fail over because the front provider's daily quota is spent,
    the primary guard's day window is already full; routing the fallback's calls
    through it too would re-raise `DailyBudgetExceededError` on the very first
    fallback call. The fallback provider's quota is independent, so its lane gets
    its own budget, tuned to that provider's own limits."""
    rpm, daily = _provider_limits(config.FALLBACK_LLM_PROVIDER)
    return RateGuard(rpm_limit=rpm, daily_limit=daily)


def fallback_target() -> tuple[str, str] | None:
    """(provider, api_key) for the emergency fallback lane, or None when the
    fallback provider is unknown or has no key configured — in which case the
    gateway behaves exactly as it did before the lane existed. Read at call time
    so a test (or a mid-session provider swap) is seen without rebuilding the
    gateway."""
    provider = config.FALLBACK_LLM_PROVIDER
    if not provider or provider not in _VALID_PROVIDERS:
        return None
    key = _fallback_api_key(provider)
    if not key:
        return None
    return provider, key


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
    # An explicit `api_key` override lets the gateway build a model on the
    # FALLBACK credentials (same provider, a different key) without a second
    # config provider entry. Absent, each branch uses its configured key as
    # before, so nothing about the default path changes.
    api_key_override = overrides.pop("api_key", None)

    if provider == "gemini":
        key = api_key_override or config.GEMINI_API_KEY
        if not key:
            raise MissingAPIKeyError("gemini", "GEMINI_API_KEY")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=key,
            temperature=temperature,
            **overrides,
        )

    if provider == "nvidia":
        # NVIDIA's NIM endpoint speaks the OpenAI wire protocol, so the OpenAI
        # client reaches it with a custom base_url. This is the fast lane for
        # prose-only surfaces (see config.FAST_LLM_SURFACES); nothing numeric is
        # ever routed here, so the 8B's paise-scaling weakness cannot bite.
        key = api_key_override or config.NVIDIA_API_KEY
        if not key:
            raise MissingAPIKeyError("nvidia", _NVIDIA_API_KEY_ENV)
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_name,
            api_key=key,
            base_url=config.NVIDIA_BASE_URL,
            temperature=temperature,
            **overrides,
        )

    if provider == "groq":
        # GroqCloud speaks the OpenAI wire protocol, so the OpenAI client reaches
        # it with Groq's base_url. Two callers: the prompt-understanding step
        # (demo/intent.py, on GROQ_API_KEY), and the emergency fallback lane
        # (config.FALLBACK_LLM_*), which passes the second key as `api_key`.
        key = api_key_override or config.GROQ_API_KEY
        if not key:
            raise MissingAPIKeyError("groq", _GROQ_API_KEY_ENV)
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_name,
            api_key=key,
            base_url=config.GROQ_BASE_URL,
            temperature=temperature,
            **overrides,
        )

    if provider == "anthropic":
        api_key = api_key_override or config.ANTHROPIC_API_KEY
        if not api_key:
            raise MissingAPIKeyError("anthropic", _ANTHROPIC_API_KEY_ENV)
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model_name, api_key=api_key, temperature=temperature, **overrides
        )

    # provider == "openai": the only remaining option in _VALID_PROVIDERS.
    api_key = api_key_override or config.OPENAI_API_KEY
    if not api_key:
        raise MissingAPIKeyError("openai", _OPENAI_API_KEY_ENV)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model_name, api_key=api_key, temperature=temperature, **overrides)


# --- provider routing ---------------------------------------------------------------------


def provider_for_purpose(purpose: str) -> str:
    """Which provider a given agent surface routes to.

    Fast, prose-only surfaces (`config.FAST_LLM_SURFACES`) take the NVIDIA fast
    lane; everything else — anything numeric or judgment-critical — stays on the
    default provider (`config.LLM_PROVIDER`, Gemini 3.6 Flash). The split is a
    deliberate safety property, not just a speed tweak: the 8B mis-scales money
    (it returned 5000 paise where 500000 was required in the 26 Aug bench), so
    it only ever sees surfaces that emit no number anyone relies on.

    If the fast lane is selected but no NVIDIA key is configured, fall back to
    the default provider rather than failing a surface that has a good default.
    """
    if purpose in config.FAST_LLM_SURFACES:
        if config.FAST_LLM_PROVIDER == "nvidia" and not config.NVIDIA_API_KEY:
            return config.LLM_PROVIDER
        return config.FAST_LLM_PROVIDER
    return config.LLM_PROVIDER


# --- the instrumented entry point --------------------------------------------------------


class LLMGateway:
    """The one door every agent surface calls through.

    Wraps a chat model with the rate guard and retry-with-backoff, and counts
    calls per `purpose` (the name of the calling agent surface) so a metrics
    agent can report where the shared quota went.

    Model selection: if an explicit `model` is injected, it is used for every
    call (tests and single-provider callers rely on this). Otherwise the
    gateway routes per `purpose` via `provider_for_purpose`, lazily building and
    caching one chat model per provider — so a prose surface hits the NVIDIA
    fast lane and a numeric surface hits Gemini, through the same one door.
    """

    def __init__(
        self,
        model=None,
        *,
        guard: RateGuard | None = None,
        fallback_guard: RateGuard | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        sleep_fn=time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")
        # None means "route per purpose, lazily". An explicit model overrides
        # routing entirely and is used for every call.
        self._explicit_model = model
        self._models_by_provider: dict[str, object] = {}
        self._guard = guard if guard is not None else default_rate_guard()
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._sleep_fn = sleep_fn
        self._purpose_counts: Counter[str] = Counter()
        # The emergency fallback lane (config.FALLBACK_LLM_*): built lazily the
        # first time a failover is actually needed, so a gateway that never
        # exhausts its primary never touches the second key.
        self._fallback_guard = fallback_guard
        self._fallback_model = None

    def _provider_model(self, provider: str):
        """Lazily build and cache the chat model for one explicit provider."""
        model = self._models_by_provider.get(provider)
        if model is None:
            model = get_chat_model(provider=provider)
            self._models_by_provider[provider] = model
        return model

    def _model_for(self, purpose: str):
        """The chat model this purpose should use. An injected model wins; else
        route to a provider and lazily build+cache that provider's model."""
        if self._explicit_model is not None:
            return self._explicit_model
        return self._provider_model(provider_for_purpose(purpose))

    def invoke(self, messages, *, purpose: str):
        """Call the underlying model, guarded and retried, with an emergency
        cross-key failover.

        The primary path runs the guarded retry loop against the routed (or
        injected) model. If that path signals the provider is RATE-LIMITED or
        out of daily quota — `DailyBudgetExceededError` from the guard, or
        `RetryBudgetExceededError` after transient (429/503) retries are spent —
        and a fallback key is configured, the whole call is re-run ONCE on the
        fallback lane (config.FALLBACK_LLM_*, its own key and its own guard).
        A non-transient error (a bug, an auth failure, a dead model) is never a
        reason to fail over: it propagates so it can be seen and fixed.

        An injected explicit model (tests, single-provider callers) has no
        routing and no fallback lane — there is no second key to swap in for one
        specific model object — so it runs the primary path alone.
        """
        if self._explicit_model is not None:
            return self._run_attempts(
                messages, self._explicit_model, None, self._guard, purpose,
                allow_degrade=False,
            )

        provider = provider_for_purpose(purpose)
        model = self._provider_model(provider)
        try:
            return self._run_attempts(
                messages, model, provider, self._guard, purpose,
                allow_degrade=True,
            )
        except (DailyBudgetExceededError, RetryBudgetExceededError):
            fallback = self._fallback_lane()
            if fallback is None:
                raise  # no fallback configured: original exhaustion stands
            fb_model, fb_provider, fb_guard = fallback
            # One shot on the fallback lane. It has nowhere further to degrade,
            # so its own exhaustion (or any error) propagates unwrapped.
            return self._run_attempts(
                messages, fb_model, fb_provider, fb_guard, purpose,
                allow_degrade=False,
            )

    def _run_attempts(self, messages, model, provider, guard, purpose, *, allow_degrade):
        """The guarded retry loop against ONE model/guard pair.

        Returns the model's result, or raises: `DailyBudgetExceededError` (quota
        gone — from the guard, never retried), `RetryBudgetExceededError`
        (transient retries exhausted), or a non-transient error unchanged.

        Every attempt — including retries — goes back through the rate guard
        first: a retry storm that skips the limiter would defeat the whole point
        of having one.

        `allow_degrade` enables the ONE-TIME fast-lane -> default-provider
        degrade on a non-transient failure (see below); it is off on the
        fallback lane, which is already the last resort.
        """
        degraded = False
        last_error: BaseException | None = None
        for attempt in range(1, self._max_attempts + 1):
            guard.acquire()
            self._purpose_counts[purpose] += 1
            try:
                return model.invoke(messages)
            except DailyBudgetExceededError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                if _is_transient(exc):
                    last_error = exc
                    if attempt < self._max_attempts:
                        delay = self._backoff_base_seconds * (2 ** (attempt - 1))
                        self._sleep_fn(delay)
                    continue
                # Non-transient. A FAST-LANE (non-default) provider that fails
                # outright — e.g. NVIDIA retiring its model with an HTTP 410, as
                # happened live on 2026-08-26 — degrades ONCE to the default
                # provider so a prose surface stays alive instead of hard-failing
                # (or, at an endpoint, silently returning nothing). The default
                # provider has nowhere to fall back to, so its own non-transient
                # errors still propagate. Only prose surfaces route to the fast
                # lane, so degrading to Gemini here never puts a numeric task on
                # a model that shouldn't do arithmetic — the routing already
                # guaranteed that upstream.
                if (
                    allow_degrade
                    and not degraded
                    and provider is not None
                    and provider != config.LLM_PROVIDER
                ):
                    degraded = True
                    last_error = exc
                    model = self._provider_model(config.LLM_PROVIDER)
                    continue
                raise

        assert last_error is not None  # the loop always sets this before exiting
        raise RetryBudgetExceededError(self._max_attempts, last_error)

    def _fallback_lane(self):
        """(model, provider, guard) for the fallback lane, or None when no
        fallback key is configured. Model and guard are built once and cached,
        so repeated failovers in a session reuse the same client and budget."""
        target = fallback_target()
        if target is None:
            return None
        provider, key = target
        if self._fallback_model is None:
            self._fallback_model = get_chat_model(provider=provider, api_key=key)
        if self._fallback_guard is None:
            self._fallback_guard = default_fallback_guard()
        return self._fallback_model, provider, self._fallback_guard

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
