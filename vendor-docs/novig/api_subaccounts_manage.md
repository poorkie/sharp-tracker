> ## Documentation Index
> Fetch the complete documentation index at: https://docs.novig.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Open and manage

> Open a subaccount and its trading key. List subaccounts, read a balance, and change a label.

A subaccount is a separate wallet with its own balance, positions, orders, and trading key.
[Account model](https://docs.novig.com/api/concepts/account-model.md) explains how subaccounts and keys fit together.

| Route | Reply |
| - | - |
| `POST /v3/account/subaccounts` | 201 |
| `GET /v3/account/subaccounts` | 200 |
| `GET /v3/account/subaccounts/{keyId}/balance` | 200 |
| `PATCH /v3/account/subaccounts/{keyId}` | 200 404 |

All four routes count against the `account` [throttle](https://docs.novig.com/api/throttling.md).

## Open a subaccount

One call to `POST /v3/account/subaccounts` opens the subaccount, its wallet, and its one `trading` key.
Sign the call with your `management` key.

First, generate the trading keypair:

```bash theme={"dark"}
openssl genpkey -algorithm ed25519 -out desk-1.pem
openssl pkey -in desk-1.pem -pubout -out desk-1.pub.pem
```

macOS ships LibreSSL, which can't generate an Ed25519 key. Run these commands with OpenSSL 3 (`brew install openssl`).

Then send the public key in the body:

| Field | Type | Required |
| - | - | - |
| `label` | `string` | Yes |
| `publicKey` | `string` | Yes |
| `algorithm` | `Ed25519` or `P-256` | Yes |
| `expiresAt` | `date-time` | No |

* `label` holds up to 64 characters. It's for display only and doesn't have to be unique.
* `publicKey` is the SPKI PEM of a fresh keypair, newlines included.
* `algorithm` must match the key material.
* `expiresAt` must be strictly in the future.

> [tab] Request
>
> ```json theme={"dark"}
> {
>   "label": "desk-1",
>   "publicKey": "-----BEGIN PUBLIC KEY-----\n…\n-----END PUBLIC KEY-----\n",
>   "algorithm": "Ed25519"
> }
> ```

> [tab] 201
>
> ```json theme={"dark"}
> {
>   "keyId": "8f14e45f-…",
>   "label": "desk-1",
>   "balance": "0.00000"
> }
> ```

The `keyId` identifies the trading key.
It's also the subaccount's address. Store it beside `desk-1.pem`.

> [!WARNING]
>
> If you lose `desk-1.pem`, nothing can sign as this subaccount again. Your `management` key can still defund the subaccount and change its label.

Opening a subaccount has three requirements:

* You've passed KYC, our identity check, and aren't self-excluded. We check this here, on every transfer, and on every order you place.
* You call from an allowed location: no VPN or proxy, and not a restricted state.
* You have a free slot. An open past 10 live subaccounts fails with a 400 `ACCOUNT_RULE_REFUSED`.

## List subaccounts

`GET /v3/account/subaccounts` returns one row per live key on each subaccount, the oldest subaccount first.
A subaccount whose keys are all revoked has no row.
Sign it with a `management` or `management::read` key.

The route has no paging and no filter.

```json 200 theme={"dark"}
[
  {
    "keyId": "8f14e45f-…",
    "label": "desk-1",
    "balance": "1234.50000"
  },
  {
    "keyId": "b7c3a1e9-…",
    "label": "desk-2",
    "balance": "0.00000"
  }
]
```

## Read a balance

`GET /v3/account/subaccounts/{keyId}/balance` returns one subaccount's balance.

The `keyId` is the subaccount's trading key, not the key you sign with.
Put it in the path before you sign.

| Signed with | Reads |
| - | - |
| `management` | Any subaccount |
| `management::read` | Any subaccount |
| `trading` | Its own subaccount |
| `trading::read` | Its own subaccount |

A `trading` or `trading::read` key that reads another subaccount gets a 404 `SUBACCOUNT_NOT_FOUND`, as if the subaccount did not exist.

A trader holds up to 128 live `trading::read` keys across every subaccount.
A trader holds up to 128 live `management::read` keys.
Creating a read key past either cap returns a 400 `ACCOUNT_RULE_REFUSED`.

## Change a label

`PATCH /v3/account/subaccounts/{keyId}` changes a subaccount's label.
The label is the only part of a subaccount you can change. Sign the call with your `management` key.

The call moves no money and opens no position, so self-exclusion doesn't block it.

The call is idempotent: sending the same label twice makes one change.

```json theme={"dark"}
{
  "label": "desk-1-nba"
}
```

```json 200 theme={"dark"}
{
  "keyId": "8f14e45f-…",
  "label": "desk-1-nba",
  "balance": "1234.50000"
}
```


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.