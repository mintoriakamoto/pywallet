"""
test_coin_registry
~~~~~~~~~~~~~~~~~~
Tests for coin parameter resolution, provenance, and WIF-byte guessing.

These run under Python 3 with no wallet.dat, no Berkeley DB, and no network:
each test builds its own CoinRegistry and stamps _last_fetch so the
miningpoolstats refresh never fires.

    python3 -m pytest tests/ -q
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multiwallet.coin_registry import (  # noqa: E402
    BITCOIN_WIF_VER,
    PROVENANCE_DEFAULT,
    PROVENANCE_GUESSED,
    PROVENANCE_MANUAL,
    PROVENANCE_NODE,
    PROVENANCE_TABLE,
    CoinInfo,
    CoinRegistry,
    wif_candidates,
)


@pytest.fixture
def registry():
    """A fresh, offline registry (never the process-wide singleton)."""
    reg = CoinRegistry()
    reg._last_fetch = time.time()  # suppress the MPS fetch
    return reg


# ---------------------------------------------------------------------------
# wif_candidates
# ---------------------------------------------------------------------------

def test_candidates_are_ranked_convention_first():
    assert wif_candidates(0x30) == [0xB0, BITCOIN_WIF_VER]  # LTC


def test_candidates_wrap_at_256():
    # UNO's pubkey byte is 0x82; 0x82 + 0x80 overflows a byte.
    assert wif_candidates(0x82)[0] == 0x02


def test_candidates_never_duplicate():
    # pubkey 0x00 (Bitcoin) makes both conventions agree on 0x80.
    assert wif_candidates(0x00) == [BITCOIN_WIF_VER]


@pytest.mark.parametrize("pubkey_ver,wif_ver", [
    (0x00, 0x80),  # BTC
    (0x30, 0xb0),  # LTC
    (0x1e, 0x9e),  # DOGE
    (0x4c, 0xcc),  # DASH
    (0x47, 0xc7),  # VTC
])
def test_first_candidate_correct_for_conventional_coins(pubkey_ver, wif_ver):
    assert wif_candidates(pubkey_ver)[0] == wif_ver


@pytest.mark.parametrize("ticker,pubkey_ver,wif_ver", [
    ("RVN", 0x3c, 0x80),
    ("GRS", 0x24, 0x80),
    ("QTUM", 0x3a, 0x80),
    ("IXC", 0x8a, 0x80),
])
def test_second_candidate_catches_bitcoin_wif_forks(ticker, pubkey_ver, wif_ver):
    """These four break convention 1 but are caught by convention 2."""
    candidates = wif_candidates(pubkey_ver)
    assert candidates[0] != wif_ver, "expected convention 1 to miss"
    assert candidates[1] == wif_ver


@pytest.mark.parametrize("pubkey_ver,wif_ver", [
    (0x1e, 0xd4),  # PIVX
    (0x89, 0x85),  # CLAM
    (0x82, 0xe0),  # UNO
])
def test_arbitrary_wif_bytes_are_not_guessable(pubkey_ver, wif_ver):
    """Documents the known limit: neither convention reaches these."""
    assert wif_ver not in wif_candidates(pubkey_ver)


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------

def test_static_table_coins_are_verified(registry):
    btc = registry.get("BTC")
    assert btc.provenance == PROVENANCE_TABLE
    assert btc.verified is True


def test_verified_coin_offers_only_its_known_byte(registry):
    assert registry.get("PIVX").wif_candidates == [0xD4]


def test_unverified_coin_offers_fallbacks():
    coin = CoinInfo(
        ticker="VOID", name="Voidcoin", pubkey_ver=0x3F, wif_ver=0xBF,
        p2sh_ver=None, bech32_hrp=None, bip44_index=None,
        provenance=PROVENANCE_GUESSED,
    )
    assert coin.verified is False
    assert coin.wif_candidates[0] == 0xBF
    assert BITCOIN_WIF_VER in coin.wif_candidates
    assert len(set(coin.wif_candidates)) == len(coin.wif_candidates)


def test_mps_only_coins_are_marked_default_not_trusted(registry):
    registry._apply_mps_data({"NEWCOIN": {"algo": "sha256", "name": "New Coin"}})
    coin = registry.get("NEWCOIN")
    assert coin.provenance == PROVENANCE_DEFAULT
    assert coin.verified is False


def test_mps_does_not_downgrade_a_table_coin(registry):
    registry._apply_mps_data({"BTC": {"algo": "sha256"}})
    btc = registry.get("BTC")
    assert btc.provenance == PROVENANCE_TABLE
    assert btc.algorithm == "sha256"
    assert btc.pubkey_ver == 0x00


def test_to_dict_exposes_provenance(registry):
    d = registry.get("BTC").to_dict()
    assert d["provenance"] == PROVENANCE_TABLE
    assert d["verified"] is True
    assert d["wif_candidates"] == [BITCOIN_WIF_VER]


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------

def test_register_unknown_coin_with_full_params(registry):
    coin = registry.register(
        "VOID", pubkey_ver=0x41, wif_ver=0xC1, p2sh_ver=0x08,
        bech32_hrp="void", name="Voidcoin",
    )
    assert coin.provenance == PROVENANCE_MANUAL
    assert coin.verified is True
    assert registry.get("VOID").bech32_hrp == "void"


def test_register_without_wif_guesses_and_flags_it(registry):
    coin = registry.register("BT3", pubkey_ver=0x19)
    assert coin.wif_ver == 0x99
    assert coin.provenance == PROVENANCE_GUESSED
    assert coin.verified is False


def test_register_uppercases_ticker(registry):
    registry.register("void", pubkey_ver=0x41, wif_ver=0xC1)
    assert registry.get("VOID") is not None


def test_register_overrides_a_table_coin(registry):
    registry.register("DOGE", pubkey_ver=0x1F, wif_ver=0x9F)
    assert registry.get("DOGE").pubkey_ver == 0x1F


@pytest.mark.parametrize("bad", [-1, 256, 0x1FF])
def test_register_rejects_out_of_range_bytes(registry, bad):
    with pytest.raises(ValueError):
        registry.register("BAD", pubkey_ver=bad)


def test_register_rejects_out_of_range_wif(registry):
    with pytest.raises(ValueError):
        registry.register("BAD", pubkey_ver=0x10, wif_ver=999)


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

def test_resolve_known_coin_without_params(registry):
    assert registry.resolve("LTC").pubkey_ver == 0x30


def test_resolve_unknown_coin_without_params_returns_none(registry):
    assert registry.resolve("NOSUCHCOIN") is None


def test_resolve_unknown_coin_with_params_registers_it(registry):
    coin = registry.resolve("VOID", {"pubkey_ver": 0x41, "wif_ver": 0xC1})
    assert coin is not None
    assert coin.provenance == PROVENANCE_MANUAL
    assert registry.resolve("VOID").pubkey_ver == 0x41  # persisted


def test_resolve_params_override_a_known_coin(registry):
    coin = registry.resolve("BTC", {"pubkey_ver": 0x42, "wif_ver": 0xC2})
    assert coin.pubkey_ver == 0x42


def test_resolve_honours_explicit_node_provenance(registry):
    coin = registry.resolve("VOID", {
        "pubkey_ver": 0x41, "wif_ver": 0xC1, "provenance": PROVENANCE_NODE,
    })
    assert coin.provenance == PROVENANCE_NODE
    assert coin.verified is True


def test_resolve_ignores_params_without_pubkey_ver(registry):
    """wif_ver alone is not enough to define a coin; fall back to lookup."""
    assert registry.resolve("LTC", {"wif_ver": 0xB0}).pubkey_ver == 0x30
