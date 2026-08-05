"""
test_addresses
~~~~~~~~~~~~~~
Golden address vectors, including the two-byte-prefix coins.

All vectors derive from the secp256k1 generator point — the compressed public
key for private key 1 — so they can be checked against any external tool.
Its hash160 is 751e76e8199196d454941c45d1b3a323f1433bd6, and the Bitcoin
address below is the widely published one for that key.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multiwallet.coin_registry import CoinRegistry  # noqa: E402
from multiwallet.wallet_session import (  # noqa: E402
    _b58check_encode,
    _hash160,
    _pubkey_to_address,
)

# Compressed pubkey for privkey = 1 (the secp256k1 generator point).
GENERATOR_PUBKEY = bytes.fromhex(
    "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
)
GENERATOR_HASH160 = "751e76e8199196d454941c45d1b3a323f1433bd6"


@pytest.fixture
def registry():
    reg = CoinRegistry()
    reg._last_fetch = time.time()  # suppress the MPS fetch
    return reg


def test_hash160_matches_published_value():
    assert _hash160(GENERATOR_PUBKEY).hex() == GENERATOR_HASH160


def test_bitcoin_address_matches_published_value():
    """Anchors the whole encoder against a well-known vector."""
    assert _pubkey_to_address(GENERATOR_PUBKEY, 0x00) == \
        "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"


# ---------------------------------------------------------------------------
# Per-coin vectors.  The prefix assertions are the real check: they encode the
# human-visible convention each chain is known for.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker,prefix,address", [
    ("BTC",  "1",  "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"),
    ("LTC",  "L",  "LVuDpNCSSj6pQ7t9Pv6d6sUkLKoqDEVUnJ"),
    ("DOGE", "D",  "DFpN6QqFfUm3gKNaxN6tNcab1FArL9cZLE"),
])
def test_single_byte_prefix_coins(registry, ticker, prefix, address):
    coin = registry.get(ticker)
    result = _pubkey_to_address(GENERATOR_PUBKEY, coin.pubkey_ver)
    assert result == address
    assert result.startswith(prefix)


@pytest.mark.parametrize("ticker,prefix,address", [
    ("ZEC",  "t1", "t1UYsZVJkLPeMjxEtACvSxfWuNmddpWfxzs"),
    ("ZCL",  "t1", "t1UYsZVJkLPeMjxEtACvSxfWuNmddpWfxzs"),
    ("FLUX", "t1", "t1UYsZVJkLPeMjxEtACvSxfWuNmddpWfxzs"),
    ("BTCZ", "t1", "t1UYsZVJkLPeMjxEtACvSxfWuNmddpWfxzs"),
    ("ZEN",  "zn", "znbmBYaXE1eNNkRTX56LpjASXHcMERfcigj"),
    ("YEC",  "s1", "s1Xt1mrPG6QvByYAQdnxZ8cKck8XoTt3xBY"),
    ("LTZ",  "L1", "L1JRPTaJj5C4b9W5GFtRG2Gvo1n8EGatNiz"),
    ("ANON", "An", "AnYEktGAqUSEAGTn8jKkhZeCFZNibUjnYxN"),
])
def test_two_byte_prefix_coins(registry, ticker, prefix, address):
    """
    These were unrepresentable before: the encoder assumed a single version
    byte, so every one of them produced a wrong address.
    """
    coin = registry.get(ticker)
    assert len(coin.pubkey_version_bytes) == 2
    result = _pubkey_to_address(GENERATOR_PUBKEY, coin.pubkey_ver)
    assert result == address
    assert result.startswith(prefix)


def test_zcash_family_shares_a_prefix(registry):
    """ZEC, ZCL, FLUX and BTCZ all use 0x1cb8 — same key, same address."""
    tickers = ("ZEC", "ZCL", "FLUX", "BTCZ")
    addrs = {_pubkey_to_address(GENERATOR_PUBKEY, registry.get(t).pubkey_ver)
             for t in tickers}
    assert len(addrs) == 1


def test_two_byte_encoding_is_not_truncated():
    """A naive bytes([version]) would raise or silently drop the high byte."""
    assert _b58check_encode(0x1CB8, bytes.fromhex(GENERATOR_HASH160)) != \
        _b58check_encode(0xB8, bytes.fromhex(GENERATOR_HASH160))


def test_every_registered_coin_encodes(registry):
    """No coin in the table may crash the encoder."""
    for ticker, coin in registry.all().items():
        addr = _pubkey_to_address(GENERATOR_PUBKEY, coin.pubkey_ver)
        assert addr and addr.isalnum(), ticker
