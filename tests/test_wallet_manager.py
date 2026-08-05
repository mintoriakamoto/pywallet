"""
test_wallet_manager
~~~~~~~~~~~~~~~~~~~
Tests that an unknown coin can be loaded when parameters are supplied.

No real wallet.dat is needed: load_wallet resolves the coin *before* it touches
the filesystem, so the coin-resolution gate can be tested on its own by
checking which error a missing file produces.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multiwallet.coin_registry import CoinRegistry  # noqa: E402
from multiwallet.wallet_manager import WalletManager  # noqa: E402


@pytest.fixture
def manager():
    """A standalone manager backed by an offline registry."""
    CoinRegistry.get_instance()._last_fetch = time.time()  # suppress MPS fetch
    return WalletManager()


MISSING = "/nonexistent/wallet.dat"


def test_unknown_coin_without_params_is_rejected(manager):
    with pytest.raises(ValueError, match="Unknown coin ticker"):
        manager.load_wallet("NOSUCHCOIN", MISSING)


def test_unknown_coin_with_params_passes_the_coin_gate(manager):
    """
    The coin is no longer the blocker — resolution succeeds and the load gets
    as far as the missing file.
    """
    with pytest.raises(ValueError, match="not found"):
        manager.load_wallet("VOID", MISSING, params={"pubkey_ver": 0x41})


def test_params_without_pubkey_ver_do_not_open_the_gate(manager):
    with pytest.raises(ValueError, match="Unknown coin ticker"):
        manager.load_wallet("VOID2", MISSING, params={"wif_ver": 0xC1})


def test_bad_version_byte_is_rejected(manager):
    with pytest.raises(ValueError):
        manager.load_wallet("VOID3", MISSING, params={"pubkey_ver": 999})


def test_known_coin_still_loads_without_params(manager):
    with pytest.raises(ValueError, match="not found"):
        manager.load_wallet("BTC", MISSING)


def test_registering_via_load_persists_the_coin(manager):
    with pytest.raises(ValueError):
        manager.load_wallet(
            "VOID4", MISSING,
            params={"pubkey_ver": 0x41, "wif_ver": 0xC1, "name": "Voidcoin"},
        )
    coin = CoinRegistry.get_instance().get("VOID4")
    assert coin is not None
    assert coin.name == "Voidcoin"
    assert coin.verified is True


# ---------------------------------------------------------------------------
# HTTP layer — skipped when flask is absent
# ---------------------------------------------------------------------------

def test_parse_coin_params_accepts_hex_strings():
    pytest.importorskip("flask")
    from multiwallet.app import _parse_coin_params
    assert _parse_coin_params({"pubkey_ver": "0x3c"})["pubkey_ver"] == 0x3C


def test_parse_coin_params_empty_when_nothing_supplied():
    pytest.importorskip("flask")
    from multiwallet.app import _parse_coin_params
    assert _parse_coin_params({"ticker": "BTC", "dat_path": "/x"}) == {}


def test_parse_coin_params_requires_pubkey_ver():
    pytest.importorskip("flask")
    from multiwallet.app import _parse_coin_params
    with pytest.raises(ValueError, match="pubkey_ver is required"):
        _parse_coin_params({"wif_ver": "0x80"})


def test_parse_coin_params_rejects_booleans():
    pytest.importorskip("flask")
    from multiwallet.app import _parse_coin_params
    with pytest.raises(ValueError):
        _parse_coin_params({"pubkey_ver": True})
