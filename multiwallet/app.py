"""
app.py
~~~~~~
Flask REST API + single-page HTML UI for the multi-coin wallet.

Run:
    pip install flask
    python -m multiwallet.app            # runs on http://localhost:5000

Endpoints:
    GET  /                                   -> UI
    GET  /api/coins                          -> all registered coins
    GET  /api/wallets                        -> status of all loaded wallets
    POST /api/wallets/load                   -> load a wallet.dat
         body: { ticker, dat_path, passphrase?, label? }
    POST /api/wallets/unload                 -> unload a session
         body: { ticker, session_id }
    GET  /api/wallets/<ticker>               -> sessions for a coin
    GET  /api/wallets/<ticker>/<session_id>  -> single session detail
    GET  /api/wallets/<ticker>/<session_id>/change_address
                                             -> get next change address
    POST /api/wallets/<ticker>/<session_id>/mark_used
         body: { address }                  -> mark address used
    POST /api/wallets/<ticker>/<session_id>/reset_change
                                             -> reset all change addresses
    GET  /api/refresh_coins                  -> force miningpoolstats refresh
"""

from __future__ import annotations

import json
import logging
import os

from flask import Flask, jsonify, request, render_template, abort

from .coin_registry import CoinRegistry
from .wallet_manager import WalletManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(_HERE, "templates"),
    static_folder=os.path.join(_HERE, "static"),
)


def _json_ok(**kwargs):
    kwargs.setdefault("ok", True)
    return jsonify(kwargs)


def _json_err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _parse_version_byte(value) -> int:
    """
    Coerce a JSON-supplied version byte to an int.

    Accepts an int or a string in decimal or ``0x``-prefixed hex, since the UI
    and curl users naturally write ``"0x3c"``.  Raises ValueError on anything
    else; callers map that to a 400.
    """
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        raise ValueError("version byte must be a number, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value.strip(), 0)
    raise ValueError("version byte must be an int or a numeric string")


def _parse_coin_params(data: dict) -> dict:
    """
    Extract optional network parameters from a load request body.

    Returns {} when none were supplied, so a known ticker keeps its table
    entry.  Raises ValueError if the caller sent something unusable.
    """
    keys = ("pubkey_ver", "wif_ver", "p2sh_ver", "bech32_hrp", "name")
    if not any(data.get(k) is not None for k in keys):
        return {}

    params: dict = {}
    for key in ("pubkey_ver", "wif_ver", "p2sh_ver"):
        if data.get(key) is not None:
            params[key] = _parse_version_byte(data[key])
    for key in ("bech32_hrp", "name"):
        value = data.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("{} must be a string".format(key))
            params[key] = value.strip() or None

    if "pubkey_ver" not in params:
        raise ValueError("pubkey_ver is required when supplying coin params")
    return params


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Coins
# ---------------------------------------------------------------------------

@app.route("/api/coins")
def list_coins():
    registry = CoinRegistry.get_instance()
    coins = {t: c.to_dict() for t, c in registry.all().items()}
    return jsonify({"ok": True, "coins": coins})


@app.route("/api/refresh_coins")
def refresh_coins():
    CoinRegistry.get_instance().refresh()
    return _json_ok(message="Coin registry refreshed")


# ---------------------------------------------------------------------------
# Wallets — global status
# ---------------------------------------------------------------------------

@app.route("/api/wallets")
def wallets_status():
    manager = WalletManager.get_instance()
    return jsonify({"ok": True, **manager.status()})


# ---------------------------------------------------------------------------
# Load wallet
# ---------------------------------------------------------------------------

@app.route("/api/wallets/load", methods=["POST"])
def load_wallet():
    data = request.get_json(force=True, silent=True) or {}
    ticker = data.get("ticker", "").strip().upper()
    dat_path = data.get("dat_path", "").strip()
    passphrase = data.get("passphrase") or None
    label = data.get("label") or None

    if not ticker:
        return _json_err("ticker is required")
    if not dat_path:
        return _json_err("dat_path is required")

    manager = WalletManager.get_instance()
    try:
        params = _parse_coin_params(data)
        session_id = manager.load_wallet(ticker, dat_path, passphrase, label, params)
    except ValueError as exc:
        logger.warning("load_wallet validation error: %s", exc)
        return _json_err(
            "Invalid request. Check ticker, dat_path, and any coin params."
        )
    except RuntimeError as exc:
        logger.warning("load_wallet runtime error: %s", exc)
        return _json_err("Could not load wallet. Check the path, coin, and passphrase.")
    except Exception:
        logger.exception("Unexpected error loading wallet")
        return _json_err("Internal server error", 500)

    # Surface parameter provenance so the caller can tell a verified coin from
    # a guessed one before trusting any address or exported WIF.
    coin = CoinRegistry.get_instance().get(ticker)
    return _json_ok(
        session_id=session_id,
        ticker=ticker,
        coin=coin.to_dict() if coin else None,
    )


# ---------------------------------------------------------------------------
# Unload wallet
# ---------------------------------------------------------------------------

@app.route("/api/wallets/unload", methods=["POST"])
def unload_wallet():
    data = request.get_json(force=True, silent=True) or {}
    ticker = data.get("ticker", "").strip().upper()
    session_id = data.get("session_id", "").strip()

    if not ticker or not session_id:
        return _json_err("ticker and session_id are required")

    manager = WalletManager.get_instance()
    removed = manager.unload_wallet(ticker, session_id)
    if not removed:
        return _json_err("Session not found", 404)
    return _json_ok(message="Session unloaded")


# ---------------------------------------------------------------------------
# Per-coin wallet listing
# ---------------------------------------------------------------------------

@app.route("/api/wallets/<ticker>")
def coin_wallets(ticker: str):
    manager = WalletManager.get_instance()
    return jsonify({"ok": True, **manager.coin_status(ticker.upper())})


# ---------------------------------------------------------------------------
# Single session detail
# ---------------------------------------------------------------------------

@app.route("/api/wallets/<ticker>/<session_id>")
def session_detail(ticker: str, session_id: str):
    manager = WalletManager.get_instance()
    sess = manager.get_session(ticker.upper(), session_id)
    if sess is None:
        return _json_err("Session not found", 404)
    return jsonify({"ok": True, "session": sess.to_dict()})


# ---------------------------------------------------------------------------
# Change-address endpoints
# ---------------------------------------------------------------------------

@app.route("/api/wallets/<ticker>/<session_id>/change_address")
def get_change_address(ticker: str, session_id: str):
    manager = WalletManager.get_instance()
    sess = manager.get_session(ticker.upper(), session_id)
    if sess is None:
        return _json_err("Session not found", 404)
    addr = sess.get_change_address()
    if addr is None:
        return _json_err("No unused change addresses available", 409)
    return _json_ok(address=addr)


@app.route("/api/wallets/<ticker>/<session_id>/mark_used", methods=["POST"])
def mark_used(ticker: str, session_id: str):
    manager = WalletManager.get_instance()
    sess = manager.get_session(ticker.upper(), session_id)
    if sess is None:
        return _json_err("Session not found", 404)
    data = request.get_json(force=True, silent=True) or {}
    address = data.get("address", "").strip()
    if not address:
        return _json_err("address is required")
    sess.mark_address_used(address)
    return _json_ok(message="Address marked as used")


@app.route("/api/wallets/<ticker>/<session_id>/reset_change", methods=["POST"])
def reset_change(ticker: str, session_id: str):
    manager = WalletManager.get_instance()
    sess = manager.get_session(ticker.upper(), session_id)
    if sess is None:
        return _json_err("Session not found", 404)
    sess.reset_change_addresses()
    return _json_ok(message="Change addresses reset")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("MULTIWALLET_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
