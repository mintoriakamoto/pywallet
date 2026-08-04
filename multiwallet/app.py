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
        session_id = manager.load_wallet(ticker, dat_path, passphrase, label)
    except (ValueError, RuntimeError) as exc:
        return _json_err(str(exc))
    except Exception:
        logger.exception("Unexpected error loading wallet")
        return _json_err("Internal server error", 500)

    return _json_ok(session_id=session_id, ticker=ticker)


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
