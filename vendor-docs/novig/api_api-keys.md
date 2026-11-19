> ## Documentation Index
> Fetch the complete documentation index at: https://docs.novig.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Get a key

> Generate a keypair, register its public half, and see what each key can do.

You use two kinds of key.

Your first key is the management key. You create it on Profile in the web app.
If we sent you a client ID and secret instead, [create it with those](#create-it-with-a-client-id-and-secret).
It runs your account but never places an order.
It also creates every trading key, one per subaccount.

We receive only the public half of each keypair.

| Key | Scope | Reaches | Orders | Money | Live at once |
| - | - | - | - | - | - |
| Management | `management` | The whole account | No | Yes | 1 per trader |
| Trading | `trading` | One subaccount | Yes | No | 1 per subaccount |

* The management key opens, funds, and labels subaccounts. It also creates and revokes keys.
* The management key [reads every wallet](https://docs.novig.com/api/concepts/account-model.md#read-every-wallet-with-a-management-key): orders, fills, and positions.
* A trading key places and cancels orders. It also streams the order book and your fills.
* Any key reads the catalog and market history.
* You create the management key on [Profile → Settings → Novig API](#create-your-management-key).
* You create a trading key with `POST /v3/account/subaccounts`, signed by the management key.

In these docs, the management key file is `novig-api-key-mgmt-1.pem` and a trading key file is `desk-1.pem`.

These routes manage keys:

| Route | Keys | Rate limit | Success |
| - | - | - | - |
| `POST /v3/keys` | `management` | `account` | 201 |
| `GET /v3/keys` | `management` `management::read` | `account` | 200 |
| `GET /v3/keys/{id}` | `management` `management::read` | `account` | 200 |
| `DELETE /v3/keys/{id}` | `management` | `account` | 204 |

## Create your management key

**1. Open the Novig API screen**

Open Profile on [paper.novig.com](https://paper.novig.com/Profile/ProfileScreen) or on [novig.com](https://novig.com/Profile/ProfileScreen), then press **Settings** and **Novig API**. The screen lists your keys and shows your [location check](#location-checks) result.

**2. Name the key**

Press **Create Key** and enter a nickname, which is only a label and grants no access. The browser generates the keypair with Ed25519, or with P-256 if it can't generate Ed25519.

**3. Confirm**

Press **Confirm Key Creation**. The browser downloads `novig-api-key-<nickname>.pem`, a PKCS#8 PEM file, and shows the private key once.

**4. Save the private key**

If the browser blocked the download, copy the key from the screen. Then press **I've saved my key**, and we never show the private key again.

> [!WARNING]
>
> Store the private key in a secret manager before you press **I've saved my key**. We never hold the private key, so we can't show it again.

The key ID shown beside the private key identifies your management key.
Send it in the `Novig-Key-Id` header.

If you lose the private key, revoke the key on the same screen and create a new one.
Your web session is the only recovery path.
Until you revoke the key, anyone who holds the private key can act as that key.

### Create it with a client ID and secret

Some accounts have no web login. We onboard them with a client ID and a client secret.

> [!WARNING]
>
> **Use the client ID and secret only to create your management key.** They are v2 credentials. Build your integration on v3, not on the v2 routes they also open.

**1. Get a token**

Exchange the client ID and secret for a bearer token.

**2. Generate a keypair**

Create an Ed25519 keypair on your machine, and keep the private half.

**3. Register the public half**

Call `POST /v3/keys` with the token as the bearer. The reply is your management key.

Point `NOVIG_HOST` at Production, `https://api.novig.com`, then request a token:

```bash theme={"dark"}
curl -s -X POST "$NOVIG_HOST/nbx/v1/auth/emm-token" \
  -H 'content-type: application/json' \
  -d '{"grant_type": "client_credentials", "client_id": "YOUR_CLIENT_ID", "client_secret": "YOUR_CLIENT_SECRET"}'
```

The reply carries `access_token`. Export it as `TOKEN`. It expires after 30 minutes.

Generate the keypair:

```bash theme={"dark"}
openssl genpkey -algorithm ed25519 -out novig-mgmt.pem
chmod 600 novig-mgmt.pem
openssl pkey -in novig-mgmt.pem -pubout -out novig-mgmt.pub.pem
```

Register the public half:

```bash theme={"dark"}
curl -i -X POST "$NOVIG_HOST/v3/keys" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d "$(jq -n --arg name "mgmt-1" --arg publicKey "$(cat novig-mgmt.pub.pem)" \
        '{name: $name, publicKey: $publicKey, algorithm: "Ed25519"}')"
```

A 201 reply carries your `keyId` and `"scope": "management"`. Save the `keyId` with `novig-mgmt.pem`, because every signed call needs both.

A `409 MANAGEMENT_KEY_EXISTS` means your account already holds a live management key. Use that key, or ask us to revoke it.

If the reply says the client-credentials token path is not enabled for this account, ask us to turn it on.

You don't need the token again. Sign account-management calls with your management key. Use your subaccount's trading key for market data and orders. Start with the [Quickstart](https://docs.novig.com/api/quickstart.md).

## Create other keys with the API

A route creates every key below `management`.
Generate the keypair before you call the route:

```bash theme={"dark"}
openssl genpkey -algorithm ed25519 -out <nickname>.pem
chmod 600 <nickname>.pem
openssl pkey -in <nickname>.pem -pubout -out <nickname>.pub.pem
```

Then send the public key in the request body:

| Field | Type | Required |
| - | - | - |
| `name` | `string` | Yes |
| `publicKey` | `string` | Yes |
| `algorithm` | `Ed25519` or `P-256` | Yes |
| `expiresAt` | `date-time` | No |

* `name` is a label that appears in the key list.
* `publicKey` is the public key as an SPKI PEM, newlines included.
* `algorithm` must match the key material.
* `expiresAt` must be strictly in the future. Never set it on a `management` key, as [Revoke a key](#revoke-a-key) explains.

`POST /v3/account/subaccounts` takes `label` in place of `name`.

`POST /v3/keys` and `POST /v3/account/subaccounts` take no `scope` field.
If the body includes one, the route returns 400.
`POST /v3/account/subaccounts/{keyId}/keys` requires one: `trading` or `trading::read`.

The route returns the new key:

| Field | What it is |
| - | - |
| `keyId` | The value for the `Novig-Key-Id` header. Not a secret |
| `fingerprint` | `sha256:` and the hex SHA-256 of the public key's SPKI DER |

Pick Ed25519 unless a hardware requirement forces P-256.
Your signer must use the same algorithm.

To compute the fingerprint yourself, run:

```bash theme={"dark"}
openssl pkey -pubin -in <nickname>.pub.pem -outform DER | openssl dgst -sha256
```

## How a key gets its scope

Each key has a scope, which sets what it can reach and do.
Where you create a key decides its scope, so you never name one yourself.

Profile creates the `management` key and generates its keypair in the browser.
A route creates every other key from a public key that you generate.

Web sessionA bearer JWTProfile → Settings → Novig API

managementOne live per traderCreates the rest with a route

management::read128 live per tradertradingOne live per subaccounttrading::read128 live per trader

Each line points from a credential to the key it creates.

| Scope | Bound to | Access |
| - | - | - |
| `management` | Trader | Write |
| `management::read` | Trader | Read |
| `trading` | Subaccount wallet | Write |
| `trading::read` | Subaccount wallet | Read |

| Scope | Create it with |
| - | - |
| `management` | [Profile](#create-your-management-key), from your web session |
| `management::read` | `POST /v3/keys` |
| `trading` | `POST /v3/account/subaccounts` |
| `trading`, replacement | `POST /v3/account/subaccounts/{keyId}/keys` |
| `trading::read` | `POST /v3/account/subaccounts/{keyId}/keys` |

The route that opens a subaccount also creates its `trading` key.
A subaccount has one live `trading` key.
After you revoke it, `POST /v3/account/subaccounts/{keyId}/keys` creates its replacement.
The same route creates `trading::read` keys beside it, up to 128 live per trader across every subaccount.
Every key on a subaccount shares the subaccount's [throttles](https://docs.novig.com/api/throttling.md#keys-that-share-throttles).

### Why keys work this way

* Your web session is the root. Its bearer `JWT` is the only credential you hold before your first key, and the only way to a `management` key.
* Every key has its own keypair.
* No key creates a key above itself. Every route that creates a key accepts only a `management` key or your web session, so a leaked read or trading key can't escalate.
* A subaccount and its `trading` key are created together, so no wallet exists without a key. After you revoke that key, `POST /v3/account/subaccounts/{keyId}/keys` creates its replacement.

## Limits

| Limit | Value | Over the limit |
| - | - | - |
| Live `management` keys per trader | 1 | 409 |
| Live `management::read` keys per trader | 128 | 400 |
| Live subaccounts per trader | 10 | 400 |
| Live `trading` keys per subaccount | 1 | 409 |
| Live `trading::read` keys per trader | 128 | 400 |
| Uses of one public key across Novig | 1 | 409 |
| Signed body size | Capped | 403 from the edge |

## Location checks

Every signed route runs two location checks.
If either check fails, the route returns `451`. A cancel, `GET /v3/time`, and `GET /v3/exchange/status` skip both.

* **IP check:** refuses a request from a restricted state, or through a VPN, proxy, or Tor exit. A data-center address is fine.
* **Companion check:** refuses a key holder whose last device geolocation is missing, failed, named no region, or came from a restricted state. A placement also refuses one older than 3 days. A read admits it.

The restricted states are Arizona, Michigan, and Nevada.

The newest check on any of the key holder's devices decides.
A failed check blocks the key until a check passes again.
If the check failed only because it couldn't confirm the location, the last pass from the past 3 days still counts.

Some failures name their cause: `GEOLOCATION_OUT_OF_BOUNDARY`, `GEOLOCATION_BLOCKED_NETWORK`, or `GEOLOCATION_BLOCKED_SOFTWARE`.
[451](https://docs.novig.com/api/errors.md#451-geolocation-unavailable) gives the fix for each.

To pass the companion window check, open the Novig app so your device geolocates.

The Novig API screen shows your check result before you create your first key.
If a window is open, the screen also shows when it expires.

## Revoke a key

To revoke a key, press **×** beside it on Profile → Settings → Novig API.
You can also call `DELETE /v3/keys/{id}`.

Revocation takes effect within 60s, and you can't undo it.

> [!WARNING]
>
> Revoking a key leaves its resting orders on the book. Cancel them before you revoke the key.

If the key is compromised, change the order.
Revoke it first, then create a replacement, then cancel the resting orders.

> [!WARNING]
>
> An expired `management` key still takes the only `management` slot, and it can't sign its own revocation. Never set `expiresAt` on a `management` key.

## Rotate a key

* **`management`:** revoke the old key, then create a new one. There's a gap with no key.
* **`management::read`:** create the new key, switch over, then revoke the old one. There's no gap.
* **`trading`:** revoke, then create the replacement with `POST /v3/account/subaccounts/{keyId}/keys`. There's a gap, so cancel orders first.


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.