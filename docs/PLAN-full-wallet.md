# Plan: universal `wallet.dat` loading with balances and Core-grade control

**Goal.** Point the app at any `wallet.dat`, have it open, show the real
spendable balance and transaction history, and let you move funds — the control
surface a Core wallet gives you.

**Decisions taken** (these shape everything below):

1. **Chain data comes from your own node** over Core-compatible JSON-RPC. No
   dependency on public Electrum or Blockbook servers.
2. **Spending is PSBT-export only.** The app builds unsigned transactions;
   signing happens on a separate offline machine.
3. **Coin coverage must not depend on a hardcoded table.** New and obscure
   coins have to work without waiting for someone to add a row.

Those three together produce a notably better architecture than the alternatives
would have, for a reason worth stating explicitly: *if the app never signs, the
web server never needs a private key.* Balances and history need only public
scripts. So the online component becomes watch-only, and key material is
confined to an offline CLI. This removes the single largest security risk in the
project rather than mitigating it.

---

## 1. Where we actually are

Three blockers sit between the current code and the goal.

**Blocker 1 — the parser can't run in the app.** `pywallet.py` is Python 2,
`multiwallet` is Python 3. `WalletSession._parse_via_pywallet()` always throws
`SyntaxError` and silently falls back to `_parse_minimal()`, which recovers
*public keys only*. Today the web app cannot read a private key from any wallet.

**Blocker 2 — the parser understands one wallet flavour.** `parse_wallet()`
dispatches on 14 record types and drops everything else into
`json_db[type] = 'unsupported'`. Missing records that matter:

| Record | What it holds | Consequence of dropping it |
|---|---|---|
| `cscript` | P2SH redeem scripts | P2SH / multisig funds unspendable |
| `hdchain`, `keymeta` | HD seed + derivation paths | can't derive unissued addresses |
| `walletdescriptor*` | output descriptors incl. xprv | descriptor wallets read as empty |
| `watchs` | watch-only scripts | watch-only balances invisible |
| `flags` | wallet feature flags | can't distinguish descriptor from legacy |
| `tx` (body) | wallet transactions | only the txid is parsed, never the tx |

**Blocker 3 — no chain data, so no balance.** A `wallet.dat` holds keys, not
funds. Nothing in this repo talks to a network.

Supporting problems: no tests, no CI, two coin tables that have already drifted,
`bsddb3` needs native Berkeley DB headers, and the server binds `0.0.0.0` with
no authentication.

---

## 2. What "every wallet.dat type" resolves to

There is no single format. All four variants below are in scope for Phase 1 —
you asked for all of them:

| Variant | Container | Keys stored as | Today |
|---|---|---|---|
| Core ≤ 0.20 legacy, plaintext | Berkeley DB | `key` | works (Py2 CLI only) |
| Core ≤ 0.20 legacy, encrypted | Berkeley DB | `mkey` + `ckey` | works (Py2 CLI only) |
| Core 0.13–0.20 legacy HD | Berkeley DB | `key`/`ckey` + `hdchain` | keys read, HD metadata lost |
| Core 0.21+ legacy | **SQLite** | same records, SQLite `main` table | **not handled** |
| Core 0.21+ / 23+ descriptor | SQLite | `walletdescriptor*` (xprv inside) | **not handled** |
| Altcoin forks | Berkeley DB | `key`/`ckey` | works if version bytes are right |
| Damaged / partially overwritten | raw bytes | — | crude best-effort only |

Orthogonal to that: script types — P2PK, P2PKH, P2SH, P2SH-P2WPKH, P2WPKH,
P2WSH, bare multisig — each needing its own address encoding, UTXO scan, and
signing path.

### Out of scope, permanently

* **Shielded balances** (ZEC/ZEN sapling/sprout notes) — a separate
  cryptosystem with its own proving keys. Transparent balances on those chains
  are in scope; shielded ones are not.
* **Non-BDB chains** — XMR, ETH/ETC, KAS, ERGO have no Core `wallet.dat`. They
  belong in the coin table only as an explicit "unsupported" mark.
* **Two-byte address prefixes** (ZEC, ZCL, FLUX, BTCZ, ZEN, ANON) are *in*
  scope but need the schema change in Phase 0 — the current single-version-byte
  assumption cannot express them.

---

## 3. Supporting coins that aren't in any table

You named voidcoin and BT3/bc2 as examples. I could not verify those specific
tickers or their network parameters, and that is exactly the point: hand-adding
rows does not scale and silently produces *wrong but valid-looking* addresses
when a value is guessed. The mechanism matters more than the table.

Since you're running your own node, the node can be **asked** for its own
parameters — no table lookup, no guessing:

| Parameter | How it's derived |
|---|---|
| pubkey version byte | `getnewaddress "" legacy` → base58check-decode → leading byte |
| WIF version byte | `dumpprivkey <that address>` → base58check-decode → leading byte |
| P2SH version byte | `getnewaddress "" p2sh-segwit` (or `addmultisigaddress`) → decode |
| bech32 HRP | `getnewaddress "" bech32` → the part before the `1` separator |
| chain / magic | `getblockchaininfo` |

This makes any Core-derived coin work on first contact, including ones that
don't exist yet. The derived parameters get cached in a local profile keyed by
the node's chain id, and the app shows which coins were **derived from a node**
versus **taken from the static table** versus **entered by hand** — because
those carry very different confidence.

Two fallbacks for when a node isn't available for a coin:

* **Learn from a known address.** Paste any address of that coin and we recover
  the pubkey version byte. This does *not* yield the WIF byte — the widespread
  "WIF = pubkey + 0x80" convention is violated by real coins in our own table
  (RVN is `0x3c`/`0x80`, GRS is `0x24`/`0x80`), so we will not guess it. The
  wallet loads read-only for address display.
* **Manual entry**, with all four bytes required and no defaults filled in.

An important compatibility note: `scantxoutset` (the fast, no-rescan balance
path) landed in Core 0.17. Forks from older code won't have it. Fallback is
`importaddress` + `rescanblockchain` against a throwaway watch-only wallet on
that node — much slower, but present in essentially every fork. The backend
probes for `scantxoutset` and picks the path automatically.

---

## 4. Architecture

The PSBT-only decision splits this cleanly into an online half that never sees a
private key and an offline half that does.

```
   ONLINE  (watch-only — no private keys, ever)
   ┌─────────────────────────────────────────────────────┐
   │  Web UI / REST API                                  │
   ├─────────────────┬───────────────────┬───────────────┤
   │ WalletStore     │ CoreRpcBackend    │ PsbtBuilder   │
   │ (public scripts │ (balance, UTXOs,  │ (coin select, │
   │  only)          │  history, relay)  │  fees, build) │
   └─────────────────┴───────────────────┴───────────────┘
                              │  unsigned PSBT out
                              ▼
   ─ ─ ─ ─ ─ ─ ─ ─  air gap  ─ ─ ─ ─ ─ ─ ─ ─
                              │
   OFFLINE  (walletcore CLI — the only thing that touches keys)
   ┌─────────────────────────────────────────────────────┐
   │ WalletStore (full) → decrypt → sign PSBT → signed   │
   │   BdbReader · SqliteReader · DescriptorReader ·      │
   │   SalvageReader                                      │
   └─────────────────────────────────────────────────────┘
                              │  signed PSBT back
                              ▼
                    online app → sendrawtransaction
```

**`WalletStore`** turns bytes on disk into spendable descriptors: a key, the
scripts it controls, and the metadata needed to sign. Never touches the network.
It has a **watch-only mode** that extracts scripts and skips key material
entirely — that mode is what the web app uses.

**`CoreRpcBackend`** is the only chain backend, per your decision. Narrow
interface: `get_balance(scripts)`, `list_utxos(scripts)`, `get_history(scripts)`,
`broadcast(rawtx)`, `estimate_fee(target)`. Node connection details live in a
per-coin config.

**`PsbtBuilder`** does coin selection, fee estimation, and emits BIP174 PSBTs.

**We own the PSBT layer end to end.** This matters: BIP174 postdates most
altcoin forks, so their nodes cannot parse, sign, or finalize a PSBT. Ours is a
self-contained implementation — the node is only ever asked for UTXOs and for
`sendrawtransaction`, both of which exist everywhere. This is what makes
PSBT-only spending viable across coins whose Core has never heard of PSBT.

---

## 5. Phases

Each phase is independently shippable and leaves the app working.

### Phase 0 — foundation

* Port `pywallet.py` to Python 3 as a new package `walletcore/`, leaving the
  original in place at first so the Py2 CLI keeps working and the port stays
  diffable against it. Retire the original once `walletcore` passes the fixture
  suite.
* Replace the vendored pure-Python AES and ECDSA with `cryptography` and
  `coincurve` (libsecp256k1) — far faster and constant-time, which matters for
  signing and for passphrase recovery attempts. Keep the pure-Python path as an
  optional dependency-free fallback for the offline machine.
* **Write a pure-Python Berkeley DB btree reader**, eliminating the `bsddb3`
  native dependency — the single biggest install obstacle, and especially
  valuable on an offline signing machine. ~400 lines walking BDB page
  structures directly; a real version of what `_parse_minimal` gropes at.
* Collapse the two coin tables into one data file whose schema can express
  multi-byte prefixes, script-type support, hash-function hooks, sighash
  quirks, and parameter **provenance** (node-derived / table / manual).
* Add pytest + CI, commit small fixture wallets, add `.gitignore`, drop the
  checked-in `__pycache__`.

**Exit:** `walletcore` reads every wallet the Py2 CLI could, from Python 3, with
no native dependencies, under test.

### Phase 1 — universal reader

All four variants you selected, plus unknown-coin support:

* SQLite container support (Core 0.21+). Same records, different container — a
  second reader feeding the same record dispatcher.
* Descriptor wallets: parse `walletdescriptor*`, implement descriptor
  expressions (`pkh`, `wpkh`, `sh(wpkh)`, `tr`, `multi`, `combo`) and BIP32
  derivation, expand each over its range.
* Encrypted wallets: keep the `mkey`/`ckey` path, add a proper unlock/lock
  lifecycle. Online app never unlocks — it doesn't need to.
* Damaged wallets: salvage mode scanning raw bytes for key-shaped material,
  validating each candidate by deriving its pubkey and checking it against any
  recoverable `key`/`ckey` record.
* Add the missing record types from §1 — `cscript` especially, since without it
  P2SH funds are visible but permanently unspendable.
* Node-derived coin parameters (§3), with the two fallbacks.

**Exit:** a detection routine identifies the variant of any wallet handed to it
and either loads it or reports precisely why not.

### Phase 2 — balances (watch-only)

* `CoreRpcBackend`, with the `scantxoutset` / `importaddress`+rescan dual path.
* Script derivation for every type the wallet contains, so we ask the node
  about all scripts a key controls, not just P2PKH.
* Balance with confirmed/unconfirmed split, UTXO list, transaction history with
  confirmation counts.
* Caching and incremental sync; degrade gracefully when a node is unreachable.
* Multi-node config: one RPC endpoint per coin.

**Exit:** load a wallet, see the correct balance. This is the halfway point and
the most valuable single milestone — worth shipping on its own, and it needs no
key material at all.

### Phase 3 — PSBT spending

* Coin selection mirroring Core's approach (branch-and-bound, then knapsack /
  single-random-draw fallback).
* Fee estimation from the node, with vsize-accurate computation.
* Full BIP174 implementation: build unsigned PSBT with all input metadata
  (previous txout, redeem/witness scripts, derivation paths).
* Offline signer in `walletcore`: consumes wallet + PSBT, produces signed PSBT.
  Legacy sighash, BIP143 for witness inputs, `SIGHASH_FORKID` for BTG-class
  forks, RFC6979 deterministic nonces.
* Finalize and broadcast via `sendrawtransaction`, with `testmempoolaccept`
  first where the node supports it.
* **Regtest-first.** No mainnet path merges until the full round trip passes
  end-to-end against a regtest node in CI.

**Exit:** build on the online box, sign on the offline box, broadcast, confirmed
— on regtest, then testnet, then mainnet.

### Phase 4 — Core-grade control

* Labels and address book (`name` records), read and write.
* New address generation. For a legacy non-HD wallet this means writing keys
  into `wallet.dat` — the first destructive operation in the project. Mandatory
  timestamped backup, write to a copy, verify, then swap. Never edit the live
  file in place. Runs offline only.
* Keypool top-up, `dumpwallet` / `importwallet` / `dumpprivkey` equivalents,
  encrypt-wallet and change-passphrase, sign/verify message, RBF and bumpfee.

### Phase 5 — hardening

* Bind `127.0.0.1` by default; explicit flag plus token to expose.
* Session auth token, CSRF protection, rate limiting.
* Wallet-file paths treated as untrusted input; nothing internal in HTTP
  responses (the existing CodeQL pattern in `app.py`).
* Explicit spend confirmation showing destination, amount, and fee before a
  PSBT is emitted.
* Packaging so a non-developer can install both halves.

---

## 6. What this buys, and what it costs

The PSBT-only choice means the web app is watch-only forever. Balances, history,
address lists and transaction construction all work there; seeing a private key
or signing anything requires the offline CLI. That is a real ergonomic cost
versus Core — you cannot click "send" and be done — and it is the reason the
whole project stops being a place where a web bug can drain a wallet.

The own-node choice means coverage is bounded by nodes you actually run, not by
what public infrastructure exists. Every coin is reachable, but each coin costs
you a synced node. Phase 2 should therefore land with clear UI for "no node
configured for this coin" as a normal state rather than an error.

## 7. Risks

| Risk | Mitigation |
|---|---|
| A signing bug burns real funds | regtest-gated CI, PSBT round-trip tests, `testmempoolaccept`, mainnet last |
| Writing to `wallet.dat` corrupts it | never write in place; backup + verify + swap; read-only default |
| Wrong version bytes produce valid-looking wrong addresses | derive from node where possible; show provenance; refuse to guess WIF bytes; golden vectors in CI |
| Old fork lacks `scantxoutset` | probe and fall back to `importaddress` + rescan |
| Old fork can't parse PSBT | we own the PSBT layer; node only sees `sendrawtransaction` |
| Salvage mode yields plausible garbage keys | validate every candidate by pubkey derivation before surfacing it |
| Offline machine ends up online | document the threat; the CLI has no network code at all |
