"""buyer/llm.py is the LLM layer, not the money path, so these tests check
scaffolding behaviour: model construction, the rate guard, and retries.

Everything here is offline and deterministic. The rate guard is driven by a
fake clock that only moves when the code under test calls sleep — no test in
this file sleeps for real, so the whole suite runs in well under a second.
"""

from __future__ import annotations

import langchain_google_genai
import langchain_openai
import pytest

import config
from buyer.llm import (
    DailyBudgetExceededError,
    LLMGateway,
    MissingAPIKeyError,
    RateGuard,
    RateGuardStalledError,
    RetryBudgetExceededError,
    TransientLLMError,
    UnknownProviderError,
    default_rate_guard,
    get_chat_model,
    provider_for_purpose,
)


class FakeClock:
    """A monotonic clock that only advances when told to.

    `sleep` is handed to the code under test as its sleep function, so every
    "wait" in a test is an instant bookkeeping step, not a real pause.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_guard(rpm_limit=15, daily_limit=1500, clock=None):
    clock = clock or FakeClock()
    guard = RateGuard(
        rpm_limit=rpm_limit,
        daily_limit=daily_limit,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    return guard, clock


# --- RateGuard: per-minute window --------------------------------------------------


def test_first_n_requests_within_a_minute_do_not_wait():
    guard, clock = make_guard(rpm_limit=15, daily_limit=1500)
    for _ in range(15):
        guard.acquire()
    assert clock.slept == []
    assert guard.state.requests_this_minute == 15


def test_sixteenth_request_in_the_same_minute_waits_for_the_window_to_slide():
    guard, clock = make_guard(rpm_limit=15, daily_limit=1500)
    for _ in range(15):
        guard.acquire()
    assert clock.now == 0.0

    guard.acquire()  # the 16th request must wait for the oldest one to age out

    assert clock.slept, "the 16th request in a minute must sleep, not proceed immediately"
    assert clock.now >= 60.0
    # after the wait, exactly the 16th request remains inside the fresh window
    assert guard.state.requests_this_minute == 1


def test_window_sliding_forward_by_exactly_sixty_seconds_releases_the_next_request():
    guard, clock = make_guard(rpm_limit=1, daily_limit=1500)
    guard.acquire()
    assert clock.now == 0.0

    guard.acquire()

    assert clock.slept == [60.0]
    assert clock.now == 60.0


def test_requests_spread_out_never_need_to_wait():
    guard, clock = make_guard(rpm_limit=2, daily_limit=1500)
    guard.acquire()
    clock.now += 30
    guard.acquire()
    clock.now += 30
    guard.acquire()  # the first request is now > 60s old, so this fits without waiting

    assert clock.slept == []


# --- RateGuard: daily budget --------------------------------------------------------


def test_daily_budget_exhausted_raises_instead_of_blocking():
    guard, clock = make_guard(rpm_limit=1000, daily_limit=2)
    guard.acquire()
    guard.acquire()

    with pytest.raises(DailyBudgetExceededError):
        guard.acquire()

    # a demo dying mid-run is bad; a demo hanging until tomorrow is worse
    assert clock.slept == []


def test_daily_budget_is_checked_even_while_waiting_out_the_minute_window():
    guard, clock = make_guard(rpm_limit=1, daily_limit=1)
    guard.acquire()

    with pytest.raises(DailyBudgetExceededError):
        guard.acquire()


# --- RateGuard: observable state -----------------------------------------------------


def test_state_reports_requests_used_this_minute_and_today():
    guard, clock = make_guard(rpm_limit=15, daily_limit=1500)
    guard.acquire()
    guard.acquire()

    state = guard.state
    assert state.requests_this_minute == 2
    assert state.requests_today == 2


def test_state_reports_seconds_until_the_window_reopens():
    guard, clock = make_guard(rpm_limit=1, daily_limit=1500)
    guard.acquire()

    state = guard.state
    assert state.seconds_until_window_reopens == pytest.approx(60.0)

    clock.now += 45
    state = guard.state
    assert state.seconds_until_window_reopens == pytest.approx(15.0)


def test_acquire_raises_instead_of_spinning_forever_when_sleep_does_not_advance_the_clock():
    """A caller may inject a no-op sleep_fn to make the guard "non-blocking".
    If the minute window is full, the wait loop would then spin at 100% CPU
    forever: sleep does nothing, the clock never advances, the window never
    empties. The guard must detect that time did not move across a sleep and
    fail loudly instead of hanging.

    rpm_limit=1 with a clock that never advances keeps this bounded and fast
    even if the fix regresses — the guard must give up well before any test
    timeout, not spin until something external kills it."""

    class FrozenClock:
        """A clock whose sleep never advances time.

        `_MAX_SLEEP_CALLS` is a safety net for this test, not part of the
        behaviour under test: if `RateGuard.acquire` regresses back to an
        unbounded spin, this raises after a small, fast-to-reach cap instead
        of the test hanging the whole suite.
        """

        _MAX_SLEEP_CALLS = 1000

        def __init__(self) -> None:
            self.now = 0.0
            self.slept: list[float] = []

        def time(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            # deliberately does NOT advance self.now
            self.slept.append(seconds)
            if len(self.slept) > self._MAX_SLEEP_CALLS:
                raise RuntimeError(
                    "sleep_fn called far more than once for a single acquire() "
                    "call; RateGuard.acquire is spinning instead of failing"
                )

    clock = FrozenClock()
    guard = RateGuard(
        rpm_limit=1,
        daily_limit=1500,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )
    guard.acquire()  # fills the one-request minute window

    with pytest.raises(RateGuardStalledError):
        guard.acquire()  # window never frees up because the clock is frozen


def test_default_rate_guard_is_wired_from_config():
    guard = default_rate_guard()
    assert guard.rpm_limit == config.GEMINI_RPM_LIMIT
    assert guard.daily_limit == config.GEMINI_DAILY_LIMIT


# --- get_chat_model: provider validation ---------------------------------------------


def test_unknown_provider_raises_a_typed_error_naming_the_valid_options():
    with pytest.raises(UnknownProviderError) as exc_info:
        get_chat_model(provider="watson")

    message = str(exc_info.value)
    assert "gemini" in message
    assert "anthropic" in message
    assert "openai" in message


def test_missing_gemini_api_key_raises_at_construction(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")

    with pytest.raises(MissingAPIKeyError) as exc_info:
        get_chat_model(provider="gemini")

    assert "GEMINI_API_KEY" in str(exc_info.value)


def test_missing_anthropic_api_key_raises_at_construction(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(MissingAPIKeyError) as exc_info:
        get_chat_model(provider="anthropic")

    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_missing_openai_api_key_raises_at_construction(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(MissingAPIKeyError) as exc_info:
        get_chat_model(provider="openai")

    assert "OPENAI_API_KEY" in str(exc_info.value)


def test_gemini_model_is_constructed_with_the_configured_model_and_temperature(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-tests")
    captured = {}

    class FakeChatGoogleGenerativeAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        langchain_google_genai, "ChatGoogleGenerativeAI", FakeChatGoogleGenerativeAI
    )

    get_chat_model(provider="gemini")

    assert captured["model"] == config.MODELS["gemini"]
    assert captured["temperature"] == config.TEMPERATURE
    assert captured["google_api_key"] == "fake-key-for-tests"


def test_get_chat_model_overrides_the_model_name(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-key-for-tests")
    captured = {}

    class FakeChatGoogleGenerativeAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        langchain_google_genai, "ChatGoogleGenerativeAI", FakeChatGoogleGenerativeAI
    )

    get_chat_model(provider="gemini", model="gemini-experimental")

    assert captured["model"] == "gemini-experimental"


# --- LLMGateway.invoke: the instrumented entry point ----------------------------------


class FakeModel:
    """Stands in for a LangChain chat model. Queues responses/exceptions and
    records every call so tests can assert on real behaviour."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_gateway(outcomes, rpm_limit=15, daily_limit=1500, max_attempts=4, clock=None):
    clock = clock or FakeClock()
    guard, _ = make_guard(rpm_limit=rpm_limit, daily_limit=daily_limit, clock=clock)
    model = FakeModel(outcomes)
    gateway = LLMGateway(
        model=model,
        guard=guard,
        max_attempts=max_attempts,
        sleep_fn=clock.sleep,
    )
    return gateway, model, clock


def test_invoke_returns_the_model_response():
    gateway, model, clock = make_gateway(["hello"])
    result = gateway.invoke(["hi"], purpose="buyer_planner")
    assert result == "hello"
    assert model.calls == 1


def test_invoke_counts_calls_per_purpose():
    gateway, model, clock = make_gateway(["a", "b", "c"])
    gateway.invoke([], purpose="buyer_planner")
    gateway.invoke([], purpose="buyer_planner")
    gateway.invoke([], purpose="negotiator")

    assert gateway.purpose_counts == {"buyer_planner": 2, "negotiator": 1}


def test_invoke_retries_a_transient_error_and_returns_the_eventual_success():
    gateway, model, clock = make_gateway(
        [TransientLLMError("429"), TransientLLMError("429"), "third time lucky"]
    )
    result = gateway.invoke([], purpose="negotiator")

    assert result == "third time lucky"
    assert model.calls == 3
    assert len(clock.slept) == 2, "one backoff sleep per failed attempt"


def test_invoke_recognises_a_real_provider_429_as_transient():
    """Real provider SDKs don't raise our TransientLLMError; they raise their
    own exception with '429' or 'rate limit' in the message. The retry path
    has to recognise those too, not just our own marker class."""
    gateway, model, clock = make_gateway(
        [RuntimeError("429 Resource has been exhausted"), "ok"]
    )
    result = gateway.invoke([], purpose="negotiator")

    assert result == "ok"
    assert model.calls == 2


def test_invoke_gives_up_after_the_attempt_cap_and_raises_a_typed_error():
    gateway, model, clock = make_gateway(
        [TransientLLMError("429")] * 10, max_attempts=3
    )

    with pytest.raises(RetryBudgetExceededError) as exc_info:
        gateway.invoke([], purpose="negotiator")

    assert model.calls == 3
    assert exc_info.value.attempts == 3


def test_invoke_does_not_retry_a_non_transient_error():
    gateway, model, clock = make_gateway([ValueError("malformed prompt")])

    with pytest.raises(ValueError):
        gateway.invoke([], purpose="negotiator")

    assert model.calls == 1, "a non-transient error must not burn retry attempts"


def test_every_retry_attempt_passes_back_through_the_rate_guard():
    """A retry storm that bypasses the limiter is worse than the original
    failure. With rpm_limit=1, the retry attempt must wait out both its own
    backoff delay AND the guard's per-minute window before the underlying
    model is called again — the two sleeps are separate calls (backoff, then
    whatever is left of the 60s window) but must sum to the full window."""
    clock = FakeClock()
    gateway, model, clock = make_gateway(
        [TransientLLMError("429"), "ok"], rpm_limit=1, clock=clock
    )

    result = gateway.invoke([], purpose="negotiator")

    assert result == "ok"
    assert len(clock.slept) == 2, "expected one backoff sleep and one rate-guard sleep"
    assert clock.now == pytest.approx(60.0), "combined wait must cover the full minute window"


def test_invoke_propagates_the_daily_budget_error_without_wrapping_it():
    gateway, model, clock = make_gateway(["a", "b"], daily_limit=1)
    gateway.invoke([], purpose="negotiator")

    with pytest.raises(DailyBudgetExceededError):
        gateway.invoke([], purpose="negotiator")


def test_gateway_exposes_the_rate_guard_state():
    gateway, model, clock = make_gateway(["a"], rpm_limit=15, daily_limit=1500)
    gateway.invoke([], purpose="negotiator")

    assert gateway.rate_guard_state.requests_this_minute == 1


# --- NVIDIA fast lane + per-purpose routing -------------------------------------------


def test_nvidia_provider_is_constructed_with_base_url_and_key(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "fake-nvidia-key")
    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)

    get_chat_model(provider="nvidia")

    assert captured["model"] == config.MODELS["nvidia"]
    assert captured["api_key"] == "fake-nvidia-key"
    assert captured["base_url"] == config.NVIDIA_BASE_URL
    assert captured["temperature"] == config.TEMPERATURE


def test_missing_nvidia_api_key_raises_at_construction(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")

    with pytest.raises(MissingAPIKeyError) as exc_info:
        get_chat_model(provider="nvidia")

    assert "NVIDIA_API_KEY" in str(exc_info.value)


def test_unknown_provider_error_now_names_nvidia_too():
    with pytest.raises(UnknownProviderError) as exc_info:
        get_chat_model(provider="watson")
    assert "nvidia" in str(exc_info.value)


def test_prose_surfaces_route_to_the_nvidia_fast_lane(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "present")
    for purpose in config.FAST_LLM_SURFACES:
        assert provider_for_purpose(purpose) == "nvidia"


def test_numeric_and_unknown_surfaces_stay_on_the_default_provider(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "present")
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    # intent_compiler drafts paise; negotiator/planner reason over numbers.
    for purpose in ("intent_compiler", "buyer_planner", "negotiator", "evaluator"):
        assert provider_for_purpose(purpose) == "gemini"


def test_fast_surface_falls_back_to_default_when_no_nvidia_key(monkeypatch):
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    # a prose surface must not hard-fail just because NVIDIA isn't configured
    assert provider_for_purpose("storefront") == "gemini"


def test_gateway_routes_each_purpose_to_the_right_provider_model(monkeypatch):
    """With no explicit model injected, the gateway builds one model per
    provider (lazily) and sends each purpose to the correct one."""
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "present")
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")

    built = []

    def fake_get_chat_model(*, provider):
        built.append(provider)
        m = FakeModel(["ok"])
        m.provider = provider
        return m

    monkeypatch.setattr("buyer.llm.get_chat_model", fake_get_chat_model)

    guard, _ = make_guard()
    gateway = LLMGateway(guard=guard, sleep_fn=FakeClock().sleep)  # no explicit model

    # a numeric surface -> gemini; a prose surface -> nvidia
    m_numeric = gateway._model_for("intent_compiler")
    m_prose = gateway._model_for("storefront")

    assert m_numeric.provider == "gemini"
    assert m_prose.provider == "nvidia"
    # each provider built exactly once, then cached
    gateway._model_for("intent_compiler")
    assert built.count("gemini") == 1 and built.count("nvidia") == 1
