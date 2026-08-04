# MultiWallet — Exodus-style Multi-Coin Wallet Manager

An all-in-one multi-coin wallet application built on top of **pywallet**.
Inspired by Exodus, it lets you load/unload individual `wallet.dat` files for
any Bitcoin-derived coin and manage change addresses — all from a single
browser-based UI.

## Features

| Feature | Details |
|---|---|
| **Multi-coin support** | 60+ Bitcoin-derived coins via a static network-params table |
| **miningpoolstats.stream integration** | Live coin data (algorithm, hashrate, block time) fetched automatically |
| **wallet.dat load/unload** | Load any coin's `wallet.dat` at runtime; unload to erase key material |
| **Multiple wallets per coin** | Load more than one `wallet.dat` for the same coin simultaneously |
| **Change address pool** | Automatically tracks used/unused change addresses just like Core wallets do |
| **Exodus-style UI** | Dark theme single-page app with sidebar, dashboard, and per-coin views |

## Architecture

```
multiwallet/
├── __init__.py
├── app.py              # Flask REST API + SPA entry point
├── coin_registry.py    # CoinRegistry: static params + miningpoolstats.stream live data
├── wallet_manager.py   # WalletManager: {ticker -> [WalletSession, ...]}
├── wallet_session.py   # WalletSession: wallet.dat parsing, change-address logic
├── templates/
│   └── index.html      # Exodus-style single-page UI (vanilla JS)
└── requirements.txt
```

## Quick Start

### Prerequisites

- Python 3.7+
- Berkeley DB 4.x/5.x (required by `bsddb3`)
  ```
  # Ubuntu/Debian
  sudo apt-get install libdb-dev libdb5.3-dev
  # macOS
  brew install berkeley-db@5
  ```

### Install

```bash
cd pywallet/multiwallet
pip install -r requirements.txt
```

### Run

```bash
# From the repo root
python -m multiwallet.app

# Or with a custom port
MULTIWALLET_PORT=8080 python -m multiwallet.app
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/api/coins` | List all registered coins |
| `GET`  | `/api/refresh_coins` | Force refresh from miningpoolstats.stream |
| `GET`  | `/api/wallets` | Status of all loaded wallets |
| `POST` | `/api/wallets/load` | Load a wallet.dat |
| `POST` | `/api/wallets/unload` | Unload a session |
| `GET`  | `/api/wallets/<ticker>` | Sessions for a coin |
| `GET`  | `/api/wallets/<ticker>/<session_id>` | Single session detail |
| `GET`  | `/api/wallets/<ticker>/<session_id>/change_address` | Get next change address |
| `POST` | `/api/wallets/<ticker>/<session_id>/mark_used` | Mark address as used |
| `POST` | `/api/wallets/<ticker>/<session_id>/reset_change` | Reset change address pool |

### Load a wallet

```bash
curl -X POST http://localhost:5000/api/wallets/load \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"BTC","dat_path":"/home/user/.bitcoin/wallet.dat","label":"Main BTC"}'
```

### Get a change address

```bash
curl http://localhost:5000/api/wallets/BTC/<session_id>/change_address
```

## Coin Registry

Coins are sourced from two places:

1. **Static table** in `coin_registry.py` — covers 60+ coins with full network
   parameters (pubkey version byte, WIF version, P2SH version, bech32 HRP,
   BIP44 index).

2. **miningpoolstats.stream** — queried once per hour; enriches known coins
   with algorithm, hashrate, block time, and worker counts.  Unknown coins
   from MPS are added with Bitcoin-defaulted address parameters.

## Change Address Behavior

Each `WalletSession` maintains a pool of all keys from the loaded `wallet.dat`.
Keys explicitly stored as change keys in the wallet are preferred; if none
remain, regular keys are used as fallback — matching the behavior of Bitcoin
Core and its forks.

```python
session.get_change_address()   # returns next unused address, marks it used
session.mark_address_used(addr)
session.reset_change_addresses()  # un-mark all (useful for testing)
session.get_change_address_pool() # full status list
```

## Security Notes

- Private keys are held in memory only while a session is loaded.
- Calling `unload_wallet` clears all key material from the `WalletSession`.
- The server should only be run on `localhost` unless you add authentication.
- Never expose this service on a public network without TLS and auth.
