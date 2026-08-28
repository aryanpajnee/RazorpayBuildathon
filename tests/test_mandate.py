"""Mandate tests, anchored to the test vectors in docs/specs/mandate-spec.md.

Those vectors were written and committed before the implementation existed, so
they are a genuine specification rather than a recording of whatever the code
happens to do. If a change to core/mandate.py breaks one of these, the change
is wrong.
"""

import copy
import hashlib
import json

import pytest
from nacl.signing import SigningKey

from core.mandate import (
    MandateVerificationError,
    canonical,
    cart_hash,
    generate_keypair,
    load_signing_key,
    load_verify_key,
    make_cart_mandate,
    make_intent_mandate,
    save_keypair,
    sign,
    verify,
)

# The fixed keypair from spec section 6.
TEST_SEED = bytes.fromhex("00" * 31 + "01")
TEST_PUBLIC_KEY = "4cb5abf6ad79fbf5abbccafcc269d85cd2651ed4b885b5869f241aedf0a5ba29"

VECTOR_INTENT = json.loads(
    '{"agent_id":"agt_northwind_shopper","category":"footwear","currency":"INR",'
    '"expires_at":1788000000,"issued_at":1787900000,"mandate_id":"man_int_0001",'
    '"max_paise":500000,"max_purchases":1,"merchant_id":null,"type":"intent",'
    '"user_id":"usr_aryan","version":"1.0"}'
)
VECTOR_CART = json.loads(
    '{"agent_id":"agt_northwind_shopper","cart_hash":"b8f1c200000000000000000000'
    '00000000000000000000000000000000000000","currency":"INR",'
    '"intent_mandate_id":"man_int_0001","issued_at":1787900500,'
    '"mandate_id":"man_cart_0001","merchant_id":"merch_northwind",'
    '"nonce":"nonce_0001","quote_id":"qt_0001","total_paise":476800,'
    '"type":"cart","version":"1.0"}'
)


@pytest.fixture
def sk() -> SigningKey:
    return SigningKey(TEST_SEED)


# --- spec section 7.1: canonical length -------------------------------------

def test_the_test_seed_derives_the_public_key_the_spec_names(sk):
    assert sk.verify_key.encode().hex() == TEST_PUBLIC_KEY


def test_intent_vector_canonicalises_to_exactly_260_bytes():
    """If this fails, separators or ensure_ascii is wrong and nothing else
    in the file can be correct."""
    assert len(canonical(VECTOR_INTENT)) == 260


def test_cart_vector_canonicalises_to_exactly_344_bytes():
    assert len(canonical(VECTOR_CART)) == 344


# --- spec section 7.2: hashes -----------------------------------------------

def test_intent_vector_reproduces_the_documented_sha256():
    assert (
        hashlib.sha256(canonical(VECTOR_INTENT)).hexdigest()
        == "f24a4de95dbdf1eeb10e211b88bd1d306f331cc59df3bf692682992e829f8c89"
    )


def test_cart_vector_reproduces_the_documented_sha256():
    assert (
        hashlib.sha256(canonical(VECTOR_CART)).hexdigest()
        == "b772b9c5c3df1365d0271592ad36725ee6c9b5a62802c7762d9d1bed1eaf21a5"
    )


# --- spec section 7.3: signatures -------------------------------------------

def test_intent_vector_reproduces_the_documented_signature(sk):
    assert sign(VECTOR_INTENT, sk)["signature"] == (
        "9755b64aa29455198cce38c666d4b1720350b1fd19779f6d9048faf1408b815d"
        "99a72df0ecb0921b6f95f223b895b28dac6535e5bba0b33073257f6ebe11fd04"
    )


def test_cart_vector_reproduces_the_documented_signature(sk):
    assert sign(VECTOR_CART, sk)["signature"] == (
        "eb2812b22a2319b3bd5426e5c33962885c17d8b1c80430608b6989a9473bb1ee"
        "6629d8cea939d6ad9a7f66ff76e6f0b938c07b0325c319603047f1c2d557070b"
    )


# --- canonical serialisation properties -------------------------------------

def test_key_insertion_order_does_not_change_the_bytes():
    """Two dicts a human reads as identical must serialise identically."""
    forward = {"a": 1, "b": 2, "c": 3}
    reverse = {"c": 3, "b": 2, "a": 1}
    assert canonical(forward) == canonical(reverse)


def test_canonical_emits_no_whitespace():
    assert b", " not in canonical(VECTOR_INTENT)
    assert b": " not in canonical(VECTOR_INTENT)


def test_non_ascii_is_escaped_rather_than_emitted_raw():
    assert "\\u20b9" in canonical({"note": "₹500"}).decode("ascii")


def test_a_float_anywhere_in_a_payload_is_refused(sk):
    """476800 must never become 476800.0. Raise, never coerce."""
    with pytest.raises(TypeError):
        canonical({"total_paise": 476800.0})


def test_a_float_nested_deep_inside_a_payload_is_still_refused():
    with pytest.raises(TypeError):
        canonical({"cart": {"lines": [{"unit_paise": 499900.0}]}})


# --- cart_hash --------------------------------------------------------------

def test_cart_hash_is_sixty_four_hex_characters():
    h = cart_hash([{"sku": "A", "qty": 1, "unit_paise": 100}])
    assert len(h) == 64
    int(h, 16)  # raises if it is not hex


def test_the_same_cart_hashes_identically_one_hundred_times():
    cart = [
        {"sku": "NW-SHOE-001", "qty": 1, "unit_paise": 499900},
        {"sku": "NW-SOCK-001", "qty": 2, "unit_paise": 79900},
    ]
    first = cart_hash(cart)
    for _ in range(100):
        assert cart_hash(cart) == first


def test_line_order_does_not_change_the_cart_hash():
    """sort_keys orders keys inside a dict but not elements of a list, so
    without an explicit sort an identical cart sent in a different order would
    hash differently and the Gate would refuse an honest cart."""
    a = {"sku": "NW-SHOE-001", "qty": 1, "unit_paise": 499900}
    b = {"sku": "NW-SOCK-001", "qty": 2, "unit_paise": 79900}
    assert cart_hash([a, b]) == cart_hash([b, a])


def test_changing_a_quantity_changes_the_cart_hash():
    one = [{"sku": "A", "qty": 1, "unit_paise": 100}]
    two = [{"sku": "A", "qty": 2, "unit_paise": 100}]
    assert cart_hash(one) != cart_hash(two)


# --- spec section 7.4 to 7.6: round-trip, tamper, serialisation -------------

def test_a_signed_mandate_verifies_and_returns_its_payload(sk):
    assert verify(sign(VECTOR_INTENT, sk)) == VECTOR_INTENT


def test_a_tampered_payload_is_rejected(sk):
    """The single most important test in the project. If a tampered mandate
    verifies, nothing else here means anything."""
    envelope = copy.deepcopy(sign(VECTOR_INTENT, sk))
    envelope["payload"]["max_paise"] = 600000
    with pytest.raises(MandateVerificationError):
        verify(envelope)


def test_flipping_one_byte_of_the_signature_is_rejected(sk):
    envelope = sign(VECTOR_INTENT, sk)
    flipped = "0" if envelope["signature"][0] != "0" else "1"
    envelope["signature"] = flipped + envelope["signature"][1:]
    with pytest.raises(MandateVerificationError):
        verify(envelope)


def test_an_envelope_survives_a_json_round_trip(sk):
    """A canonical bug can pass the in-memory round-trip and fail here, because
    this is where key ordering actually gets shuffled."""
    envelope = sign(VECTOR_INTENT, sk)
    assert verify(json.loads(json.dumps(envelope))) == VECTOR_INTENT


def test_a_mandate_signed_by_a_different_key_does_not_verify_against_this_one(sk):
    """Substituting only the public key must fail - otherwise anyone could
    claim authorship of a payload they did not sign."""
    envelope = sign(VECTOR_INTENT, sk)
    other, _ = generate_keypair()
    envelope["public_key"] = other.verify_key.encode().hex()
    with pytest.raises(MandateVerificationError):
        verify(envelope)


# --- malformed envelopes ----------------------------------------------------

@pytest.mark.parametrize("missing", ["payload", "signature", "public_key", "alg"])
def test_an_envelope_missing_a_required_field_is_rejected(sk, missing):
    envelope = sign(VECTOR_INTENT, sk)
    del envelope[missing]
    with pytest.raises(MandateVerificationError):
        verify(envelope)


def test_a_non_ed25519_algorithm_is_rejected(sk):
    """Refusing an unknown alg outright stops an attacker downgrading to
    something weaker or to 'none'."""
    envelope = sign(VECTOR_INTENT, sk)
    envelope["alg"] = "HS256"
    with pytest.raises(MandateVerificationError):
        verify(envelope)


def test_a_wrong_length_signature_is_rejected(sk):
    envelope = sign(VECTOR_INTENT, sk)
    envelope["signature"] = "ab" * 16
    with pytest.raises(MandateVerificationError):
        verify(envelope)


def test_non_hex_characters_in_the_signature_are_rejected(sk):
    envelope = sign(VECTOR_INTENT, sk)
    envelope["signature"] = "z" * 128
    with pytest.raises(MandateVerificationError):
        verify(envelope)


def test_verify_rejects_something_that_is_not_an_envelope():
    with pytest.raises(MandateVerificationError):
        verify("not an envelope")


# --- construction -----------------------------------------------------------

def test_an_intent_mandate_has_every_field_the_spec_lists():
    payload = make_intent_mandate(
        user_id="usr_aryan", agent_id="agt_x", agent_pubkey="ab" * 32,
        category="footwear",
        max_paise=500000, max_purchases=1, ttl_seconds=3600,
    )
    assert set(payload) == {
        "version", "type", "mandate_id", "user_id", "agent_id", "agent_pubkey",
        "category", "max_paise", "max_purchases", "currency", "issued_at",
        "expires_at", "merchant_id",
    }
    assert payload["type"] == "intent"
    assert payload["merchant_id"] is None  # None means any merchant


def test_intent_mandate_rejects_a_malformed_agent_pubkey():
    """The bound agent key is what the Gate later trusts; a wrong-length or
    non-hex key must fail at construction, not silently at gate time."""
    with pytest.raises(ValueError):
        make_intent_mandate(
            user_id="u", agent_id="a", agent_pubkey="tooshort",
            category="footwear", max_paise=1, max_purchases=1, ttl_seconds=90,
        )
    with pytest.raises(ValueError):
        make_intent_mandate(
            user_id="u", agent_id="a", agent_pubkey="zz" * 32,  # 64 chars, not hex
            category="footwear", max_paise=1, max_purchases=1, ttl_seconds=90,
        )


def test_a_cart_mandate_has_every_field_the_spec_lists():
    payload = make_cart_mandate(
        intent_mandate_id="man_int_0001", agent_id="agt_x",
        merchant_id="merch_northwind", quote_id="qt_0001",
        cart_hash="b8" + "0" * 62, total_paise=476800,
    )
    assert set(payload) == {
        "version", "type", "mandate_id", "intent_mandate_id", "agent_id",
        "merchant_id", "quote_id", "cart_hash", "total_paise", "currency",
        "nonce", "issued_at",
    }
    assert payload["type"] == "cart"


def test_timestamps_are_unix_ints_not_iso_strings():
    """ISO strings carry timezone and formatting ambiguity into signed bytes."""
    payload = make_intent_mandate(
        user_id="u", agent_id="a", agent_pubkey="ab" * 32, category="footwear",
        max_paise=1, max_purchases=1, ttl_seconds=90,
    )
    assert type(payload["issued_at"]) is int
    assert payload["expires_at"] - payload["issued_at"] == 90


def test_two_cart_mandates_never_share_a_nonce():
    """The nonce is the replay defence; a collision would let a mandate be
    presented twice."""
    kwargs = dict(
        intent_mandate_id="man_int_0001", agent_id="a",
        merchant_id="m", quote_id="qt_0001",
        cart_hash="b8" + "0" * 62, total_paise=1,
    )
    nonces = {make_cart_mandate(**kwargs)["nonce"] for _ in range(100)}
    assert len(nonces) == 100


def test_a_float_amount_is_refused_at_construction():
    with pytest.raises(TypeError):
        make_intent_mandate(
            user_id="u", agent_id="a", agent_pubkey="ab" * 32, category="footwear",
            max_paise=5000.0, max_purchases=1, ttl_seconds=90,
        )


def test_a_bool_is_not_an_acceptable_purchase_count():
    with pytest.raises(TypeError):
        make_intent_mandate(
            user_id="u", agent_id="a", agent_pubkey="ab" * 32, category="footwear",
            max_paise=5000, max_purchases=True, ttl_seconds=90,
        )


# --- keys -------------------------------------------------------------------

def test_a_saved_signing_key_loads_back_identically(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    sk, _ = generate_keypair()
    save_keypair(sk, "test_user")
    assert load_signing_key("test_user").encode() == sk.encode()


def test_a_saved_private_key_is_not_readable_by_anyone_else(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    sk, _ = generate_keypair()
    path = save_keypair(sk, "test_user")
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_the_stored_public_key_matches_the_private_key(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    sk, vk = generate_keypair()
    save_keypair(sk, "test_user")
    assert load_verify_key("test_user").encode() == vk.encode()


def test_loading_a_key_that_does_not_exist_fails_loudly(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    with pytest.raises(FileNotFoundError):
        load_signing_key("no_such_user")


def test_a_key_saved_on_disk_can_sign_something_that_verifies(tmp_path, monkeypatch):
    """End to end: generate, save, load, sign, verify."""
    import config
    monkeypatch.setattr(config, "KEY_DIR", tmp_path / "keys")
    sk, _ = generate_keypair()
    save_keypair(sk, "usr_aryan")
    payload = make_intent_mandate(
        user_id="usr_aryan", agent_id="agt_x", agent_pubkey="ab" * 32,
        category="footwear",
        max_paise=500000, max_purchases=1, ttl_seconds=3600,
    )
    assert verify(sign(payload, load_signing_key("usr_aryan"))) == payload
