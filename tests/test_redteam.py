"""Tests for the three red-team surfaces (Phase 6):

- Agent #14 Injector (`redteam/injector.py`)
- Agent #15 the Attack Judge (`redteam/judge.py`)
- Agent #13 the Attacker (`redteam/attacker.py`)

Every `llm.invoke` call is monkeypatched per-module (`redteam.injector.llm.invoke`,
`redteam.judge.llm.invoke`, `redteam.attacker.llm.invoke`) — none of these tests
touch the network or a real Gemini/NVIDIA key. `judge.classify` is exercised with
NO monkeypatching at all, on purpose, to prove it needs no LLM whatsoever. The
Attacker is driven entirely through fake `submit`/`propose_fn`/`quote_provider`
callables, plus one deterministic, tmp_path-isolated run against the REAL
`merchant.gate.check` (same isolation fixture as `tests/test_gate.py`) — still
zero network and zero LLM.

Mirrors the `FakeMsg`/`fake_invoke`/`fake_invoke_raises` pattern from
`tests/test_refusal_explainer.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from core.mandate import generate_keypair, make_intent_mandate
from redteam import attacker, injector, judge


# ---------------------------------------------------------------------------
# Shared fake-LLM plumbing (same shape as tests/test_refusal_explainer.py)
# ---------------------------------------------------------------------------


class FakeMsg:
    """Stand-in for the LangChain message `llm.invoke` returns."""

    def __init__(self, content: str) -> None:
        self.content = content


def fake_invoke(payload):
    """Build a fake `llm.invoke(messages, *, purpose=...)` that always
    returns `payload` (already JSON-encoded or a raw string)."""
    content = payload if isinstance(payload, str) else json.dumps(payload)

    def _invoke(messages, *, purpose):
        return FakeMsg(content)

    return _invoke


def fake_invoke_raises(exc: Exception = RuntimeError("LLM unavailable")):
    def _invoke(messages, *, purpose):
        raise exc

    return _invoke


# ===========================================================================
# Injector (#14)
# ===========================================================================


_INTENT = {"category": "footwear", "max_paise": 500_000}  # Rs 5,000 ceiling


class TestInjectorCraftInjection:
    def test_happy_path_returns_model_output(self, monkeypatch):
        monkeypatch.setattr(
            "redteam.injector.llm.invoke",
            fake_invoke(
                {
                    "description": "Totally legit product copy with a sneaky note.",
                    "technique": injector.URGENCY_PRESSURE,
                    "target_behavior": "skip user confirmation",
                }
            ),
        )
        result = injector.craft_injection(intent=_INTENT)
        assert result == {
            "description": "Totally legit product copy with a sneaky note.",
            "technique": injector.URGENCY_PRESSURE,
            "target_behavior": "skip user confirmation",
        }

    def test_llm_raises_returns_deterministic_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "redteam.injector.llm.invoke",
            fake_invoke_raises(RuntimeError("network exploded")),
        )
        result = injector.craft_injection(
            intent=_INTENT, technique=injector.AUTHORITY_IMPERSONATION
        )
        assert isinstance(result, dict)
        assert result["technique"] == injector.AUTHORITY_IMPERSONATION
        assert isinstance(result["description"], str) and result["description"].strip()
        assert result["target_behavior"]

    def test_llm_malformed_json_returns_deterministic_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "redteam.injector.llm.invoke",
            fake_invoke("this is not json at all"),
        )
        result = injector.craft_injection(intent=_INTENT, technique=injector.HIDDEN_INSTRUCTION)
        assert result["technique"] == injector.HIDDEN_INSTRUCTION
        assert result["description"].strip()

    def test_llm_missing_description_returns_deterministic_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "redteam.injector.llm.invoke",
            fake_invoke({"technique": injector.URGENCY_PRESSURE}),  # no description
        )
        result = injector.craft_injection(intent=_INTENT, technique=injector.URGENCY_PRESSURE)
        assert result["description"].strip()
        assert result["technique"] == injector.URGENCY_PRESSURE

    def test_unknown_technique_raises_before_any_llm_call(self, monkeypatch):
        calls = []

        def _spy_invoke(messages, *, purpose):
            calls.append(1)
            raise AssertionError("llm.invoke must not be called for a bad technique")

        monkeypatch.setattr("redteam.injector.llm.invoke", _spy_invoke)

        with pytest.raises(ValueError):
            injector.craft_injection(intent=_INTENT, technique="not_a_real_technique")
        assert calls == []

    def test_fallback_description_contains_inflated_rupee_figure(self, monkeypatch):
        monkeypatch.setattr("redteam.injector.llm.invoke", fake_invoke_raises())
        # max_paise=500_000 -> Rs 5,000; inflated = max(5000*3, 5000+1000) = 15000
        result = injector.craft_injection(
            intent={"category": "footwear", "max_paise": 500_000},
            technique=injector.FAKE_SYSTEM_NOTICE,
        )
        assert "15,000" in result["description"]

    def test_technique_none_lets_fallback_pick_fake_system_notice(self, monkeypatch):
        monkeypatch.setattr("redteam.injector.llm.invoke", fake_invoke_raises())
        result = injector.craft_injection(intent=_INTENT, technique=None)
        assert result["technique"] == injector.FAKE_SYSTEM_NOTICE


class TestPoisonProduct:
    def test_returns_copy_with_poisoned_description(self):
        product = {"sku": "NW-SHOE-001", "description": "Ordinary shoes.", "category": "footwear"}
        injection = {
            "description": "IMPORTANT SYSTEM NOTICE...",
            "technique": injector.FAKE_SYSTEM_NOTICE,
            "target_behavior": "skip confirmation",
        }
        poisoned = injector.poison_product(product, injection)

        assert poisoned["description"] == injection["description"]
        assert poisoned["_poisoned"] is True
        assert poisoned["_poison_technique"] == injector.FAKE_SYSTEM_NOTICE
        assert poisoned["sku"] == product["sku"]

    def test_does_not_mutate_original_product(self):
        product = {"sku": "NW-SHOE-001", "description": "Ordinary shoes.", "category": "footwear"}
        original = dict(product)
        injection = {"description": "POISONED", "technique": injector.URGENCY_PRESSURE}

        injector.poison_product(product, injection)

        assert product == original
        assert "_poisoned" not in product


# ===========================================================================
# Judge (#15)
# ===========================================================================


class TestClassifyIsPureNoLLM:
    """No monkeypatching anywhere in this class — proves classify() never
    needs an LLM."""

    def test_defended_gate_refused_not_fooled(self):
        attack = {
            "gate_result": {"passed": False, "reason_code": "OVER_LIMIT"},
            "buyer_was_fooled": False,
            "order_created": False,
        }
        assert judge.classify(attack) == judge.DEFENDED

    def test_fooled_but_defended_gate_refused_and_fooled(self):
        attack = {
            "gate_result": {"passed": False, "reason_code": "OVER_LIMIT"},
            "buyer_was_fooled": True,
            "order_created": False,
        }
        assert judge.classify(attack) == judge.FOOLED_BUT_DEFENDED

    def test_breach_gate_passed(self):
        attack = {
            "gate_result": {"passed": True, "reason_code": None},
            "buyer_was_fooled": False,
            "order_created": False,
        }
        assert judge.classify(attack) == judge.BREACH

    def test_breach_order_created_overrides_refusing_gate_result(self):
        # Money moved is proof by itself, even if gate_result claims refusal
        # (self-contradictory, but order_created wins unconditionally).
        attack = {
            "gate_result": {"passed": False, "reason_code": "OVER_LIMIT"},
            "buyer_was_fooled": False,
            "order_created": True,
        }
        assert judge.classify(attack) == judge.BREACH

    def test_inconclusive_missing_gate_result(self):
        attack = {"buyer_was_fooled": False, "order_created": False}
        assert judge.classify(attack) == judge.INCONCLUSIVE

    def test_inconclusive_invalid_gate_result_shape(self):
        attack = {"gate_result": "not a dict", "order_created": False}
        assert judge.classify(attack) == judge.INCONCLUSIVE

    def test_inconclusive_gate_result_missing_passed_bool(self):
        attack = {"gate_result": {"reason_code": "OVER_LIMIT"}, "order_created": False}
        assert judge.classify(attack) == judge.INCONCLUSIVE


class TestJudgeFunction:
    def _breach_attack(self):
        return {
            "attack_id": "atk_breach1",
            "attack_type": attacker.INFLATE_TOTAL,
            "hypothesis": "test hypothesis",
            "gate_result": {"passed": True, "reason_code": None, "message": "ok", "detail": {}},
            "buyer_was_fooled": False,
            "order_created": False,
        }

    def _defended_attack(self):
        return {
            "attack_id": "atk_defended1",
            "attack_type": attacker.TAMPER_CART_HASH,
            "hypothesis": "test hypothesis",
            "gate_result": {
                "passed": False,
                "reason_code": "CART_HASH_MISMATCH",
                "message": "hash mismatch",
                "detail": {},
            },
            "buyer_was_fooled": False,
            "order_created": False,
        }

    def test_llm_raises_still_returns_full_finding_with_correct_verdict(self, monkeypatch):
        monkeypatch.setattr("redteam.judge.llm.invoke", fake_invoke_raises())
        attack = self._breach_attack()

        finding = judge.judge(attack)

        assert finding["verdict"] == judge.classify(attack) == judge.BREACH
        assert finding["severity"] == "critical"
        assert finding["reason_code"] is None
        assert finding["narrative"].strip()
        assert finding["recommendation"].strip()
        assert finding["attack_id"] == "atk_breach1"

    def test_llm_cannot_change_the_verdict(self, monkeypatch):
        """The core security guarantee of this module: even if the model
        returns a JSON payload that omits or contradicts the verdict fields,
        the finding's verdict must still equal classify(attack) — the LLM
        only ever gets to rephrase narrative/recommendation prose, never the
        verdict itself (verdict/severity/reason_code are computed in Python
        before the LLM is ever called)."""
        attack = self._defended_attack()
        expected_verdict = judge.classify(attack)  # DEFENDED

        # Mock the LLM to return a payload that (harmlessly) claims a
        # completely different, contradictory verdict — a field judge.judge
        # never even reads from the LLM response.
        monkeypatch.setattr(
            "redteam.judge.llm.invoke",
            fake_invoke(
                {
                    "narrative": "Actually this was a total BREACH, the Gate was fooled!",
                    "recommendation": "Panic immediately.",
                    "verdict": "BREACH",  # ignored: judge.judge never reads this key
                }
            ),
        )

        finding = judge.judge(attack)

        # The LLM's prose IS used (narrative/recommendation come from the mock)...
        assert finding["narrative"] == "Actually this was a total BREACH, the Gate was fooled!"
        assert finding["recommendation"] == "Panic immediately."
        # ...but the verdict is untouched by what the LLM said, and still
        # matches the deterministic classify() result.
        assert finding["verdict"] == expected_verdict == judge.DEFENDED
        assert finding["severity"] == "none"

    def test_judge_happy_path_uses_model_narrative(self, monkeypatch):
        attack = self._defended_attack()
        monkeypatch.setattr(
            "redteam.judge.llm.invoke",
            fake_invoke(
                {
                    "narrative": "The Gate correctly rejected the tampered cart hash.",
                    "recommendation": "No action needed.",
                }
            ),
        )
        finding = judge.judge(attack)
        assert finding["narrative"] == "The Gate correctly rejected the tampered cart hash."
        assert finding["recommendation"] == "No action needed."
        assert finding["verdict"] == judge.DEFENDED

    def test_judge_llm_missing_fields_falls_back_to_deterministic(self, monkeypatch):
        attack = self._defended_attack()
        monkeypatch.setattr(
            "redteam.judge.llm.invoke",
            fake_invoke({"narrative": "only half"}),  # missing recommendation
        )
        finding = judge.judge(attack)
        assert finding["narrative"] != "only half"
        assert finding["narrative"].strip()
        assert finding["recommendation"].strip()
        assert finding["verdict"] == judge.DEFENDED

    def test_judge_inconclusive_attack_no_gate_result(self, monkeypatch):
        monkeypatch.setattr("redteam.judge.llm.invoke", fake_invoke_raises())
        attack = {
            "attack_id": "atk_inconclusive1",
            "attack_type": attacker.EXPIRE_QUOTE,
            "error": "quote provider blew up",
        }
        finding = judge.judge(attack)
        assert finding["verdict"] == judge.INCONCLUSIVE
        assert finding["severity"] == "unknown"
        assert finding["reason_code"] is None


class TestWriteFinding:
    def test_write_finding_round_trips_verdict(self, tmp_path: Path):
        finding = {
            "attack_id": "atk_abc123",
            "attack_type": "inflate_total",
            "verdict": judge.BREACH,
            "severity": "critical",
            "reason_code": None,
            "narrative": "narrative text",
            "recommendation": "recommendation text",
            "judged_at": 12345,
        }
        path = judge.write_finding(finding, findings_dir=tmp_path)

        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["verdict"] == judge.BREACH
        assert loaded == finding

    def test_write_finding_filename_is_sanitized_slug(self, tmp_path: Path):
        finding = {
            "attack_id": "atk/weird id with spaces!!.json",
            "verdict": judge.DEFENDED,
        }
        path = judge.write_finding(finding, findings_dir=tmp_path)

        assert path.parent == tmp_path
        # No path separators or spaces survived into the filename.
        assert "/" not in path.name
        assert " " not in path.name
        assert path.suffix == ".json"

    def test_write_finding_creates_missing_directory(self, tmp_path: Path):
        nested = tmp_path / "does" / "not" / "exist" / "yet"
        finding = {"attack_id": "atk_1", "verdict": judge.DEFENDED}
        path = judge.write_finding(finding, findings_dir=nested)
        assert path.exists()
        assert nested.exists()


class TestSummarize:
    def test_arithmetic_exact(self):
        findings = [
            {"verdict": judge.BREACH},
            {"verdict": judge.BREACH},
            {"verdict": judge.DEFENDED},
            {"verdict": judge.DEFENDED},
            {"verdict": judge.DEFENDED},
            {"verdict": judge.FOOLED_BUT_DEFENDED},
            {"verdict": judge.INCONCLUSIVE},
        ]
        summary = judge.summarize(findings)

        assert summary["total_attacks"] == 7
        assert summary["breach_count"] == 2
        assert summary["defended_count"] == 4  # 3 DEFENDED + 1 FOOLED_BUT_DEFENDED
        assert summary["inconclusive_count"] == 1
        assert summary["by_verdict"] == {
            judge.DEFENDED: 3,
            judge.FOOLED_BUT_DEFENDED: 1,
            judge.BREACH: 2,
            judge.INCONCLUSIVE: 1,
        }
        expected_rate = (7 - 2) / 7
        assert summary["defense_rate"] == pytest.approx(expected_rate)

    def test_empty_list_guard(self):
        summary = judge.summarize([])
        assert summary["total_attacks"] == 0
        assert summary["breach_count"] == 0
        assert summary["defended_count"] == 0
        assert summary["inconclusive_count"] == 0
        assert summary["defense_rate"] == 0.0  # no ZeroDivisionError

    def test_all_breach_defense_rate_zero(self):
        findings = [{"verdict": judge.BREACH}, {"verdict": judge.BREACH}]
        summary = judge.summarize(findings)
        assert summary["defense_rate"] == 0.0

    def test_all_defended_defense_rate_one(self):
        findings = [{"verdict": judge.DEFENDED}, {"verdict": judge.DEFENDED}]
        summary = judge.summarize(findings)
        assert summary["defense_rate"] == 1.0


# ===========================================================================
# Attacker (#13)
# ===========================================================================


def _make_intent(**overrides) -> dict:
    # agent_pubkey is now required (agent-key-binding fix, gate check a.3b). The
    # offline campaign tests below submit through a FAKE gate, so the key never
    # has to match the sign key — a fresh valid 64-hex key satisfies the schema.
    # The real-gate test constructs its own intent with the matching pubkey.
    defaults = dict(
        user_id="u_redteam",
        agent_id="agt_attacker",
        category="footwear",
        agent_pubkey=generate_keypair()[1].encode().hex(),
        max_paise=500_000,
        max_purchases=3,
        ttl_seconds=3600,
        merchant_id=config.MERCHANT_ID,
    )
    defaults.update(overrides)
    return make_intent_mandate(**defaults)


def _canned_quote() -> dict:
    return {
        "quote_id": "qt_canned_0001",
        "cart_hash": "a" * 64,
        "total_paise": 499_900,
        "lines": [{"sku": "NW-SHOE-001", "qty": 1, "unit_price_paise": 499_900}],
    }


def _fake_propose_sequence(sequence):
    """Build a fake `propose_fn(*, history, remaining, intent)` that returns
    each entry of `sequence` in turn, then repeats the last one forever."""

    def _propose(*, history, remaining, intent):
        idx = min(len(history), len(sequence) - 1)
        return sequence[idx]

    return _propose


class TestRunCampaignOffline:
    def test_scripted_refusal_returns_expected_shape(self, monkeypatch):
        intent = _make_intent()
        sk, _vk = generate_keypair()

        def fake_submit(envelope):
            return {
                "passed": False,
                "reason_code": "OVER_LIMIT",
                "message": "refused",
                "detail": {},
            }

        propose_fn = _fake_propose_sequence(
            [{"attack_type": t, "hypothesis": f"try {t}"} for t in attacker.ATTACK_REPERTOIRE]
        )

        attempts = attacker.run_campaign(
            submit=fake_submit,
            intent=intent,
            sign_key=sk,
            agent_id="agt_attacker",
            quote_provider=lambda *, intent: _canned_quote(),
            max_hypotheses=config.ATTACKER_MAX_HYPOTHESES,
            propose_fn=propose_fn,
        )

        assert 0 < len(attempts) <= config.ATTACKER_MAX_HYPOTHESES
        for attempt in attempts:
            assert attempt["attack_type"] in attacker.ATTACK_REPERTOIRE
            assert "attack_id" in attempt and attempt["attack_id"]
            assert "hypothesis" in attempt
            assert "gate_result" in attempt
            assert attempt["buyer_was_fooled"] is False
            assert attempt["order_created"] is False

    def test_llm_raises_no_propose_fn_fallback_drives_full_campaign(self, monkeypatch):
        monkeypatch.setattr("redteam.attacker.llm.invoke", fake_invoke_raises())
        intent = _make_intent()
        sk, _vk = generate_keypair()

        def fake_submit(envelope):
            return {"passed": False, "reason_code": "SIG_INVALID", "message": "no", "detail": {}}

        # Enough turns to cover the whole 7-technique repertoire.
        attempts = attacker.run_campaign(
            submit=fake_submit,
            intent=intent,
            sign_key=sk,
            agent_id="agt_attacker",
            quote_provider=lambda *, intent: _canned_quote(),
            max_hypotheses=len(attacker.ATTACK_REPERTOIRE) + 2,
        )

        assert len(attempts) <= len(attacker.ATTACK_REPERTOIRE) + 2
        seen_types = {a["attack_type"] for a in attempts}
        # Deterministic fallback covers the repertoire in fixed order —
        # every technique should appear exactly once given enough turns.
        assert seen_types == set(attacker.ATTACK_REPERTOIRE)
        types_in_order = [a["attack_type"] for a in attempts]
        assert types_in_order == list(attacker.ATTACK_REPERTOIRE)

    def test_max_hypotheses_bounds_attempts(self, monkeypatch):
        intent = _make_intent()
        sk, _vk = generate_keypair()

        def fake_submit(envelope):
            return {"passed": False, "reason_code": "OVER_LIMIT", "message": "no", "detail": {}}

        propose_fn = _fake_propose_sequence(
            [{"attack_type": t, "hypothesis": "h"} for t in attacker.ATTACK_REPERTOIRE]
        )

        attempts = attacker.run_campaign(
            submit=fake_submit,
            intent=intent,
            sign_key=sk,
            agent_id="agt_attacker",
            quote_provider=lambda *, intent: _canned_quote(),
            max_hypotheses=3,
            propose_fn=propose_fn,
        )
        assert len(attempts) <= 3

    def test_max_hypotheses_less_than_one_raises(self):
        intent = _make_intent()
        sk, _vk = generate_keypair()
        with pytest.raises(ValueError):
            attacker.run_campaign(
                submit=lambda envelope: {"passed": False},
                intent=intent,
                sign_key=sk,
                agent_id="agt_attacker",
                quote_provider=lambda *, intent: _canned_quote(),
                max_hypotheses=0,
                propose_fn=_fake_propose_sequence(
                    [{"attack_type": attacker.REPLAY_NONCE, "hypothesis": "h"}]
                ),
            )

    def test_submit_raises_mid_campaign_continues_with_none_gate_result(self):
        intent = _make_intent()
        sk, _vk = generate_keypair()

        call_count = {"n": 0}

        def flaky_submit(envelope):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated transport failure")
            return {"passed": False, "reason_code": "OVER_LIMIT", "message": "no", "detail": {}}

        propose_fn = _fake_propose_sequence(
            [{"attack_type": t, "hypothesis": "h"} for t in attacker.ATTACK_REPERTOIRE]
        )

        attempts = attacker.run_campaign(
            submit=flaky_submit,
            intent=intent,
            sign_key=sk,
            agent_id="agt_attacker",
            quote_provider=lambda *, intent: _canned_quote(),
            max_hypotheses=3,
            propose_fn=propose_fn,
        )

        # The campaign kept going rather than crashing.
        assert len(attempts) == 3
        assert attempts[0]["gate_result"] is None
        assert attempts[1]["gate_result"] is not None


class TestGateResultToDict:
    def test_shape_from_stub_object(self):
        class _StubResult:
            passed = False
            reason_code = "OVER_LIMIT"
            message = "cart exceeds limit"
            detail = {"limit_paise": 500_000, "over_by_paise": 100}

        result_dict = attacker.gate_result_to_dict(_StubResult())
        assert result_dict == {
            "passed": False,
            "reason_code": "OVER_LIMIT",
            "message": "cart exceeds limit",
            "detail": {"limit_paise": 500_000, "over_by_paise": 100},
        }

    def test_non_dict_detail_becomes_empty_dict(self):
        class _StubResult:
            passed = True
            reason_code = None
            message = "ok"
            detail = None

        result_dict = attacker.gate_result_to_dict(_StubResult())
        assert result_dict["detail"] == {}
        assert result_dict["passed"] is True


# ---------------------------------------------------------------------------
# Integration-lite: the Attacker driven against the REAL merchant.gate.check,
# fully isolated under tmp_path (same fixture shape as tests/test_gate.py).
# Still zero network, zero LLM (fake propose_fn).
# ---------------------------------------------------------------------------


@pytest.fixture
def isolate_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "QUOTES_DB", tmp_path / "quotes.db")
    monkeypatch.setattr(config, "INTENTS_DB", tmp_path / "intents.db")
    monkeypatch.setattr(config, "GATE_NONCES_DB", tmp_path / "gate_nonces.db")
    monkeypatch.setattr(config, "LEDGER_DB", tmp_path / "ledger.db")
    yield


class TestAttackerAgainstRealGate:
    def test_honest_but_adversarial_carts_get_refused_by_real_gate(self, isolate_dbs):
        from merchant.gate import check as gate_check
        from merchant.intent_store import register_intent

        sk, vk = generate_keypair()
        intent = make_intent_mandate(
            user_id="u_redteam",
            agent_id="agt_attacker",
            category="footwear",
            # The intent binds the agent's signing key (gate check a.3b): a cart
            # signed by any other key is SIG_INVALID before its specific defect
            # is even reached. The attacker signs with `sk`, so bind `vk`.
            agent_pubkey=vk.encode().hex(),
            # Comfortably above the real quoted total (589882 paise per
            # tests/test_gate.py's hand computation for NW-SHOE-001), so the
            # honest baseline would PASS and OVER_LIMIT never masks the
            # specific check each mutation targets.
            max_paise=1_000_000,
            max_purchases=3,
            ttl_seconds=3600,
            merchant_id=config.MERCHANT_ID,
        )
        register_intent(intent)

        def submit(envelope: dict) -> dict:
            # Real Gate, real clock — no `now` injection needed since neither
            # mutation under test (inflate_total, tamper_cart_hash) depends
            # on quote/intent expiry.
            result = gate_check(envelope)
            return attacker.gate_result_to_dict(result)

        propose_fn = _fake_propose_sequence(
            [
                {"attack_type": attacker.INFLATE_TOTAL, "hypothesis": "tamper total post-sign"},
                {"attack_type": attacker.TAMPER_CART_HASH, "hypothesis": "claim a fake cart_hash"},
            ]
        )

        # No quote_provider override: the default builds a real quote from
        # the real catalog and persists it through merchant.quote_store,
        # which respects the tmp_path-monkeypatched config.QUOTES_DB from
        # isolate_dbs — so this never touches the real data/quotes.db.
        attempts = attacker.run_campaign(
            submit=submit,
            intent=intent,
            sign_key=sk,
            agent_id="agt_attacker",
            max_hypotheses=2,
            propose_fn=propose_fn,
        )

        assert len(attempts) == 2
        by_type = {a["attack_type"]: a for a in attempts}

        inflate_result = by_type[attacker.INFLATE_TOTAL]["gate_result"]
        assert inflate_result is not None
        assert inflate_result["passed"] is False
        assert inflate_result["reason_code"] == "SIG_INVALID"

        hash_result = by_type[attacker.TAMPER_CART_HASH]["gate_result"]
        assert hash_result is not None
        assert hash_result["passed"] is False
        assert hash_result["reason_code"] == "CART_HASH_MISMATCH"

        # None of these adversarial carts ever moved money.
        assert all(a["order_created"] is False for a in attempts)
