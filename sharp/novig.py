"""Novig exchange client (API v3) for paper-trading the in-game signal.

Auth: every request is signed with NOVIG-V3 (Ed25519). The canonical string is
six lines joined by LF: NOVIG-V3, timestamp ms, METHOD, path, canonical query,
lowercase hex sha256 of the body. Signature = base64(ed25519(string)).

Keys:
  * management key: NOVIG_MGMT_KEY_ID + NOVIG_MGMT_PEM (only used by the
    one-time setup to open/fund the subaccount).
  * trading key: derived from NOVIG_TRADING_SEED (sha256 -> Ed25519 seed),
    so the private key never has to be stored anywhere but that secret. Its
    key ID is public and lives in state/novig.json.

Contracts pay 1 cent each, so a $100 stake at price p is round(10000 / p)
contracts. To buy an outcome you cross the best bid on the OTHER outcome:
ask(outcome) = 1 - best_bid(other).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from decimal import ROUND_CEILING, Decimal
from urllib.parse import quote, unquote

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log = logging.getLogger(__name__)

PAPER = "https://api.paper.novig.com"
PROD = "https://api.novig.com"

# Polymarket sport key -> Novig league
LEAGUES = {"nba": "NBA", "nhl": "NHL", "nfl": "NFL", "mlb": "MLB", "cbb": "NCAAB",
           "cfb": "NCAAF", "epl": "EPL", "atp": "ATP", "wta": "WTA",
           "lol": "League of Legends", "dota2": "Dota 2"}


# ---------------- signing ----------------

def _enc(s: str) -> str:
    return quote(s, safe="-._~")


def canonical_query(raw: str) -> str:
    if not raw:
        return ""
    pairs = []
    for part in raw.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append((_enc(unquote(k)), _enc(unquote(v))))
    pairs.sort(key=lambda kv: (kv[0].encode(), kv[1].encode()))
    return "&".join(f"{k}={v}" for k, v in pairs)


def canonical_string(ts: str, method: str, path: str, query: str, body: bytes) -> str:
    return "\n".join(["NOVIG-V3", ts, method.upper(), path, canonical_query(query),
                      hashlib.sha256(body).hexdigest()])


class Key:
    def __init__(self, key_id: str, private: Ed25519PrivateKey):
        self.id = key_id
        self.private = private

    @classmethod
    def from_pem(cls, key_id: str, pem: str) -> "Key":
        return cls(key_id, serialization.load_pem_private_key(pem.encode(), None))

    @classmethod
    def from_seed(cls, key_id: str, seed_text: str) -> "Key":
        seed = hashlib.sha256(("novig-trading:" + seed_text).encode()).digest()
        return cls(key_id, Ed25519PrivateKey.from_private_bytes(seed))

    def public_pem(self) -> str:
        return self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def headers(self, method: str, path: str, query: str, body: bytes) -> dict:
        ts = str(int(time.time() * 1000))
        sig = self.private.sign(canonical_string(ts, method, path, query, body).encode())
        return {"Novig-Key-Id": self.id, "Novig-Timestamp": ts,
                "Novig-Signature": base64.b64encode(sig).decode(),
                "Content-Type": "application/json"}


class NovigError(Exception):
    pass


class Client:
    def __init__(self, host: str = PAPER, key: Key | None = None, timeout: int = 20):
        self.host = host.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.s = requests.Session()

    def call(self, method: str, path: str, query: str = "", body=None, signed: bool = True):
        data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        q = canonical_query(query)
        url = self.host + path + (f"?{q}" if q else "")
        headers = (self.key.headers(method, path, q, data) if signed and self.key
                   else {"Content-Type": "application/json"})
        for attempt in range(3):
            r = self.s.request(method, url, data=data or None, headers=headers, timeout=self.timeout)
            if r.status_code == 429 and attempt < 2:
                time.sleep(float(r.headers.get("Retry-After", 1)))
                headers = (self.key.headers(method, path, q, data) if signed and self.key else headers)
                continue
            break
        if r.status_code >= 400:
            raise NovigError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    # public catalog (no key needed)
    def live_money_markets(self, league: str) -> list[dict]:
        out, after = [], None
        while True:
            q = f"league={league}&marketType=MONEY&eventStatus=OPEN_INGAME&limit=500"
            if after:
                q += f"&after={after}"
            page = self.call("GET", "/v3/public/catalog/markets", q, signed=False) or {}
            out += page.get("items") or []
            after = page.get("next")
            if not after or len(out) > 3000:
                return out

    def book(self, market_id: str) -> dict:
        return self.call("GET", f"/v3/public/catalog/markets/{market_id}/book", signed=False) or {}

    # trading (trading key)
    def place(self, outcome_id: str, price: str, qty: int, tif: str = "IOC") -> dict:
        return self.call("POST", "/v3/orders", body={
            "clientId": str(uuid.uuid4()), "outcomeId": outcome_id,
            "price": price, "qty": qty, "tif": tif})

    def fills_for_order(self, order_id: str) -> list[dict]:
        page = self.call("GET", "/v3/portfolio/fills", f"order={order_id}&limit=100") or {}
        return page.get("items") if isinstance(page, dict) else page


# ---------------- pricing helpers ----------------

def on_grid(p: float) -> str:
    """Round UP to the submittable grid (a buy limit must not be below the ask)."""
    d = Decimal(str(p))
    step = Decimal("0.001") if d <= Decimal("0.050") or d >= Decimal("0.950") else Decimal("0.005")
    v = (d / step).to_integral_value(rounding=ROUND_CEILING) * step
    v = min(max(v, Decimal("0.001")), Decimal("0.999"))
    return f"{v:.3f}"


def best_ask(book: dict, outcome_id: str) -> tuple[float, int] | None:
    """Price and contracts available to BUY outcome_id: cross the best bid on
    the other outcome."""
    orders = book.get("orders") or {}
    others = [o for oid, lst in orders.items() if oid != outcome_id for o in lst[:1]]
    if not others:
        return None
    top = max(others, key=lambda o: float(o["price"]))
    return round(1 - float(top["price"]), 4), int(top.get("qty") or 0)


def taker_fee_per_contract(price: float, coefficient: float = 0.06) -> float:
    """Dollars per 1-cent contract, as published: coefficient x P x (1 - P) cents."""
    return coefficient * price * (1 - price) * 0.01
