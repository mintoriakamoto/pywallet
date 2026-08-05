#!/usr/bin/env python3
"""
verify_from_chainparams.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Verify coin version bytes against each coin's own ``chainparams.cpp``.

    python3 tools/verify_from_chainparams.py            # check disputed coins
    python3 tools/verify_from_chainparams.py --all      # check everything known
    python3 tools/verify_from_chainparams.py --apply    # write results back

Every Bitcoin-derived coin declares its address bytes in ``src/chainparams.cpp``
as ``base58Prefixes[PUBKEY_ADDRESS]`` / ``[SCRIPT_ADDRESS]`` / ``[SECRET_KEY]``.
That file is the coin's own ground truth — better than any aggregator, and the
only way to settle a disagreement between two third-party datasets.

The one thing that must not go wrong here is reading the *testnet* block by
mistake: that is exactly the bug that makes hdwallet report Dogecoin's WIF byte
as 0xf1. ``mainnet_block()`` slices out ``CMainParams`` before any parsing, and
``--apply`` refuses to write a coin whose mainnet block could not be isolated.

Fetches over raw.githubusercontent.com; no GitHub API token needed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COINS_JSON = os.path.join(REPO, "multiwallet", "coins.json")
RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
BRANCHES = ("master", "main", "develop", "dev")
#: Bitcoin Core moved CMainParams to src/kernel/chainparams.cpp in v24, so
#: forks rebased on modern Core keep only a stub at the classic path.  Try
#: both and take whichever actually contains the class.
PATHS = ("src/chainparams.cpp", "src/kernel/chainparams.cpp")

# ticker -> GitHub owner/repo.  Only coins whose upstream we can name; this is
# deliberately explicit rather than guessed, because fetching the wrong repo
# would silently "verify" a coin against someone else's parameters.
REPOS = {
    # --- controls: values we are already confident about ---
    "BTC":  "bitcoin/bitcoin",
    "LTC":  "litecoin-project/litecoin",
    "DOGE": "dogecoin/dogecoin",
    "DASH": "dashpay/dash",
    "NMC":  "namecoin/namecoin-core",
    # --- corrected by 2-of-3 majority; confirm against source ---
    "BTG":  "BTCGPU/BTCGPU",
    "DGB":  "DigiByte-Core/digibyte",
    "MONA": "monacoinproject/monacoin",
    "VTC":  "vertcoin-project/vertcoin-core",
    # --- disputed: this is what we are here to settle ---
    "BTX":  "LIMXTEC/BitCore",
    "LBC":  "lbryio/lbrycrd",
    "LCC":  "litecoincash-project/litecoincash",
    "NLG":  "Gulden/gulden-official",
    "PHR":  "phoreproject/Phore",
    "SYS":  "syscoin/syscoin",
    # --- multi-byte prefixes, worth confirming ---
    "ZEC":  "zcash/zcash",
    "ZEN":  "HorizenOfficial/zen",
    "RVN":  "RavenProject/Ravencoin",
    "GRS":  "Groestlcoin/groestlcoin",
    "QTUM": "qtumproject/qtum",
    "PIVX": "PIVX-Project/PIVX",
}

FIELDS = {
    "pub": "PUBKEY_ADDRESS",
    "p2sh": "SCRIPT_ADDRESS",
    "wif": "SECRET_KEY",
}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(repo: str) -> tuple:
    """
    Return (text, url) for the first candidate that actually defines
    CMainParams, or (None, None).

    A file that fetches but has no CMainParams is not good enough: modern Core
    leaves a stub at src/chainparams.cpp with the class in kernel/, so taking
    the first HTTP 200 would silently yield nothing to parse.
    """
    fallback = (None, None)
    for branch in BRANCHES:
        for path in PATHS:
            url = RAW.format(repo=repo, branch=branch, path=path)
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "pywallet-coin-verify/1.0"})
                with urllib.request.urlopen(req, timeout=30) as fh:
                    text = fh.read().decode("utf-8", "replace")
            except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                continue
            if re.search(r"class\s+CMainParams\b", text):
                return text, url
            if fallback[0] is None:
                fallback = (text, url)  # keep for the error message
    return fallback


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def mainnet_block(text: str) -> str | None:
    """
    Slice out the CMainParams class body.

    Returns None rather than guessing when the class can't be located — a
    caller must not fall back to scanning the whole file, or it will read
    testnet values.
    """
    start = re.search(r"class\s+CMainParams\b", text)
    if not start:
        return None
    rest = text[start.end():]
    # Stop at the next params class (CTestNetParams, CRegTestParams, ...).
    nxt = re.search(r"class\s+C[A-Za-z0-9_]*Params\b", rest)
    return rest[:nxt.start()] if nxt else rest


def eval_int_expr(expr: str):
    """
    Evaluate a trivial integer expression from C++ source.

    Gulden writes ``std::vector<unsigned char>(1, 38+128)`` — the coin spells
    out the pubkey+0x80 convention rather than a literal — so a plain literal
    match would miss its SECRET_KEY entirely.  Only decimal/hex literals joined
    by + or - are accepted; anything else returns None rather than guessing.
    """
    expr = expr.strip()
    if not re.fullmatch(r"(0[xX][0-9a-fA-F]+|\d+)(\s*[+-]\s*(0[xX][0-9a-fA-F]+|\d+))*", expr):
        return None
    tokens = re.findall(r"[+-]|0[xX][0-9a-fA-F]+|\d+", expr)
    total, sign = int(tokens[0], 0), 1
    for token in tokens[1:]:
        if token in "+-":
            sign = 1 if token == "+" else -1
        else:
            total += sign * int(token, 0)
    return total if total >= 0 else None


def parse_prefix(block: str, key: str):
    """
    Extract one base58Prefixes entry as an int.

    Handles the four spellings that occur in the wild:
        std::vector<unsigned char>(1,30)
        std::vector<unsigned char>(1,0x1E)
        {0x1C,0xB8}                       (multi-byte, Zcash family)
        boost::assign::list_of(0x1C)(0xB8)
    """
    m = re.search(
        r"base58Prefixes\s*\[\s*" + key + r"\s*\]\s*=\s*([^;]+);",
        block, re.S)
    if not m:
        return None
    expr = m.group(1)

    single = re.search(r"vector\s*<\s*unsigned\s+char\s*>\s*\(\s*1\s*,\s*"
                       r"([^)]+?)\s*\)", expr)
    if single:
        value = eval_int_expr(single.group(1))
        if value is not None:
            return value

    listof = re.findall(r"list_of\s*\(\s*(0[xX][0-9a-fA-F]+|\d+)\s*\)"
                        r"|\)\s*\(\s*(0[xX][0-9a-fA-F]+|\d+)\s*\)", expr)
    if listof:
        vals = [int(a or b, 0) for a, b in listof]
        return int.from_bytes(bytes(vals), "big")

    braced = re.search(r"\{([^}]*)\}", expr)
    if braced:
        vals = [int(v.strip(), 0) for v in braced.group(1).split(",")
                if v.strip() and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", v.strip())]
        if vals:
            return int.from_bytes(bytes(vals), "big")
    return None


def parse_bech32(block: str):
    m = re.search(r'bech32_hrp\s*=\s*"([a-z0-9]+)"', block)
    return m.group(1) if m else None


def inspect(ticker: str, repo: str) -> dict:
    text, url = fetch(repo)
    if text is None:
        return {"ticker": ticker, "repo": repo, "error": "fetch failed"}
    block = mainnet_block(text)
    if block is None:
        return {"ticker": ticker, "repo": repo, "url": url,
                "error": "CMainParams not found"}
    result = {"ticker": ticker, "repo": repo, "url": url,
              "hrp": parse_bech32(block)}
    for field, key in FIELDS.items():
        result[field] = parse_prefix(block, key)
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def hexs(value):
    return "--" if value is None else "0x{:02x}".format(value)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true",
                    help="check every coin in REPOS (default: same thing, kept "
                         "for symmetry with --disputed)")
    ap.add_argument("--disputed", action="store_true",
                    help="only coins currently flagged disputed in coins.json")
    ap.add_argument("--apply", action="store_true",
                    help="write confirmed values back into coins.json")
    args = ap.parse_args()

    coins = json.load(open(COINS_JSON))
    targets = dict(REPOS)
    if args.disputed:
        targets = {t: r for t, r in REPOS.items() if coins.get(t, {}).get("disputed")}

    print("fetching chainparams.cpp for {} coins...\n".format(len(targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda kv: inspect(*kv), sorted(targets.items())))

    print("%-6s %-8s %-8s %-8s %-8s %s" %
          ("coin", "src.pub", "our.pub", "src.wif", "our.wif", "verdict"))
    print("-" * 78)

    agree, differ, failed, resolved = [], [], [], []
    for r in results:
        t = r["ticker"]
        ours = coins.get(t, {})
        if r.get("error"):
            failed.append((t, r["error"]))
            print("%-6s %-8s %-8s %-8s %-8s %s" %
                  (t, "--", hexs(ours.get("pub")), "--", hexs(ours.get("wif")),
                   "FETCH/PARSE FAILED: " + r["error"]))
            continue

        pub_ok = r["pub"] is not None and r["pub"] == ours.get("pub")
        wif_ok = r["wif"] is not None and r["wif"] == ours.get("wif")
        if r["pub"] is None or r["wif"] is None:
            verdict = "incomplete parse"
            failed.append((t, verdict))
        elif pub_ok and wif_ok:
            verdict = "agrees"
            agree.append(t)
            if ours.get("disputed"):
                verdict = "RESOLVES DISPUTE (ours confirmed)"
                resolved.append((t, ours.get("wif"), r["wif"]))
        else:
            verdict = "DISAGREES -> source wins"
            differ.append((t, ours.get("pub"), ours.get("wif"), r["pub"], r["wif"]))
            if ours.get("disputed"):
                resolved.append((t, ours.get("wif"), r["wif"]))

        print("%-6s %-8s %-8s %-8s %-8s %s" %
              (t, hexs(r["pub"]), hexs(ours.get("pub")),
               hexs(r["wif"]), hexs(ours.get("wif")), verdict))

    print("\nagree: {}   disagree: {}   unusable: {}".format(
        len(agree), len(differ), len(failed)))
    if resolved:
        print("\ndisputes settled by source ({}):".format(len(resolved)))
        for t, was, now in resolved:
            print("  {:<6} {} -> {}".format(t, hexs(was), hexs(now)))

    if args.apply:
        changed = 0
        for t, _, _, src_pub, src_wif in differ:
            coins[t].update(pub=src_pub, wif=src_wif)
            coins[t]["provenance_note"] = "verified against chainparams.cpp"
            coins[t].pop("disputed", None)
            coins[t].pop("wif_alternatives", None)
            changed += 1
        for t in agree:
            coins[t]["provenance_note"] = "verified against chainparams.cpp"
            coins[t].pop("disputed", None)
            coins[t].pop("wif_alternatives", None)
        with open(COINS_JSON, "w") as fh:
            json.dump(coins, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print("\napplied: {} corrected, {} confirmed -> {}".format(
            changed, len(agree), COINS_JSON))
    return 0


if __name__ == "__main__":
    sys.exit(main())
