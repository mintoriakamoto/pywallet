# Plan: universal `wallet.dat` loading with balances and Core-grade control

**Goal.** Point the app at any `wallet.dat`, have it open, show the real
spendable balance and transaction history, and let you send funds — the control
surface a Core wallet gives you.

This document is the roadmap to get there from where the repo is today. It is
deliberately explicit about what is achievable, what is expensive, and what is
not possible at all, because the phrase "every wallet.dat" hides three separate
problems that need different solutions.

---

## 1. Where we actually are

Three hard blockers sit between the current code and the goal.

**Blocker 1 — the parser can't run in the app.** `pywallet.py` is Python 2.
`multiwallet` is Python 3. `WalletSession._parse_via_pywallet()` always throws
`SyntaxError` and silently degrades to `_parse_minimal()`, which recovers
*public keys only*. Today the web app cannot read a private key from any wallet,
encrypted or not.

**Blocker 2 — the parser only understands one wallet flavour.**
`parse_wallet()` dispatches on 14 record types and dumps everything else into
`json_db[type] = 'unsupported'`. Missing records that matter:

| Record | What it holds | Consequence of dropping it |
|---|---|---|
| `cscript` | P2SH redeem scripts | multisig / P2SH funds are unspendable |
| `hdchain`, `keymeta` | HD seed + derivation paths | can't derive unissued addresses |
| `walletdescriptor*` | output descriptors incl. xprv | descriptor wallets read as empty |
| `watchs` | watch-only scripts | watch-only balances invisible |
| `flags` | wallet feature flags | can't tell descriptor from legacy |
| `tx` (body) | wallet transactions | only the txid is parsed, never the tx |

**Blocker 3 — there is no chain data, so there is no balance.** A `wallet.dat`
holds keys, not funds. A balance requires querying a chain. Nothing in this repo
talks to a network. This is the single largest piece of new work, and no amount
of parser improvement substitutes for it.

Supporting problems: no tests, no CI, two coin tables that have already drifted,
`bsddb3` requires native Berkeley DB headers, and the server binds `0.0.0.0`
with no authentication — unacceptable once it can spend.

---

## 2. What "every wallet.dat type" resolves to

There is no single format. The realistic target is *every Bitcoin-Core-derived
wallet*, which is this matrix:

| Variant | Container | Keys stored as | Today |
|---|---|---|---|
| Core ≤ 0.20 legacy, plaintext | Berkeley DB | `key` | works (Py2 CLI only) |
| Core ≤ 0.20 legacy, encrypted | Berkeley DB | `mkey` + `ckey` | works (Py2 CLI only) |
| Core 0.13–0.20 legacy HD | Berkeley DB | `key`/`ckey` + `hdchain` | keys read, HD metadata lost |
| Core 0.21+ legacy | **SQLite** | same records, SQLite `main` table | **not handled** |
| Core 0.21+ / 23+ descriptor | SQLite | `walletdescriptor*` (xprv inside) | **not handled** |
| Altcoin forks (the ~60 in our table) | Berkeley DB | `key`/`ckey` | works if version bytes are right |
| Damaged / partially overwritten | raw bytes | — | crude best-effort only |

Add the orthogonal axis of *script types* — P2PK, P2PKH, P2SH, P2SH-P2WPKH,
P2WPKH, P2WSH, bare multisig — each needing its own address encoding, its own
UTXO scan, and its own signing path.

### Out of scope, permanently

State this up front so it never becomes a surprise:

* **Shielded balances** (ZEC/ZEN sapling/sprout notes) — a different
  cryptosystem with its own trusted-setup proving keys. Transparent balances on
  those chains are in scope; shielded ones are not.
* **Non-BDB chains** — XMR, ETH/ETC, KAS, ERGO don't have a Core `wallet.dat`
  at all. They belong in the coin table only as an explicit "unsupported" mark.
* **Coins with different hashing** — GRS (Groestl) needs a per-coin hash hook.
  Cheap to add for one or two coins, not free across the long tail.
* **Two-byte address prefixes** (ZEC, ZCL, FLUX, BTCZ, ZEN, ANON) are *in*
  scope but require the coin table schema change in Phase 0 — the current
  single-version-byte assumption cannot express them.

---

## 3. Architecture

Three layers, each independently testable, replacing the current single
`WalletSession` that does everything.

```
                  ┌──────────────────────────────────────┐
                  │  Web UI / REST API / CLI             │
                  └──────────────────┬───────────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
┌───────▼────────┐          ┌────────▼────────┐          ┌────────▼────────┐
│ WalletStore    │          │ ChainBackend    │          │ TxBuilder       │
│ (keys/scripts) │          │ (balance/utxo)  │          │ (spend/sign)    │
├────────────────┤          ├─────────────────┤          ├─────────────────┤
│ BdbBackend     │          │ CoreRpcBackend  │          │ coin selection  │
│ SqliteBackend  │          │ ElectrumBackend │          │ fee estimation  │
│ DescriptorRdr  │          │ BlockbookBackend│          │ sighash + sign  │
│ SalvageReader  │          │ EsploraBackend  │          │ PSBT import/exp │
└────────────────┘          └─────────────────┘          └─────────────────┘
```

**`WalletStore`** turns bytes on disk into a set of *spendable descriptors*: a
key, the script(s) it controls, and the metadata needed to sign. It never
touches the network.

**`ChainBackend`** is a narrow interface — `get_balance(scripts)`,
`list_utxos(scripts)`, `get_history(scripts)`, `broadcast(rawtx)`,
`estimate_fee(target)`. Four implementations because no single one covers the
coin list:

* `CoreRpcBackend` — any Core-compatible node. Uses `scantxoutset` so we get a
  balance without importing keys or triggering a rescan. Highest fidelity,
  works for *any* fork the user runs a node for. This is the answer for the
  long tail of altcoins.
* `ElectrumBackend` — ElectrumX/Fulcrum protocol. No node needed. Covers BTC,
  LTC, DASH, DOGE, VTC, BTG, DGB, RVN, MONA, GRS and others.
* `BlockbookBackend` — Trezor's uniform REST API across ~30 coins.
* `EsploraBackend` — BTC/LTC fallback.

Backend choice is per-coin config, with automatic fallback down the list.

**`TxBuilder`** does coin selection, fee estimation, sighash and signing, and
emits either a signed raw transaction or a PSBT.

### Coverage tiers

Not every coin gets every feature, and pretending otherwise is how this becomes
unmaintainable. Each coin in the table declares its tier:

| Tier | Meaning | Coins |
|---|---|---|
| **A — full** | balance, history, send, tested against regtest | BTC, LTC, DOGE, DASH, RVN, DGB |
| **B — balance** | balance + history via a public backend; send untested | ~20 with Electrum/Blockbook infra |
| **C — dump only** | keys and addresses only; balance needs your own node | the remaining long tail |

The UI must show the tier, so nobody assumes a C-tier balance of zero means
"no funds" when it means "we never looked".

---

## 4. Phases

Each phase is independently shippable and leaves the app working.

### Phase 0 — foundation

* Port `pywallet.py` to Python 3 as a new package `walletcore/`, leaving the
  original file untouched at first so the Py2 CLI keeps working and the port is
  diffable against it. Retire the original once `walletcore` passes the fixture
  suite.
* Replace the vendored pure-Python AES and ECDSA with `cryptography` and
  `coincurve` (libsecp256k1) — orders of magnitude faster and constant-time,
  which matters a lot for signing and for brute-forcing a forgotten passphrase.
  Keep the pure-Python path as an optional dependency-free fallback.
* **Write a pure-Python Berkeley DB btree reader.** This deletes the `bsddb3`
  native dependency, which is the single biggest install obstacle. ~400 lines
  to walk BDB page structures directly; a proper version of what
  `_parse_minimal` currently gropes at.
* Collapse the two coin tables into one data file (`coins.toml`) with a schema
  that can express multi-byte prefixes, script-type support, hash-function
  hooks, sighash quirks, tier, and default backend endpoints.
* Add pytest + CI. Commit small fixture wallets. Add a `.gitignore` and drop
  the checked-in `__pycache__`.

**Exit:** `walletcore` reads every wallet the Py2 CLI could, from Python 3, with
no native dependencies, under test.

### Phase 1 — universal reader

* SQLite container support (Core 0.21+). The records inside are identical; only
  the container changed, so this mostly means a second `WalletStore` backend
  feeding the same record dispatcher.
* Descriptor wallet support: parse `walletdescriptor*`, implement descriptor
  expressions (`pkh`, `wpkh`, `sh(wpkh)`, `tr`, `multi`, `combo`) and BIP32
  derivation, expand each descriptor over its range.
* Add the missing record types from the table in §1 — `cscript` in particular,
  since without it P2SH funds are visible but unspendable.
* Salvage mode: scan raw bytes for key-shaped material when the container is
  damaged, validating each candidate by deriving its pubkey.
* Encryption: keep the existing `mkey`/`ckey` path, add a proper unlock/lock
  lifecycle and an auto-lock timer.

**Exit:** a detection routine identifies the variant of any wallet handed to it
and either loads it or reports precisely why not.

### Phase 2 — balances (watch-only)

* `ChainBackend` interface and the four implementations.
* Script derivation for every type the wallet contains, so we ask the chain
  about all scripts a key controls, not just P2PKH.
* Balance, confirmed/unconfirmed split, UTXO list, transaction history with
  confirmations.
* Caching and incremental sync; must degrade gracefully offline.

**Exit:** load a wallet, see the correct balance. This is the halfway point and
the most valuable single milestone — worth shipping on its own.

### Phase 3 — spending

* Coin selection mirroring Core's approach (branch-and-bound, then knapsack /
  single-random-draw fallback).
* Fee estimation from the backend, with vsize-accurate computation.
* Signing: legacy sighash, BIP143 for witness inputs, `SIGHASH_FORKID` for
  BTG-class forks. RFC6979 deterministic nonces.
* Verify before broadcast — `testmempoolaccept` where a node is available.
* PSBT import/export, so an air-gapped flow is possible.
* **Regtest-first.** No mainnet send path merges until the same flow passes
  end-to-end against a regtest node in CI.

**Exit:** send a transaction on regtest, then on testnet, then mainnet.

### Phase 4 — Core-grade control

* Labels and address book (`name` records), read *and* write.
* New address generation. For a legacy non-HD wallet this means writing keys
  into `wallet.dat` — the first destructive operation in the project. It gets a
  mandatory timestamped backup, a write-ahead copy, and a verify-after-write
  step. Never edit the user's live file in place.
* Keypool top-up, `dumpwallet`/`importwallet`/`dumpprivkey` equivalents,
  encrypt-wallet and change-passphrase, sign/verify message, RBF and bumpfee.

### Phase 5 — hardening

* Bind `127.0.0.1` by default; require an explicit flag and a token to expose.
* Session auth token, CSRF protection, rate limiting.
* Passphrases and keys never logged, never serialized, wiped on lock.
* Explicit spend confirmation showing destination, amount, and fee.
* Packaging so a non-developer can install it.

---

## 5. Sequencing note

Phases 0 and 1 are pure local work with no external dependencies and no
security exposure — start there regardless of what gets decided about backends.
Phase 2 is where the design question about chain data sources actually bites,
and Phase 3 is where the security posture stops being theoretical.

The most useful thing to know before Phase 2 starts is whether you intend to run
your own nodes. If yes, `CoreRpcBackend` alone covers the entire coin list and
Phases 2–3 get dramatically simpler and more private. If no, coverage is capped
at whatever public Electrum/Blockbook infrastructure exists, which is the real
reason the tier system in §3 exists.

## 6. Risks

| Risk | Mitigation |
|---|---|
| A signing bug burns real funds | regtest-gated CI, PSBT path, verify-before-broadcast, mainnet last |
| Writing to `wallet.dat` corrupts it | never write in place; backup + verify; read-only default |
| Public backend lies about balance/UTXOs | cross-check across two backends for A-tier coins |
| Public backend learns all your addresses | document the privacy leak; recommend own node |
| Wrong version bytes silently produce valid-looking wrong addresses | golden address vectors per coin in CI; tier C marked clearly in UI |
| Scope sprawl across 60 coins | tier system; only A-tier promises send support |
