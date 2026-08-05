"""
wallet_session.py
~~~~~~~~~~~~~~~~~
WalletSession wraps a single wallet.dat file for a specific coin.
It handles:
  - Parsing the Berkeley DB wallet.dat using pywallet's existing code
  - Address generation using coin-specific version bytes
  - Change-address pool management (marks used/unused)
  - Optional passphrase decryption for encrypted wallets
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import time
from typing import Dict, List, Optional, Tuple

from .coin_registry import CoinInfo, version_bytes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# We reuse crypto primitives from the existing pywallet.py.  We import them
# lazily so that the multiwallet package can be imported without BerkeleyDB
# being present on a dev machine.
# ---------------------------------------------------------------------------

def _load_pywallet():
    """Import pywallet module from the parent directory."""
    import importlib.util, sys, os
    parent = os.path.join(os.path.dirname(__file__), "..")
    pw_path = os.path.join(parent, "pywallet.py")
    spec = importlib.util.spec_from_file_location("pywallet_core", pw_path)
    mod = importlib.util.load_from_spec(spec) if hasattr(importlib.util, "load_from_spec") \
          else importlib.util.module_from_spec(spec)
    sys.modules.setdefault("pywallet_core", mod)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Minimal pure-Python b58check helpers so we do not depend on pywallet being
# importable at all times.
# ---------------------------------------------------------------------------
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    """Base-58 encode raw bytes (no checksum)."""
    n = int.from_bytes(data, "big")
    result = []
    while n:
        n, r = divmod(n, 58)
        result.append(_B58_ALPHABET[r:r+1])
    # preserve leading zero bytes
    for byte in data:
        if byte == 0:
            result.append(_B58_ALPHABET[0:1])
        else:
            break
    return b"".join(reversed(result)).decode("ascii")


def _b58check_encode(version: int, payload: bytes) -> str:
    """
    Encode version + payload with a 4-byte checksum into Base58Check.

    *version* may be wider than one byte — the Zcash-derived coins use two
    (ZEC 0x1cb8, ZEN 0x2089) — so the prefix is encoded at its natural width
    rather than assumed to be a single byte.
    """
    data = version_bytes(version) + payload
    chk = hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4]
    return _b58encode(data + chk)


def _hash160(data: bytes) -> bytes:
    """RIPEMD-160(SHA-256(data))."""
    sha = hashlib.sha256(data).digest()
    h = hashlib.new("ripemd160")
    h.update(sha)
    return h.digest()


def _pubkey_to_address(pubkey_bytes: bytes, pubkey_ver: int) -> str:
    """Convert a raw compressed/uncompressed public key to a P2PKH address."""
    return _b58check_encode(pubkey_ver, _hash160(pubkey_bytes))


# ---------------------------------------------------------------------------
# WalletKey  (one entry extracted from wallet.dat)
# ---------------------------------------------------------------------------

class WalletKey:
    """Represents a single key-pair read from a wallet.dat."""

    def __init__(
        self,
        pubkey: bytes,
        privkey_wif: str,
        address: str,
        label: str = "",
        is_change: bool = False,
        used: bool = False,
    ):
        self.pubkey = pubkey
        self.privkey_wif = privkey_wif  # WIF-encoded; may be '' if wallet locked
        self.address = address
        self.label = label
        self.is_change = is_change      # True if address is flagged as change in the DB
        self.used = used                # True once we hand it out as a change address

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "label": self.label,
            "is_change": self.is_change,
            "used": self.used,
            "has_privkey": bool(self.privkey_wif),
        }


# ---------------------------------------------------------------------------
# WalletSession
# ---------------------------------------------------------------------------

class WalletSession:
    """
    Represents one loaded wallet.dat file for a specific coin.

    Parameters
    ----------
    coin_info : CoinInfo
        Coin network parameters used for address encoding.
    dat_path : str
        Absolute path to the wallet.dat file.
    passphrase : str, optional
        Wallet encryption passphrase.  Leave None for unencrypted wallets.
    """

    def __init__(
        self,
        coin_info: CoinInfo,
        dat_path: str,
        passphrase: Optional[str] = None,
    ):
        self.coin_info = coin_info
        self.dat_path = os.path.abspath(dat_path)
        self.passphrase = passphrase
        self.session_id: str = "{}-{}-{}".format(
            coin_info.ticker,
            os.path.basename(dat_path),
            int(time.time()),
        )
        self._keys: List[WalletKey] = []
        self._loaded: bool = False
        self._label: str = os.path.basename(dat_path)  # display name
        self._error: Optional[str] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, value: str) -> None:
        self._label = value

    @property
    def keys(self) -> List[WalletKey]:
        return list(self._keys)

    @property
    def addresses(self) -> List[str]:
        return [k.address for k in self._keys]

    @property
    def error(self) -> Optional[str]:
        return self._error

    # ------------------------------------------------------------------
    # Load / unload
    # ------------------------------------------------------------------

    def load(self) -> bool:
        """
        Parse the wallet.dat and populate self._keys.
        Returns True on success.
        """
        self._error = None
        try:
            self._keys = self._parse_wallet_dat()
            self._loaded = True
            logger.info(
                "WalletSession: loaded %s (%d keys) for %s",
                self._label,
                len(self._keys),
                self.coin_info.ticker,
            )
            return True
        except Exception as exc:
            self._error = str(exc)
            self._loaded = False
            logger.error("WalletSession: failed to load %s: %s", self._label, exc)
            return False

    def unload(self) -> None:
        """Release all key material from memory."""
        self._keys = []
        self._loaded = False
        logger.info("WalletSession: unloaded %s", self._label)

    # ------------------------------------------------------------------
    # Change-address management
    # ------------------------------------------------------------------

    def get_change_address(self) -> Optional[str]:
        """
        Return the next unused change address from the key pool.
        Marks the address as used so it won't be reused.
        Returns None if no unused change addresses remain.
        """
        # Prefer keys explicitly flagged as change addresses
        for key in self._keys:
            if key.is_change and not key.used:
                key.used = True
                logger.debug("Change address (flagged): %s", key.address)
                return key.address
        # Fall back to any unused address
        for key in self._keys:
            if not key.used and not key.is_change:
                key.used = True
                logger.debug("Change address (fallback): %s", key.address)
                return key.address
        logger.warning("WalletSession: no unused change addresses in %s", self._label)
        return None

    def mark_address_used(self, address: str) -> None:
        for key in self._keys:
            if key.address == address:
                key.used = True
                return

    def reset_change_addresses(self) -> None:
        """Mark all change addresses as unused (for testing / re-use)."""
        for key in self._keys:
            key.used = False

    def get_change_address_pool(self) -> List[dict]:
        """Return status of all keys in the pool."""
        return [k.to_dict() for k in self._keys]

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "coin": self.coin_info.ticker,
            "label": self._label,
            "dat_path": self.dat_path,
            "loaded": self._loaded,
            "key_count": len(self._keys),
            "error": self._error,
            "addresses": self.addresses,
            "change_pool": self.get_change_address_pool(),
        }

    # ------------------------------------------------------------------
    # wallet.dat parsing (BerkeleyDB)
    # ------------------------------------------------------------------

    def _parse_wallet_dat(self) -> List[WalletKey]:
        """
        Open the wallet.dat with BerkeleyDB and extract all key pairs.
        Uses pywallet's existing parsing logic where available, otherwise
        falls back to a minimal pure-Python reader.
        """
        try:
            return self._parse_via_pywallet()
        except ImportError:
            logger.warning("pywallet_core not importable; using fallback parser")
            return self._parse_minimal()
        except Exception as exc:
            logger.warning("pywallet_core parse failed (%s); trying fallback", exc)
            return self._parse_minimal()

    def _parse_via_pywallet(self) -> List[WalletKey]:
        """Use pywallet's proven wallet.dat reading code."""
        import sys, os
        # Make the parent directory importable
        parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if parent not in sys.path:
            sys.path.insert(0, parent)

        # Override the global address version bytes that pywallet uses
        import importlib
        try:
            pw = importlib.import_module("pywallet_core")
        except ModuleNotFoundError:
            # Load directly
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "pywallet_core",
                os.path.join(parent, "pywallet.py"),
            )
            pw = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pw)

        # Patch global version bytes to match this coin
        original_addrtype = pw.addrtype
        original_wif = pw.wif_prefix
        pw.addrtype = self.coin_info.pubkey_ver
        pw.wif_prefix = self.coin_info.wif_ver

        try:
            db_dir = os.path.dirname(self.dat_path)
            db_filename = os.path.basename(self.dat_path)
            db_env = pw.create_env(db_dir)
            local_json: dict = {}
            pw.read_wallet(local_json, db_env, True, False, "", db_filename)
        finally:
            pw.addrtype = original_addrtype
            pw.wif_prefix = original_wif

        return self._convert_pywallet_json(local_json)

    def _convert_pywallet_json(self, json_db: dict) -> List[WalletKey]:
        """Convert pywallet's json_db output into WalletKey objects."""
        keys: List[WalletKey] = []
        change_set = set(json_db.get("change_addresses", []))
        for entry in json_db.get("keys", []):
            addr = entry.get("addr", "")
            wif = entry.get("secret", "") or entry.get("wif", "")
            pubkey_hex = entry.get("pubkey", "")
            pubkey_bytes = bytes.fromhex(pubkey_hex) if pubkey_hex else b""
            label = entry.get("label", "")
            is_change = addr in change_set or entry.get("change", False)
            keys.append(WalletKey(
                pubkey=pubkey_bytes,
                privkey_wif=wif,
                address=addr,
                label=label,
                is_change=is_change,
            ))
        return keys

    def _parse_minimal(self) -> List[WalletKey]:
        """
        Minimal wallet.dat reader using bsddb3 directly.
        Only extracts public keys and derives addresses; does not decrypt.
        """
        try:
            import bsddb3
            db = bsddb3.btopen(self.dat_path, "r")
        except ImportError:
            try:
                from bsddb import db as _bsddb
                env = _bsddb.DBEnv()
                env.open(
                    os.path.dirname(self.dat_path),
                    _bsddb.DB_CREATE | _bsddb.DB_INIT_MPOOL,
                )
                database = _bsddb.DB(env)
                database.open(
                    self.dat_path, "main", _bsddb.DB_BTREE,
                    _bsddb.DB_RDONLY,
                )
                db = database
            except Exception as e:
                raise RuntimeError(
                    "BerkeleyDB not available.  Install bsddb3: pip install bsddb3. ({})".format(e)
                )

        keys: List[WalletKey] = []
        try:
            item = db.first()
            while item:
                key_raw, value_raw = item
                try:
                    if b"key" in key_raw:
                        pubkey = self._extract_pubkey(key_raw, value_raw)
                        if pubkey:
                            addr = _pubkey_to_address(pubkey, self.coin_info.pubkey_ver)
                            keys.append(WalletKey(
                                pubkey=pubkey,
                                privkey_wif="",
                                address=addr,
                            ))
                except Exception:
                    pass
                try:
                    item = db.next()
                except Exception:
                    break
        finally:
            db.close()

        return keys

    def _extract_pubkey(self, key_raw: bytes, value_raw: bytes) -> Optional[bytes]:
        """
        Best-effort public key extraction from a raw BDB key/value pair.
        The key record type is encoded as a variable-length string at the
        start of key_raw; the public key follows.
        """
        try:
            # skip the varint length and type string
            if len(key_raw) < 2:
                return None
            type_len = key_raw[0]
            if isinstance(type_len, int):
                tlen = type_len
            else:
                tlen = ord(type_len)
            rec_type = key_raw[1:1 + tlen]
            if rec_type not in (b"key", b"wkey"):
                return None
            pubkey_len_offset = 1 + tlen
            plen_byte = key_raw[pubkey_len_offset]
            if isinstance(plen_byte, int):
                plen = plen_byte
            else:
                plen = ord(plen_byte)
            pubkey = key_raw[pubkey_len_offset + 1: pubkey_len_offset + 1 + plen]
            if len(pubkey) in (33, 65):
                return pubkey
        except (IndexError, struct.error):
            pass
        return None
