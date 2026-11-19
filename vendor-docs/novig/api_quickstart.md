> ## Documentation Index
> Fetch the complete documentation index at: https://docs.novig.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Quickstart

> Go from a management key to a resting order in five calls.

This page takes you from a management key to your first order on Paper, our play-money exchange.

Run the five steps in order, in one script.

| Step | Call | Signed by |
| - | - | - |
| 1 | `POST /v3/echo` | `management` |
| 2 | `POST /v3/account/subaccounts` | `management` |
| 3 | `POST /v3/account/subaccounts/{keyId}/transfer` | `management` |
| 4 | `GET  /v3/catalog/markets` | `trading` |
| 5 | `POST /v3/orders` | `trading` |

## Set up

Create a management key on [Profile](https://paper.novig.com/Profile/ProfileScreen) → Settings → Novig API, on paper.novig.com.
If you have no Paper account yet, [set one up](https://docs.novig.com/api/environments.md#set-up-a-paper-account) first.
[Get a key](https://docs.novig.com/api/api-keys.md) walks you through it.

Then point the samples at Paper and at your key:

```bash theme={"dark"}
export NOVIG_HOST=https://api.paper.novig.com
export NOVIG_KEY_ID=<your management key ID>
export NOVIG_PEM=novig-api-key-<nickname>.pem
```

The Python samples need two packages: `pip install requests cryptography`.

The Node samples need Node 18 or later and no packages.

## Save the signer

Save this helper as `novig.py` or `novig.mjs`.
Its `call` function signs a request, sends it, and returns the parsed reply.

The signature covers `NOVIG-V3`, the timestamp, the method, the path, the query, and a hash of the body.
[Sign a request](https://docs.novig.com/api/signing.md) explains each rule.

```python Python theme={"dark"}
import base64, hashlib, json, os, time

import requests
from cryptography.hazmat.primitives.serialization import (
    load_pem_private_key,
)

HOST = os.environ["NOVIG_HOST"]

def load(key_id, pem):
    with open(pem, "rb") as f:
        return key_id, load_pem_private_key(f.read(), None)

def call(key, method, path, query="", body=None):
    key_id, private = key
    data = b"" if body is None else json.dumps(body).encode()
    ts = str(int(time.time() * 1000))
    digest = hashlib.sha256(data).hexdigest()
    text = "\n".join(["NOVIG-V3", ts, method, path, query, digest])
    signature = base64.b64encode(private.sign(text.encode()))
    url = HOST + path + (f"?{query}" if query else "")
    return requests.request(method, url, data=data, headers={
        "Novig-Key-Id": key_id,
        "Novig-Timestamp": ts,
        "Novig-Signature": signature.decode(),
        "Content-Type": "application/json",
    }).json()
```

```javascript Node.js theme={"dark"}
import { createHash, createPrivateKey, sign } from "node:crypto";
import { readFileSync } from "node:fs";

const HOST = process.env.NOVIG_HOST;

export function load(keyId, pem) {
  return { keyId, key: createPrivateKey(readFileSync(pem)) };
}

export async function call(key, method, path, query = "", body) {
  const data = body === undefined ? "" : JSON.stringify(body);
  const ts = Date.now().toString();
  const digest = createHash("sha256").update(data).digest("hex");
  const text = ["NOVIG-V3", ts, method, path, query, digest];
  const signature = sign(null, Buffer.from(text.join("\n")), key.key);
  const url = HOST + path + (query ? `?${query}` : "");
  const res = await fetch(url, {
    method,
    body: data || undefined,
    headers: {
      "Novig-Key-Id": key.keyId,
      "Novig-Timestamp": ts,
      "Novig-Signature": signature.toString("base64"),
      "Content-Type": "application/json",
    },
  });
  return res.json();
}
```

* Pass `query` in [canonical form](https://docs.novig.com/api/signing.md#the-canonical-query): parameters sorted by name and percent-encoded.
* With a P-256 key, the Node signer works as is. In Python, sign with `private.sign(text.encode(), ECDSA(SHA256()))`.
* For Rust, use the complete signer on [Sign a request](https://docs.novig.com/api/signing.md#sign-in-rust).

## 1. Test your signature

Send a body to `POST /v3/echo`:

```python Python theme={"dark"}
import os

from novig import call, load

management = load(os.environ["NOVIG_KEY_ID"],
                  os.environ["NOVIG_PEM"])
print(call(management, "POST", "/v3/echo", body={"hello": "world"}))
```

```javascript Node.js theme={"dark"}
import { readFileSync } from "node:fs";
import { call, load } from "./novig.mjs";

const { NOVIG_KEY_ID, NOVIG_PEM } = process.env;
const management = load(NOVIG_KEY_ID, NOVIG_PEM);
const body = { hello: "world" };
console.log(await call(management, "POST", "/v3/echo", "", body));
```

Echo returns your body unchanged.
A `200` means your host, key, clock, and signature are all correct.

A `401` means one of them is wrong. [Troubleshooting](https://docs.novig.com/api/troubleshooting) maps each error message to a fix.

## 2. Open a subaccount

Each subaccount trades with its own key. Generate a keypair for it:

```bash theme={"dark"}
openssl genpkey -algorithm ed25519 -out desk-1.pem
openssl pkey -in desk-1.pem -pubout -out desk-1.pub.pem
```

macOS ships LibreSSL, which can't generate an Ed25519 key. Use OpenSSL 3 instead: `brew install openssl`.

Then send the public key to open the subaccount:

```python Python theme={"dark"}
opened = call(management, "POST", "/v3/account/subaccounts", body={
    "label": "desk-1",
    "publicKey": open("desk-1.pub.pem").read(),
    "algorithm": "Ed25519",
})
desk = load(opened["keyId"], "desk-1.pem")
```

```javascript Node.js theme={"dark"}
const opened = await call(
  management, "POST", "/v3/account/subaccounts", "", {
    label: "desk-1",
    publicKey: readFileSync("desk-1.pub.pem", "utf8"),
    algorithm: "Ed25519",
  },
);
const desk = load(opened.keyId, "desk-1.pem");
```

The `keyId` in the response identifies both the new trading key and the subaccount.

## 3. Fund the subaccount

Move \$10 from your account into the subaccount:

```python Python theme={"dark"}
path = f"/v3/account/subaccounts/{opened['keyId']}/transfer"
call(management, "POST", path, body={
    "direction": "fund", "amount": "10.00000",
    "clientTransferId": "desk-1-first-fund",
})
```

```javascript Node.js theme={"dark"}
const path = `/v3/account/subaccounts/${opened.keyId}/transfer`;
await call(management, "POST", path, "", {
  direction: "fund", amount: "10.00000",
  clientTransferId: "desk-1-first-fund",
});
```

A `202` means we accepted the transfer, not that the money moved.
Before you place an order, [check that its status](https://docs.novig.com/api/subaccounts/funding.md#check-the-transfer-status) is `Applied`.

## 4. Find a market

From here on, use the trading key.

List five NFL markets and take the first outcome:

```python Python theme={"dark"}
markets = call(desk, "GET", "/v3/catalog/markets",
               "league=NFL&limit=5")["items"]
outcome = markets[0]["outcomes"][0]["outcomeId"]
```

```javascript Node.js theme={"dark"}
const { items } = await call(desk, "GET", "/v3/catalog/markets",
  "league=NFL&limit=5");
const outcome = items[0].outcomes[0].outcomeId;
```

Any key can read the catalog.
[Catalog](https://docs.novig.com/api/catalog) lists every filter.

## 5. Place an order

Bid on that outcome:

```python Python theme={"dark"}
placed = call(desk, "POST", "/v3/orders", body={
    "outcomeId": outcome, "price": "0.050", "qty": 10, "tif": "GTC",
})
print(placed["orderId"])
```

```javascript Node.js theme={"dark"}
const placed = await call(desk, "POST", "/v3/orders", "", {
  outcomeId: outcome, price: "0.050", qty: 10, tif: "GTC",
});
console.log(placed.orderId);
```

This order bids 10 contracts at `0.050`, which costs \$0.005.

A `201` means we accepted the order, not that it's on the book.
The order rests when the `open` event arrives on the [private stream](https://docs.novig.com/api/streaming/private.md).

## Next

| Page | Covers |
| - | - |
| [Private stream](https://docs.novig.com/api/streaming/private.md) | Watch the order rest, fill, and cancel |
| [Orders](https://docs.novig.com/api/execution/orders) | Cancel, batch, and time in force |
| [Throttling](https://docs.novig.com/api/throttling.md) | Pace your requests |


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.