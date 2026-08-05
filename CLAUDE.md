# CLAUDE.md

Guidance for AI assistants working in this repository.

## What this repo is

Two loosely-coupled pieces of software that both read Bitcoin-Core-style
Berkeley DB `wallet.dat` files:

| Component | Path | Language | Purpose |
|---|---|---|---|
| **pywallet** | `pywallet.py` (single ~1900-line file) | **Python 2 only** | CLI wallet dumper / private-key importer. Fork of joric/pywallet 1.2.3, public domain. |
| **multiwallet** | `multiwallet/` | **Python 3 only** | Flask REST API + single-page "Exodus-style" UI for loading many coins' `wallet.dat` files and handing out change addresses. |

There is **no build system, no CI, no linter config, and no packaging
metadata** in this repo. `multiwallet/requirements.txt` is the only dependency
declaration. Tests exist for the Python 3 side only (`tests/`, run with
`python3 -m pytest tests/ -q`) and nothing runs them automatically.

## ⚠️ The single most important fact: the Python 2 / Python 3 split

`pywallet.py` is Python 2 source (`print` statements, `0x...L` long literals,
`StringIO`, `exceptions`, `xrange`, `from bsddb.db import *`). It **cannot be
imported or parsed by Python 3**:

```
$ python3 -c "import ast; ast.parse(open('pywallet.py').read())"
SyntaxError: invalid hexadecimal literal   # the 0x...L secp256k1 constants
```

`multiwallet` is Python 3 (`from __future__ import annotations`, PEP 484 type
hints, f-strings in templates, Flask 2+).

Consequences you must keep in mind:

* `WalletSession._parse_via_pywallet()` (`multiwallet/wallet_session.py:293`)
  tries to `exec_module` `pywallet.py`. Under Python 3 this **always** raises
  `SyntaxError`, which is caught in `_parse_wallet_dat()` and silently degrades
  to `_parse_minimal()` — a best-effort raw-BDB reader that recovers **public
  keys only** (no WIF private keys, no decryption). So in practice today,
  multiwallet never uses pywallet's proven parser.
* `_load_pywallet()` at `multiwallet/wallet_session.py:31` is dead code — nothing
  calls it, and it references a non-existent `importlib.util.load_from_spec`.
* Do not "fix" a Python 3 syntax error in `pywallet.py` by modernizing one
  line. Either leave it as Python 2, or port the whole file deliberately.
  Partial ports will break the CLI without fixing multiwallet.

When editing, always ask: *which interpreter runs this file?*

## Running things

### pywallet CLI (needs Python 2.7 + `bsddb`)

```bash
python2 pywallet.py --listcoins                    # print the coin table and exit
python2 pywallet.py --dumpwallet                   # dump default BTC datadir wallet as JSON
python2 pywallet.py --dumpwallet --coin LTC        # set network params from the ticker
python2 pywallet.py --dumpwallet --walletfile /path/to/any.dat
python2 pywallet.py --dumpwallet --testnet --password 'secret'
python2 pywallet.py --importprivkey <WIF>          # writes to the wallet
```

Note: `python2` is **not installed in this container**. You can read and edit
`pywallet.py`, but you cannot execute or syntax-check it here. Verify changes by
careful reading, not by running.

Network-parameter precedence in `main()` (`pywallet.py:1774`):
`--addrtype`/`--wifprefix` > `--coin` > `--testnet` > BTC defaults. The
resolution mutates the module globals `addrtype` and `wif_prefix`.

### multiwallet web app (Python 3 + Flask + Berkeley DB)

```bash
pip install -r multiwallet/requirements.txt        # flask, bsddb3
python -m multiwallet.app                          # http://localhost:5000
MULTIWALLET_PORT=8080 python -m multiwallet.app
```

`bsddb3` needs system Berkeley DB headers (`libdb-dev` on Debian/Ubuntu,
`brew install berkeley-db@5` on macOS). Without it the app still imports and
serves the UI — wallet loading is what fails.

`app.run(host="0.0.0.0")` binds all interfaces. There is **no authentication**.
Treat this as localhost-only software.

## Layout and responsibilities

```
pywallet.py               # everything: AES, EC_KEY/ecdsa, base58, bech32,
                          # BCDataStream, BDB wallet parsing, CLI main()
multiwallet/
├── app.py                # Flask routes only — thin; all logic delegated
├── coin_registry.py      # CoinInfo + CoinRegistry singleton (coins.json + MPS)
├── coins.json            # generated coin table — source of truth, 165 coins
├── wallet_manager.py     # WalletManager singleton: {ticker: [WalletSession]}
├── wallet_session.py     # WalletSession: one wallet.dat, change-address pool
└── templates/index.html  # 700-line vanilla-JS SPA, dark theme, no build step
tools/build_coin_table.py # regenerates coins.json from 3 sources
tests/                    # pytest, Python 3 only, no wallet.dat or network
```

Data flow: HTTP → `app.py` route → `WalletManager.get_instance()` →
`WalletSession` → BDB parse → `WalletKey` objects → `.to_dict()` → JSON.

Both `CoinRegistry` and `WalletManager` are process-wide singletons guarded by a
class-level `threading.Lock` and reached via `.get_instance()` — never construct
them directly.

### The frontend

`templates/index.html` is hand-written HTML + CSS + vanilla JS in one file. No
bundler, no framework, no npm. It calls the REST API via a small `api()` helper
(`index.html:403`). Edit it directly; there is nothing to compile.

## The coin tables

`multiwallet/coins.json` (165 coins) is the **source of truth** for the Python 3
side. It is generated by `tools/build_coin_table.py`, which merges three
sources: this repo's `COIN_PARAMS`, the **hdwallet** PyPI package (142 mainnet
coins), and the **coininfo** npm package (24 coins, each citing the coin's own
`chainparams.cpp`). Regenerate rather than hand-editing:

```bash
python3 tools/build_coin_table.py     # needs pypi.org + registry.npmjs.org
```

Two smaller tables remain and still need manual care:

| | `pywallet.py:56` `COIN_PARAMS` | `coin_registry.py` `STATIC_COIN_PARAMS` |
|---|---|---|
| Role | the Python 2 CLI's only table | fallback if `coins.json` is unreadable |
| Tuple | `(name, pubkey_ver, wif_ver, p2sh_ver, bech32_hrp)` | same **+ `bip44_index`** |
| Coverage | 58 tickers incl. `TBTC`/`TLTC` testnet | 36 tickers |

`COIN_PARAMS` feeds the generator, so **corrections still start there**; the
Py2 CLI reads nothing else. `STATIC_COIN_PARAMS` is a degraded fallback only.

**Conflict policy.** The generator changes a value only when *two independent
sources agree against us*; a lone disagreement is recorded as
`"disputed": true` with both candidates in `wif_alternatives`, and the registry
then reports the coin as unverified rather than picking a winner. Four WIF
bytes were corrected this way (BTG, DGB, MONA, VTC — all had `pubkey+0x80`
wrongly applied) and two were confirmed *ours* against a bad third-party value
(DOGE, where hdwallet carries a testnet byte; NMC). Six remain disputed: BTX,
LBC, LCC, NLG, PHR, SYS. See `OVERRIDES` / `KNOWN_GOOD` in the generator for
the reasoning — never silently resolve one of these.

**Provenance.** Every `CoinInfo` carries `provenance` (`node` > `manual` >
`table` > `guessed` > `default`) and a `verified` flag. Unknown coins can be
loaded by passing `pubkey_ver` to `/api/wallets/load`; an omitted `wif_ver` is
inferred from two ranked conventions (`wif_candidates()`) and flagged as a
guess. Coins known only from miningpoolstats are `default` — Bitcoin bytes,
almost certainly wrong.

Remaining limitations:

* **2-byte address prefixes now work** (ZEC, ZCL, FLUX, BTCZ, ZEN, YEC, LTZ,
  ANON, HUSH, XAX, DCR). `version_bytes()` encodes a version at its natural
  width and `_b58check_encode` uses it. Golden vectors live in
  `tests/test_addresses.py`. Note `pywallet.py` still cannot express these.
* Coins with **non-Bitcoin hashing** (GRS uses Groestl; XMR/ETH/ETC/KAS/ERGO are
  not BDB wallets at all) produce wrong addresses even when WIF decoding works.

`CoinRegistry` also enriches coins hourly from
`https://miningpoolstats.stream/data/coins_data.js` (`CACHE_TTL = 3600`). That
data is **cosmetic** — algorithm, hashrate, block time. Coins found only on MPS
are registered with **Bitcoin default version bytes**, so addresses derived for
them will be wrong. Network failures are swallowed and `_last_fetch` is still
stamped to avoid hammering; the app must work fully offline.

## Conventions

**Style.** `multiwallet` uses 4-space indent, module docstrings with a `~~~~`
underline, typed signatures, `# ---- section ----` banner comments, `logger =
logging.getLogger(__name__)` per module, and `.format()` (not f-strings) in
Python code. `pywallet.py` follows the original upstream style — leave its
formatting alone; keep diffs to it minimal so it stays diffable against
joric/pywallet.

**API responses.** Every `multiwallet` endpoint returns JSON containing `ok`.
Use the helpers in `app.py`: `_json_ok(**kwargs)` and `_json_err(msg, code)`.
Status codes in use: 400 validation, 404 session not found, 409 no unused change
address, 500 unexpected.

**Error messages must never leak internals.** Several commits in this repo's
history (`9e44c11`, `b7afe0a`, `e8a9829`) exist solely to fix CodeQL
`py/stack-trace-exposure`. The established pattern in `app.py:117-127` is:

```python
except ValueError as exc:
    logger.warning("load_wallet validation error: %s", exc)   # detail -> log
    return _json_err("Invalid request. Check ticker and dat_path.")  # generic -> client
except Exception:
    logger.exception("Unexpected error loading wallet")
    return _json_err("Internal server error", 500)
```

Never interpolate `str(exc)`, a traceback, or a filesystem path into an HTTP
response body. Log it instead.

**Secrets.** Private keys and passphrases live in memory only, inside
`WalletSession`. `unload()` clears `_keys`. Do not log key material, WIFs, or
passphrases; do not add them to `to_dict()` (note `WalletKey.to_dict()`
deliberately exposes only `has_privkey: bool`). Do not write wallet contents to
disk or send them anywhere.

**Exception handling.** `WalletSession.load()` returns `bool` and stashes the
message on `self._error`; `WalletManager.load_wallet()` converts failure into
`RuntimeError`, unknown ticker / missing file into `ValueError`, and `app.py`
maps those to status codes. Preserve that layering.

## Working in this repo

* Branch: develop on `claude/claude-md-docs-s6vebd`; `master` is the default branch.
* Commits here are short imperative one-liners (`bech32 support`, `Add multiwallet: ...`).
* `multiwallet/__pycache__/` is checked in and there is no `.gitignore`. Don't
  add new `.pyc` files to commits; adding a `.gitignore` is a reasonable
  cleanup if asked.
* Tests live in `tests/` (89 tests, `python3 -m pytest tests/ -q`); there is no CI
  to run them. If you change parsing or address-encoding logic, say
  explicitly in your report that it is unverified, or add a test that can run
  under Python 3 without a real `wallet.dat`.
* `README` (no extension, at the root) documents only the original Python 2 CLI
  and is out of date with respect to `--coin`, `--walletfile`, `--addrtype`,
  `--wifprefix`, and `--listcoins`. `multiwallet/README.md` is the accurate,
  detailed doc for the web app.
