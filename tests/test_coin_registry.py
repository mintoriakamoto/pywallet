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
    STATIC_COIN_PARAMS,
    CoinInfo,
    CoinRegistry,
    version_bytes,
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


@pytest.mark.parametrize("bad", [-1, 0x10000, 0xFFFFF])
def test_register_rejects_out_of_range_bytes(registry, bad):
    with pytest.raises(ValueError):
        registry.register("BAD", pubkey_ver=bad)


def test_register_rejects_out_of_range_wif(registry):
    with pytest.raises(ValueError):
        registry.register("BAD", pubkey_ver=0x10, wif_ver=0x10000)


def test_register_accepts_two_byte_prefix(registry):
    """ZEC-style prefixes must survive registration."""
    coin = registry.register("ZEC2", pubkey_ver=0x1CB8, wif_ver=0x80, p2sh_ver=0x1CBD)
    assert coin.pubkey_version_bytes == b"\x1c\xb8"


# ---------------------------------------------------------------------------
# version_bytes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,expected", [
    (0x00, b"\x00"),      # BTC — still one byte
    (0x30, b"\x30"),      # LTC
    (0xFF, b"\xff"),      # widest single byte
    (0x1CB8, b"\x1c\xb8"),  # ZEC
    (0x2089, b"\x20\x89"),  # ZEN
])
def test_version_bytes_uses_natural_width(version, expected):
    assert version_bytes(version) == expected


def test_version_bytes_rejects_negative():
    with pytest.raises(ValueError):
        version_bytes(-1)


# ---------------------------------------------------------------------------
# the generated table
# ---------------------------------------------------------------------------

def test_generated_table_is_loaded(registry):
    """coins.json should be found and preferred over the embedded table."""
    assert len(registry.all()) > len(STATIC_COIN_PARAMS)


@pytest.mark.parametrize("ticker,pubkey_ver,wif_ver", [
    ("BTC", 0x00, 0x80),
    ("LTC", 0x30, 0xb0),
    ("DOGE", 0x1e, 0x9e),   # hdwallet had testnet 0xf1; we keep 0x9e
    ("NMC", 0x34, 0xb4),    # hdwallet had 0x80; coininfo agrees with us
])
def test_known_good_values_survive_the_merge(registry, ticker, pubkey_ver, wif_ver):
    coin = registry.get(ticker)
    assert (coin.pubkey_ver, coin.wif_ver) == (pubkey_ver, wif_ver)


@pytest.mark.parametrize("ticker,wif_ver", [
    ("BTG", 0x80),   # was 0xa6
    ("DGB", 0x80),   # was 0x9e
    ("MONA", 0xb0),  # was 0xb2
    ("VTC", 0x80),   # was 0xc7
])
def test_majority_corrections_applied(registry, ticker, wif_ver):
    """Four WIF bytes that two independent sources agreed we had wrong."""
    assert registry.get(ticker).wif_ver == wif_ver


@pytest.mark.parametrize("ticker,wif_ver", [
    ("BTX", 0x80),  # was 0x83
    ("LBC", 0x1c),  # was 0xd5
    ("LCC", 0xb0),  # was 0x9c
    ("PHR", 0xd4),  # was 0xb7
    ("SYS", 0x80),  # was 0xbf
    ("NLG", 0xa6),  # ours confirmed: Gulden literally writes (1, 38+128)
])
def test_disputes_settled_against_chainparams(registry, ticker, wif_ver):
    """
    Every previously disputed coin was resolved by reading its own
    chainparams.cpp, so none should remain flagged.
    """
    coin = registry.get(ticker)
    assert coin.wif_ver == wif_ver
    assert coin.disputed is False
    assert coin.source_verified is True
    assert coin.verified is True


def test_no_coin_is_left_disputed(registry):
    assert [t for t, c in registry.all().items() if c.disputed] == []


@pytest.mark.parametrize("ticker", ["BTC", "LTC", "DOGE", "ZEC", "ZEN", "VTC"])
def test_source_verified_flag_is_exposed(registry, ticker):
    assert registry.get(ticker).to_dict()["source_verified"] is True


def test_disputed_coin_would_be_unverified():
    """The disputed machinery still works even though no coin uses it now."""
    coin = CoinInfo(
        ticker="X", name="X", pubkey_ver=0x30, wif_ver=0xB0, p2sh_ver=None,
        bech32_hrp=None, bip44_index=None, disputed=True,
        wif_alternatives=[0x80],
    )
    assert coin.verified is False
    assert coin.wif_candidates == [0xB0, 0x80]


@pytest.mark.parametrize("ticker,pubkey_ver", [
    ("ZEC", 0x1cb8),
    ("ZEN", 0x2089),
    ("FLUX", 0x1cb8),
    ("BTCZ", 0x1cb8),
])
def test_two_byte_prefix_coins_are_present(registry, ticker, pubkey_ver):
    """Previously unrepresentable — the single-byte schema could not hold them."""
    coin = registry.get(ticker)
    assert coin.pubkey_ver == pubkey_ver
    assert len(coin.pubkey_version_bytes) == 2
    assert coin.to_dict()["multibyte_prefix"] is True


def test_single_byte_coins_not_marked_multibyte(registry):
    assert registry.get("BTC").to_dict()["multibyte_prefix"] is False


def test_every_coin_has_usable_parameters(registry):
    for ticker, coin in registry.all().items():
        assert isinstance(coin.pubkey_ver, int), ticker
        assert 0 <= coin.pubkey_ver <= 0xFFFF, ticker
        assert isinstance(coin.wif_ver, int), ticker
        assert 0 <= coin.wif_ver <= 0xFFFF, ticker
        assert coin.name, ticker


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
