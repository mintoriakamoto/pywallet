"""
coin_registry.py
~~~~~~~~~~~~~~~~
Provides a CoinRegistry that merges a static table of Bitcoin-derived coin
network parameters with live data fetched from miningpoolstats.stream.

Static table covers everything needed to parse / produce wallet.dat addresses:
  (full_name, pubkey_version, wif_version, p2sh_version, bech32_hrp, bip44_index)

The miningpoolstats.stream API is queried once per process and the result is
cached in memory.  It enriches coins with algorithm, block-time, and hashrate
info that is shown in the UI but is not required for wallet operations.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Dict, Optional, Any

try:
    from urllib2 import urlopen, Request, URLError  # Python 2
except ImportError:
    from urllib.request import urlopen, Request  # Python 3
    from urllib.error import URLError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static coin table
# Format: TICKER -> (full_name, pubkey_ver, wif_ver, p2sh_ver, bech32_hrp, bip44_index)
# pubkey_ver  : version byte used when encoding a P2PKH address
# wif_ver     : version byte used when encoding a WIF private key
# p2sh_ver    : version byte for P2SH address (None if unknown/unsupported)
# bech32_hrp  : human-readable part for bech32/segwit addresses (None = no segwit)
# bip44_index : SLIP-0044 coin-type index (None = not registered)
# ---------------------------------------------------------------------------
STATIC_COIN_PARAMS: Dict[str, tuple] = {
    # ticker  full_name             pub    wif    p2sh   bech32  bip44
    'BTC':  ('Bitcoin',            0x00,  0x80,  0x05,  'bc',      0),
    'LTC':  ('Litecoin',           0x30,  0xb0,  0x32,  'ltc',     2),
    'DOGE': ('Dogecoin',           0x1e,  0x9e,  0x16,  None,      3),
    'DASH': ('Dash',               0x4c,  0xcc,  0x10,  None,      5),
    'VTC':  ('Vertcoin',           0x47,  0xc7,  0x05,  'vtc',    28),
    'BTG':  ('Bitcoin Gold',       0x26,  0xa6,  0x17,  'btg',   156),
    'RVN':  ('Ravencoin',          0x3c,  0x80,  0x7a,  None,    175),
    'DGB':  ('DigiByte',           0x1e,  0x9e,  0x3f,  'dgb',    20),
    'XVG':  ('Verge',              0x1e,  0x9e,  0x21,  None,     77),
    'MONA': ('Monacoin',           0x32,  0xb2,  0x05,  'mona',   22),
    'GRS':  ('Groestlcoin',        0x24,  0x80,  0x05,  'grs',    17),
    'FIRO': ('Firo',               0x52,  0xd2,  0x07,  None,    136),
    'XZC':  ('Zcoin',              0x52,  0xd2,  0x07,  None,    136),
    'BTX':  ('Bitcore',            0x03,  0x83,  0x08,  'btx',   160),
    'PIVX': ('PIVX',               0x1e,  0xd4,  0x0d,  None,     77),
    'KMD':  ('Komodo',             0x3c,  0xbc,  0x55,  None,    141),
    'VIA':  ('Viacoin',            0x47,  0xc7,  0x21,  'via',    14),
    'SYS':  ('Syscoin',            0x3f,  0xbf,  0x05,  'sys',    57),
    'QTUM': ('Qtum',               0x3a,  0x80,  0x32,  'qc',   2301),
    'RTM':  ('Raptoreum',          0x3c,  0xbc,  0x10,  None,   10226),
    'PPC':  ('Peercoin',           0x37,  0xb7,  0x75,  None,      6),
    'NMC':  ('Namecoin',           0x34,  0xb4,  0x0d,  None,      7),
    'NVC':  ('Novacoin',           0x08,  0x88,  0x14,  None,     50),
    'FTC':  ('Feathercoin',        0x0e,  0x8e,  0x05,  'fc',      8),
    'AUR':  ('Auroracoin',         0x17,  0x97,  0x05,  None,     85),
    'XPM':  ('Primecoin',          0x17,  0x97,  0x53,  None,     24),
    'FLO':  ('Florincoin',         0x23,  0xa3,  0x08,  None,    216),
    'RDD':  ('Reddcoin',           0x3d,  0xbd,  0x05,  None,      4),
    'EMC2': ('Einsteinium',        0x21,  0xa1,  0x05,  None,   None),
    'BLK':  ('Blackcoin',          0x19,  0x99,  0x55,  None,     10),
    'XMY':  ('Myriadcoin',         0x32,  0xb2,  0x09,  'my',     90),
    'PHR':  ('Phore',              0x37,  0xb7,  0x0d,  None,   None),
    'ZCL':  ('Zclassic',           0x1c,  0x80,  0x1c,  None,   None),
    'XSN':  ('Stakenet',           0x4c,  0xcc,  0x10,  None,   None),
    'MUE':  ('MonetaryUnit',       0x0f,  0x8f,  0x09,  None,   None),
    'ION':  ('ION',                0x49,  0xc9,  0x0d,  None,   None),
}


class CoinInfo:
    """Holds network parameters and live stats for a single coin."""

    def __init__(
        self,
        ticker: str,
        name: str,
        pubkey_ver: int,
        wif_ver: int,
        p2sh_ver: Optional[int],
        bech32_hrp: Optional[str],
        bip44_index: Optional[int],
        algorithm: str = "unknown",
        **extra: Any,
    ):
        self.ticker = ticker
        self.name = name
        self.pubkey_ver = pubkey_ver
        self.wif_ver = wif_ver
        self.p2sh_ver = p2sh_ver
        self.bech32_hrp = bech32_hrp
        self.bip44_index = bip44_index
        self.algorithm = algorithm
        self.extra = extra  # live stats from miningpoolstats

    def to_dict(self) -> dict:
        d = {
            "ticker": self.ticker,
            "name": self.name,
            "pubkey_ver": self.pubkey_ver,
            "wif_ver": self.wif_ver,
            "p2sh_ver": self.p2sh_ver,
            "bech32_hrp": self.bech32_hrp,
            "bip44_index": self.bip44_index,
            "algorithm": self.algorithm,
        }
        d.update(self.extra)
        return d

    def __repr__(self) -> str:
        return "<CoinInfo {ticker} pubkey=0x{pubkey_ver:02x} wif=0x{wif_ver:02x}>".format(
            **self.__dict__
        )


class CoinRegistry:
    """
    Singleton-style registry that combines static coin parameters with
    live data from miningpoolstats.stream.

    Usage::

        registry = CoinRegistry.get_instance()
        btc = registry.get('BTC')
        all_coins = registry.all()
    """

    _instance: Optional["CoinRegistry"] = None
    _lock = threading.Lock()

    MPS_URL = "https://miningpoolstats.stream/data/coins_data.js"
    CACHE_TTL = 3600  # seconds

    def __init__(self) -> None:
        self._coins: Dict[str, CoinInfo] = {}
        self._last_fetch: float = 0.0
        self._build_from_static()

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------
    @classmethod
    def get_instance(cls) -> "CoinRegistry":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(self, ticker: str) -> Optional[CoinInfo]:
        ticker = ticker.upper()
        self._maybe_refresh()
        return self._coins.get(ticker)

    def all(self) -> Dict[str, CoinInfo]:
        self._maybe_refresh()
        return dict(self._coins)

    def refresh(self) -> None:
        """Force a refresh from miningpoolstats.stream."""
        self._fetch_mps_data()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_from_static(self) -> None:
        for ticker, params in STATIC_COIN_PARAMS.items():
            name, pub, wif, p2sh, bech32, bip44 = params
            self._coins[ticker] = CoinInfo(
                ticker=ticker,
                name=name,
                pubkey_ver=pub,
                wif_ver=wif,
                p2sh_ver=p2sh,
                bech32_hrp=bech32,
                bip44_index=bip44,
            )

    def _maybe_refresh(self) -> None:
        if time.time() - self._last_fetch > self.CACHE_TTL:
            self._fetch_mps_data()

    def _fetch_mps_data(self) -> None:
        """
        Fetch coin list from miningpoolstats.stream and enrich known coins.
        The endpoint returns a JS assignment like:
            var all_data = {...};
        We strip the JS wrapper and parse the JSON payload.
        """
        try:
            req = Request(
                self.MPS_URL,
                headers={"User-Agent": "pywallet-multiwallet/1.0"},
            )
            response = urlopen(req, timeout=10)
            raw = response.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")

            # Strip JS variable assignment wrapper
            # Format:  var all_data = { ... };
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1 or end == 0:
                raise ValueError("Unexpected response format from miningpoolstats")
            data = json.loads(raw[start:end])

            self._apply_mps_data(data)
            self._last_fetch = time.time()
            logger.info("CoinRegistry: refreshed %d coins from miningpoolstats", len(data))

        except Exception as exc:  # network errors, parse errors, etc.
            logger.warning("CoinRegistry: could not fetch miningpoolstats data: %s", exc)
            self._last_fetch = time.time()  # avoid hammering on failure

    def _apply_mps_data(self, data: dict) -> None:
        """
        Merge live MPS data into the registry.
        Adds coins that are only known from MPS with minimal parameters.
        """
        for ticker, info in data.items():
            ticker = ticker.upper()
            algo = info.get("algo") or info.get("algorithm") or "unknown"
            extra = {
                "hashrate": info.get("hashrate"),
                "workers": info.get("workers"),
                "block_time": info.get("block_time"),
                "block_reward": info.get("block_reward"),
                "difficulty": info.get("difficulty"),
                "mps_name": info.get("name") or info.get("coin"),
            }
            if ticker in self._coins:
                self._coins[ticker].algorithm = algo
                self._coins[ticker].extra.update(extra)
            else:
                # Coin is on MPS but not in static table — add with defaults
                name = info.get("name") or info.get("coin") or ticker
                self._coins[ticker] = CoinInfo(
                    ticker=ticker,
                    name=name,
                    pubkey_ver=0x00,  # unknown; will use BTC defaults
                    wif_ver=0x80,
                    p2sh_ver=0x05,
                    bech32_hrp=None,
                    bip44_index=None,
                    algorithm=algo,
                    **extra,
                )
