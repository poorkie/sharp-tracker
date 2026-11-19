> ## Documentation Index
> Fetch the complete documentation index at: https://docs.novig.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Account model

> How your account, subaccounts, and keys fit together.

Trader

Management keyOpens, funds, and labels every subaccount. Can't place orders.

Subaccount A
Balance, positions, orders
Trading key

Subaccount B
Balance, positions, orders
Trading key

Each trading key reaches only its own subaccount, and that never changes.

| Part | What it is | Limit |
| - | - | - |
| Trader | You. Your trader ID never appears in the API | 1 |
| Subaccount | A separate wallet: balance, positions, orders | Up to 10 live |
| Key | A keypair. We store only the public half | By scope |

## Scopes

Each key has one scope, fixed when you create it.

| Scope | Reaches | Orders | Money | Limit |
| - | - | - | - | - |
| `management` | All subaccounts | No | Yes | 1 per trader |
| `management::read` | All subaccounts | No | No | 128 per trader |
| `trading` | One subaccount | Yes | No | 1 per subaccount |
| `trading::read` | One subaccount | No | No | 128 per trader |

* `management` opens, funds, and labels subaccounts, and creates and revokes keys. It also reads what `management::read` reads.
* `management::read` lists keys and subaccounts, and reads any balance, ledger, order, fill, and position.
* `trading` places and cancels orders on its subaccount, and reads the catalog.
* `trading::read` reads its subaccount's orders, fills, positions, and balance, and the catalog.

| Scope | Create it with |
| - | - |
| `management` | [Profile → Settings → Novig API](https://docs.novig.com/api/api-keys.md#create-your-management-key) |
| `management::read` | `POST /v3/keys` |
| `trading` | `POST /v3/account/subaccounts` |
| `trading`, replacement | `POST /v3/account/subaccounts/{keyId}/keys` |
| `trading::read` | `POST /v3/account/subaccounts/{keyId}/keys` |

> [!WARNING]
>
> No key can both move money and place orders. A leaked trading key can lose its balance through trades, but it can't withdraw money or reach another subaccount.

## Address a subaccount by its trading key

Routes that act on a subaccount take its trading key ID as `{keyId}`, as in `GET /v3/account/subaccounts/{keyId}/balance`.

* A management key ID isn't an address, because it doesn't point at one subaccount. It returns a 400.
* A revoked trading key's ID still works as an address. Your management key can still reach the subaccount through it.
* The subaccount list has one row per live subaccount. Each row's `keyId` is that subaccount's trading key.

## Read every wallet with a management key

A `management` or `management::read` key reads the same REST routes as a `trading::read` key.
It still can't place or cancel an order. Those routes return 403 `SCOPE_INSUFFICIENT`.

The catalog and the market and event history look the same to every key.
Routes that read your own orders, fills, and positions return an object keyed by key ID instead.
Each value is the body a trading key gets for that wallet.

```json theme={"dark"}
{
  "3b6f0c2e-…": [],
  "8f14e45f-…": []
}
```

This example is `GET /v3/portfolio/positions` for a trader with one subaccount.

* Your cash wallet appears under the ID of the management key that signed the request.
* Each subaccount appears under its `trading` key ID.
* If you revoked that `trading` key, the subaccount appears under its oldest live key instead.
* If you revoked every key of a subaccount, it appears under its newest revoked key.
* A closed subaccount still appears, so you can read its orders, fills, and positions.
* The subaccount list and `GET /v3/account/subaccounts/{keyId}/balance` still leave a closed subaccount out.
* A trading key's response doesn't change. It's never wrapped in an object.

### Read one wallet

Add `?keyId=` to read one wallet. The response is the same object, with one entry.

* The ID of the key that signed the request reads your cash wallet.
* Any key ID of a subaccount reads that subaccount, even a revoked key or a closed subaccount's key.
* An unknown key ID, or another trader's, returns 404 `SUBACCOUNT_NOT_FOUND`.
* A trading key that sends `keyId` gets 403 `SCOPE_INSUFFICIENT`.

### Pages, single items, and snapshots

* To read past the first page, send `keyId` with `after`. When the read covers more than one wallet, `after` without `keyId` returns 400 `AFTER_NEEDS_KEY_ID`.
* `GET /v3/orders/{id}` and `GET /v3/history/positions/{id}` return one entry: the wallet that holds the ID.
* `GET /v3/account/orders` and `GET /v3/account/positions` send no `ETag` to a management key, and ignore `If-None-Match`.
* The WebSocket at `GET /v3/ws` still takes only a `trading` or `trading::read` key.

## Recommended setup

* Use one subaccount per strategy, so a cancel-all stops only that strategy.
* Use one subaccount per process, so you can revoke a compromised host without stopping the others.
* Keep your one management key in a secret manager, never in a running process. It's the only key that moves money.


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.