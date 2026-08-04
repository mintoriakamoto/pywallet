"""
wallet_manager.py
~~~~~~~~~~~~~~~~~
WalletManager is the central object that tracks all loaded WalletSession
instances, grouped by coin ticker.

  manager = WalletManager.get_instance()

  # Load a BTC wallet
  session_id = manager.load_wallet('BTC', '/path/to/wallet.dat', passphrase='secret')

  # Get a change address from that session
  addr = manager.get_change_address('BTC', session_id)

  # Unload it
  manager.unload_wallet('BTC', session_id)
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from .coin_registry import CoinRegistry
from .wallet_session import WalletSession

logger = logging.getLogger(__name__)


class WalletManager:
    """
    Thread-safe manager that holds {ticker: [WalletSession, ...]} and
    delegates wallet operations to individual sessions.
    """

    _instance: Optional["WalletManager"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # ticker -> list of sessions
        self._sessions: Dict[str, List[WalletSession]] = {}
        self._registry = CoinRegistry.get_instance()

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------
    @classmethod
    def get_instance(cls) -> "WalletManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------
    # Load / unload
    # ------------------------------------------------------------------

    def load_wallet(
        self,
        ticker: str,
        dat_path: str,
        passphrase: Optional[str] = None,
        label: Optional[str] = None,
    ) -> str:
        """
        Load a wallet.dat for *ticker*.  Multiple wallet.dat files can be
        loaded for the same coin.

        Returns the session_id string on success.
        Raises ValueError if the coin is unknown or the file doesn't exist.
        """
        ticker = ticker.upper()
        coin_info = self._registry.get(ticker)
        if coin_info is None:
            raise ValueError("Unknown coin ticker: {}".format(ticker))

        import os
        if not os.path.isfile(dat_path):
            raise ValueError("wallet.dat not found: {}".format(dat_path))

        session = WalletSession(coin_info, dat_path, passphrase)
        if label:
            session.label = label

        ok = session.load()
        if not ok:
            raise RuntimeError(
                "Failed to load {}: {}".format(dat_path, session.error)
            )

        with self._lock:
            self._sessions.setdefault(ticker, []).append(session)

        logger.info(
            "WalletManager: loaded session %s for %s (%d keys)",
            session.session_id,
            ticker,
            len(session.keys),
        )
        return session.session_id

    def unload_wallet(self, ticker: str, session_id: str) -> bool:
        """
        Unload (and erase key material from) the session identified by
        *session_id*.  Returns True if found and removed.
        """
        ticker = ticker.upper()
        with self._lock:
            sessions = self._sessions.get(ticker, [])
            for i, sess in enumerate(sessions):
                if sess.session_id == session_id:
                    sess.unload()
                    sessions.pop(i)
                    if not sessions:
                        del self._sessions[ticker]
                    logger.info("WalletManager: unloaded session %s", session_id)
                    return True
        logger.warning("WalletManager: session not found: %s", session_id)
        return False

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_sessions(self, ticker: str) -> List[WalletSession]:
        ticker = ticker.upper()
        return list(self._sessions.get(ticker, []))

    def get_session(self, ticker: str, session_id: str) -> Optional[WalletSession]:
        for sess in self.get_sessions(ticker):
            if sess.session_id == session_id:
                return sess
        return None

    def list_coins(self) -> List[str]:
        """Return tickers that currently have at least one loaded wallet."""
        return list(self._sessions.keys())

    def get_all_addresses(self, ticker: str) -> List[str]:
        """Aggregate all addresses across every loaded wallet for *ticker*."""
        addrs: List[str] = []
        for sess in self.get_sessions(ticker):
            addrs.extend(sess.addresses)
        return addrs

    # ------------------------------------------------------------------
    # Change-address management
    # ------------------------------------------------------------------

    def get_change_address(
        self,
        ticker: str,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Get the next unused change address.

        If *session_id* is given, only that session is used.
        Otherwise the first session with a free change address is used.
        """
        ticker = ticker.upper()
        sessions = (
            [self.get_session(ticker, session_id)]
            if session_id
            else self.get_sessions(ticker)
        )
        for sess in sessions:
            if sess is None:
                continue
            addr = sess.get_change_address()
            if addr:
                return addr
        return None

    # ------------------------------------------------------------------
    # Serialisation helpers (used by the REST API)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a JSON-serialisable status summary."""
        coins: Dict[str, list] = {}
        for ticker, sessions in self._sessions.items():
            coins[ticker] = [s.to_dict() for s in sessions]
        return {"loaded_coins": coins}

    def coin_status(self, ticker: str) -> dict:
        ticker = ticker.upper()
        sessions = self.get_sessions(ticker)
        coin_info = self._registry.get(ticker)
        return {
            "ticker": ticker,
            "coin_info": coin_info.to_dict() if coin_info else None,
            "sessions": [s.to_dict() for s in sessions],
        }
