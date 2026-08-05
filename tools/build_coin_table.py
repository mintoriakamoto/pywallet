#!/usr/bin/env python3
"""
build_coin_table.py
~~~~~~~~~~~~~~~~~~~
Regenerate ``multiwallet/coins.json`` from this repo's curated table plus two
independent third-party datasets.

    python3 tools/build_coin_table.py [--out multiwallet/coins.json]

Sources
-------
1. ``pywallet.py:COIN_PARAMS`` — this repo's curated table (58 coins). Hand
   maintained, and demonstrably wrong for a handful of WIF bytes where the
   "WIF = pubkey + 0x80" convention was applied to coins that don't follow it.
2. **hdwallet** (PyPI) — ``hdwallet/cryptocurrencies.py``, 142 mainnet coins
   with pubkey / script / WIF bytes, segwit HRP and SLIP-44 index. The only
   source here that carries multi-byte address prefixes (ZEC, ZEN, FLUX …).
3. **coininfo** (npm) — 24 coins, each file citing the coin's own
   ``chainparams.cpp``. Small, but high quality, and it is the tie-breaker
   whenever 1 and 2 disagree.

Conflict policy
---------------
A value is only changed when **two independent sources agree against us**.
Where exactly one source disagrees the coin is marked ``disputed`` and both
candidate WIF bytes are recorded — the registry then treats it as unverified
and offers both on export, rather than silently picking a side.  See OVERRIDES
below for the resolved cases and the reasoning behind each.

Requires network access to pypi.org and registry.npmjs.org.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HDWALLET_VERSION = "2.2.1"

# ---------------------------------------------------------------------------
# Conflicts resolved by a 2-of-3 majority.  ticker -> (wif, why)
#
# Everything here was wrong in COIN_PARAMS because pubkey+0x80 was assumed.
# Each is confirmed by BOTH hdwallet and coininfo, and coininfo's file for the
# coin cites that coin's own chainparams.cpp.
# ---------------------------------------------------------------------------
OVERRIDES = {
    "BTG":  (0x80, "Bitcoin Gold kept Bitcoin's SECRET_KEY=128; we had 0xa6"),
    "DGB":  (0x80, "DigiByte SECRET_KEY=128; we had 0x9e"),
    "MONA": (0xB0, "Monacoin SECRET_KEY=176, as Litecoin; we had 0xb2"),
    "VTC":  (0x80, "Vertcoin SECRET_KEY=128; we had 0xc7"),
}

# Conflicts where *we* are right and the third-party source is wrong.
# Recorded so a future regeneration doesn't silently "fix" them back.
KNOWN_GOOD = {
    "DOGE": (0x9E, "hdwallet reports 0xf1, which is Dogecoin *testnet*; "
                   "coininfo and we both say 0x9e"),
    "NMC":  (0xB4, "hdwallet reports 0x80; coininfo and we both say 0xb4"),
}


# ---------------------------------------------------------------------------
# Source 1 — this repo
# ---------------------------------------------------------------------------

def load_pywallet_table() -> dict:
    """Parse COIN_PARAMS out of pywallet.py (which Python 3 cannot import)."""
    src = open(os.path.join(REPO, "pywallet.py")).read()
    block = src[src.index("COIN_PARAMS = {"):]
    block = block[:block.index("\n}\n") + 2]

    coins = {}
    row = re.compile(
        r"'([A-Z0-9]+)':\s*\('([^']+)',\s*(0x[0-9a-f]+),\s*(0x[0-9a-f]+),\s*"
        r"(None|0x[0-9a-f]+),\s*(None|'[a-z0-9]+')"
    )
    for ticker, name, pub, wif, p2sh, hrp in row.findall(block):
        coins[ticker] = {
            "name": name,
            "pub": int(pub, 16),
            "wif": int(wif, 16),
            "p2sh": None if p2sh == "None" else int(p2sh, 16),
            "hrp": None if hrp == "None" else hrp.strip("'"),
            "bip44": None,
        }
    return coins


# ---------------------------------------------------------------------------
# Source 2 — hdwallet (PyPI)
# ---------------------------------------------------------------------------

def load_hdwallet(workdir: str) -> dict:
    """Download hdwallet and AST-parse its Cryptocurrency subclasses."""
    url = ("https://pypi.org/pypi/hdwallet/{}/json".format(HDWALLET_VERSION))
    with urllib.request.urlopen(url, timeout=60) as fh:
        meta = json.load(fh)
    sdist = next(u["url"] for u in meta["urls"] if u["packagetype"] == "sdist")

    tgz = os.path.join(workdir, "hdwallet.tar.gz")
    urllib.request.urlretrieve(sdist, tgz)
    with tarfile.open(tgz) as tf:
        member = next(m for m in tf.getmembers()
                      if m.name.endswith("hdwallet/cryptocurrencies.py"))
        src = tf.extractfile(member).read().decode("utf-8")

    def const(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -const(node.operand)
        return None

    def wrapped_dict(node):
        """CoinType({...}) / SegwitAddress({...}) -> plain dict."""
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Dict):
            return {k.value: const(v)
                    for k, v in zip(node.args[0].keys, node.args[0].values)
                    if isinstance(k, ast.Constant)}
        return {}

    coins = {}
    for cls in [n for n in ast.parse(src).body if isinstance(n, ast.ClassDef)]:
        attrs = {
            st.targets[0].id: st.value
            for st in cls.body
            if isinstance(st, ast.Assign)
            and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
        }
        if "SYMBOL" not in attrs or "WIF_SECRET_KEY" not in attrs:
            continue
        if const(attrs.get("NETWORK")) != "mainnet":
            continue
        segwit = wrapped_dict(attrs["SEGWIT_ADDRESS"]) if "SEGWIT_ADDRESS" in attrs else {}
        cointype = wrapped_dict(attrs["COIN_TYPE"]) if "COIN_TYPE" in attrs else {}
        coins[const(attrs["SYMBOL"])] = {
            "name": const(attrs.get("NAME")),
            "pub": const(attrs.get("PUBLIC_KEY_ADDRESS")),
            "wif": const(attrs.get("WIF_SECRET_KEY")),
            "p2sh": const(attrs.get("SCRIPT_ADDRESS")),
            "hrp": segwit.get("HRP"),
            "bip44": cointype.get("INDEX"),
        }
    return coins


# ---------------------------------------------------------------------------
# Source 3 — coininfo (npm)
# ---------------------------------------------------------------------------

def load_coininfo(workdir: str) -> dict:
    """Download the coininfo npm package and evaluate it with node."""
    with urllib.request.urlopen("https://registry.npmjs.org/coininfo", timeout=60) as fh:
        meta = json.load(fh)
    version = meta["dist-tags"]["latest"]
    tarball = meta["versions"][version]["dist"]["tarball"]

    tgz = os.path.join(workdir, "coininfo.tgz")
    urllib.request.urlretrieve(tarball, tgz)
    with tarfile.open(tgz) as tf:
        tf.extractall(workdir)

    script = """
      const fs=require('fs'), path=require('path');
      const dir=process.argv[1], out={};
      for (const f of fs.readdirSync(dir)) {
        let m; try { m=require(path.resolve(dir,f)); } catch(e) { continue; }
        const c=m.main||m;
        if(!c||!c.versions||c.versions.public===undefined||c.versions.private===undefined) continue;
        out[c.unit||f]={name:c.name,pub:c.versions.public,wif:c.versions.private,
                        p2sh:c.versions.scripthash===undefined?null:c.versions.scripthash,
                        hrp:c.bech32||null,bip44:null};
      }
      process.stdout.write(JSON.stringify(out));
    """
    coins_dir = os.path.join(workdir, "package", "lib", "coins")
    proc = subprocess.run(["node", "-e", script, coins_dir],
                          capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge(pywallet: dict, hdwallet: dict, coininfo: dict) -> tuple:
    """Merge the three sources.  Returns (coins, report)."""
    coins, report = {}, {"overridden": [], "disputed": [], "added": 0, "kept": []}

    def sources_for(ticker):
        return [n for n, tbl in (("pywallet", pywallet),
                                 ("hdwallet", hdwallet),
                                 ("coininfo", coininfo)) if ticker in tbl]

    for ticker in sorted(set(pywallet) | set(hdwallet) | set(coininfo)):
        ours = pywallet.get(ticker)
        base = dict(ours or hdwallet.get(ticker) or coininfo[ticker])
        entry = {
            "name": base.get("name") or ticker,
            "pub": base["pub"],
            "wif": base["wif"],
            "p2sh": base.get("p2sh"),
            "hrp": base.get("hrp"),
            "bip44": base.get("bip44"),
            "sources": sources_for(ticker),
        }

        # Fill gaps from the other sources without overwriting our values.
        for other in (hdwallet.get(ticker), coininfo.get(ticker)):
            if not other:
                continue
            for field in ("p2sh", "hrp", "bip44"):
                if entry.get(field) is None and other.get(field) is not None:
                    entry[field] = other[field]

        if ticker in OVERRIDES and ours:
            wif, why = OVERRIDES[ticker]
            entry["wif"], entry["corrected"] = wif, why
            report["overridden"].append((ticker, ours["wif"], wif))
        elif ticker in KNOWN_GOOD and ours:
            entry["verified_against"] = KNOWN_GOOD[ticker][1]
            report["kept"].append(ticker)
        elif ours:
            # Unresolved disagreement: record both, pick neither.
            others = {n: t[ticker]["wif"]
                      for n, t in (("hdwallet", hdwallet), ("coininfo", coininfo))
                      if ticker in t and t[ticker]["wif"] != ours["wif"]}
            if others:
                entry["disputed"] = True
                entry["wif_alternatives"] = sorted(set(others.values()))
                report["disputed"].append((ticker, ours["wif"], others))
        else:
            report["added"] += 1

        coins[ticker] = entry

    return coins, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(REPO, "multiwallet", "coins.json"))
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as workdir:
        pywallet = load_pywallet_table()
        print("pywallet.py COIN_PARAMS : {} coins".format(len(pywallet)))
        hdwallet = load_hdwallet(workdir)
        print("hdwallet (PyPI)         : {} coins".format(len(hdwallet)))
        coininfo = load_coininfo(workdir)
        print("coininfo (npm)          : {} coins".format(len(coininfo)))

    coins, report = merge(pywallet, hdwallet, coininfo)

    print("\ncorrected by 2-of-3 majority ({}):".format(len(report["overridden"])))
    for ticker, was, now in report["overridden"]:
        print("  {:<6} 0x{:02x} -> 0x{:02x}  {}".format(
            ticker, was, now, OVERRIDES[ticker][1]))

    print("\nours confirmed against a bad third-party value ({}):".format(len(report["kept"])))
    for ticker in report["kept"]:
        print("  {:<6} {}".format(ticker, KNOWN_GOOD[ticker][1]))

    print("\ndisputed, left unresolved ({}):".format(len(report["disputed"])))
    for ticker, ours_wif, others in report["disputed"]:
        alts = ", ".join("{}=0x{:02x}".format(n, v) for n, v in others.items())
        print("  {:<6} ours=0x{:02x}  {}".format(ticker, ours_wif, alts))

    multibyte = [t for t, c in coins.items()
                 if c["pub"] > 0xFF or (c["p2sh"] or 0) > 0xFF]
    print("\nmulti-byte prefixes ({}): {}".format(len(multibyte), ", ".join(sorted(multibyte))))
    print("new coins added: {}".format(report["added"]))
    print("TOTAL: {} coins".format(len(coins)))

    with open(args.out, "w") as fh:
        json.dump(coins, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("\nwrote {}".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
