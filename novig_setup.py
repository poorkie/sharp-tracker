"""One-time Novig Paper setup. Run by .github/workflows/novig-setup.yml.

Needs secrets NOVIG_MGMT_KEY_ID, NOVIG_MGMT_PEM, NOVIG_TRADING_SEED.
  1. Checks the management key signature (POST /v3/echo).
  2. Opens a subaccount whose trading key is derived from NOVIG_TRADING_SEED
     (so no private key is ever stored outside GitHub secrets).
  3. Funds it with play money and checks the trading key works.
Writes the public trading key ID to state/novig.json and a report to
NOVIG_SETUP.md. Never prints or writes any private key material.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from sharp import novig

ROOT = Path(__file__).parent
STATE = ROOT / "state" / "novig.json"
LABEL = "sharper-sharp-ingame"
FUND_USD = "5000"


def main():
    host = os.environ.get("NOVIG_HOST", novig.PAPER)
    report = [f"# Novig setup ({'Paper' if 'paper' in host else 'PRODUCTION'})", ""]
    missing = [k for k in ("NOVIG_MGMT_KEY_ID", "NOVIG_MGMT_PEM", "NOVIG_TRADING_SEED") if not os.environ.get(k)]
    if missing:
        report.append(f"❌ Missing GitHub secrets: {', '.join(missing)}")
        Path(ROOT / "NOVIG_SETUP.md").write_text("\n".join(report) + "\n")
        print("\n".join(report))
        sys.exit(1)
    mgmt = novig.Client(host, novig.Key.from_pem(os.environ["NOVIG_MGMT_KEY_ID"].strip(),
                                                 os.environ["NOVIG_MGMT_PEM"].strip() + "\n"))
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    def step(name, fn):
        try:
            out = fn()
            report.append(f"✅ {name}")
            return out
        except Exception as e:
            report.append(f"❌ {name}: {e}")
            raise

    try:
        step("Management key signature (echo)", lambda: mgmt.call("POST", "/v3/echo", body={"hello": "sharper-sharp"}))
        subs = step("List subaccounts", lambda: mgmt.call("GET", "/v3/account/subaccounts")) or []
        trading_seed = os.environ["NOVIG_TRADING_SEED"].strip()
        probe = novig.Key.from_seed("pending", trading_seed)
        key_id = state.get("trading_key_id")
        if key_id and any(s.get("keyId") == key_id for s in subs):
            report.append(f"✅ Subaccount already open (trading key {key_id})")
        else:
            opened = step("Open subaccount + register trading key", lambda: mgmt.call(
                "POST", "/v3/account/subaccounts",
                body={"label": LABEL, "publicKey": probe.public_pem(), "algorithm": "Ed25519"}))
            key_id = opened["keyId"]
            state.update({"trading_key_id": key_id, "label": LABEL, "host": host})
            STATE.parent.mkdir(exist_ok=True)
            STATE.write_text(json.dumps(state, indent=1))
            report.append(f"   Trading key ID (public): `{key_id}`")
        bal = mgmt.call("GET", f"/v3/account/subaccounts/{key_id}/balance") or {}
        if float(bal.get("balance") or 0) < 100:
            step(f"Fund subaccount with ${FUND_USD} play money", lambda: mgmt.call(
                "POST", f"/v3/account/subaccounts/{key_id}/transfer",
                body={"direction": "fund", "amount": FUND_USD, "clientTransferId": f"setup-{key_id[:8]}"}))
            bal = mgmt.call("GET", f"/v3/account/subaccounts/{key_id}/balance") or {}
        report.append(f"   Subaccount balance: ${bal.get('balance')}")
        trader = novig.Client(host, novig.Key.from_seed(key_id, trading_seed))
        step("Trading key signature (echo)", lambda: trader.call("POST", "/v3/echo", body={"ping": 1}))
        live = trader.call("GET", "/v3/types/markets/live") or {}
        wanted = ["NBA", "NHL", "NCAAB", "ATP", "WTA", "League of Legends", "Dota 2"]
        report += ["", "Live MONEY markets offered: " + ", ".join(l for l in wanted if "MONEY" in (live.get(l) or []))]
        report += ["", "Setup complete. In-game signals will now place $100 paper orders on Novig.",
                   "You can delete the NOVIG_MGMT_PEM secret now if you like; trading only uses NOVIG_TRADING_SEED."]
    finally:
        (ROOT / "NOVIG_SETUP.md").write_text("\n".join(report) + "\n")
        print("\n".join(report))


if __name__ == "__main__":
    main()
