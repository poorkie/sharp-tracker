> ## Documentation Index
> Fetch the complete documentation index at: https://docs.novig.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Private

> Your own orders and positions: open, fill, cancel, reject.

The private stream carries your own orders and positions on two channels.
The `orders` channel costs 1 token. The `positions` channel costs 1 token.

Name each channel in the `private` field of a subscribe.
To drop both, unsubscribe the subject `PRIVATE`.

Each channel covers only the subaccount of the key that signed the handshake.

```json theme={"dark"}
{
  "nonce": 3,
  "subscribe": {
    "private": ["orders"]
  }
}
```

To follow an order from start to finish, see [Track order state](https://docs.novig.com/api/concepts/order-lifecycle.md#track-order-state).

## Snapshot

```json theme={"dark"}
{
  "ts": 1757894400123,
  "nonce": 3,
  "subscribed": {
    "markets": {},
    "events": {},
    "private": ["orders"]
  },
  "snapshot": {},
  "orders": {
    "seq": 907,
    "open": [
      {
        "orderId": "7c9e6679-…",
        "clientId": "0b3f5f20-…",
        "marketId": "6f9619ff-…",
        "outcomeId": "3f2504e0-…",
        "price": "0.665",
        "qty": 110,
        "originalQty": 110,
        "tif": "GTC"
      }
    ]
  }
}
```

`orders.seq` counts the order events of your subaccount, not of one connection.
It's independent of every market's `seq`.

A `seq` of `0` means no event has occurred.

A server restart resets the `seq`. Never compare a `seq` across connections.

A subscribe that names only `private` gets an empty `snapshot`.

## Events

Events arrive in the `orders` field of a delta, as one batch with one `seq`.
Within a batch, an order's `fill` events come before its `open`, `cancel`, or `reject`.

Every event names the order's outcome, its limit price, and its `originalQty`, the number of contracts you placed.
You can read each event on its own, without matching it to an earlier one.

```json theme={"dark"}
{
  "ts": 1757894400456,
  "delta": {},
  "orders": {
    "seq": 908,
    "deltas": [
      {
        "kind": "cancel",
        "orderId": "7c9e6679-…",
        "outcomeId": "3f2504e0-…",
        "price": "0.665",
        "originalQty": 110,
        "remaining": 70
      }
    ]
  }
}
```

> [tab] open
>
> ```json theme={"dark"}
> {
>   "kind": "open",
>   "orderId": "7c9e6679-…",
>   "clientId": "0b3f5f20-…",
>   "marketId": "6f9619ff-…",
>   "outcomeId": "3f2504e0-…",
>   "price": "0.665",
>   "qty": 110,
>   "originalQty": 110,
>   "tif": "GTC"
> }
> ```
>
> The order rests. **This event, not the `201`, confirms that the order rests.**
>
> `price` is the order's limit price.
>
> `qty` is the resting quantity.
> It's lower than `originalQty` when part of the order filled before it rested.
>
> `clientId` appears only when the placement sent one.
> An order with an expiry also carries `expiresAt`, in Unix milliseconds.

> [tab] fill
>
> ```json theme={"dark"}
> {
>   "kind": "fill",
>   "fillId": "1e6a2f94-…",
>   "orderId": "7c9e6679-…",
>   "clientId": "0b3f5f20-…",
>   "outcomeId": "3f2504e0-…",
>   "price": "0.660",
>   "limit": "0.665",
>   "qty": 40,
>   "originalQty": 110,
>   "remaining": 70,
>   "taker": true
> }
> ```
>
> `fillId` matches the `fillId` from `GET /v3/portfolio/fills`.
> Two fills on one order always have different IDs, even at the same quantity.
>
> `taker` is `true` when your order took liquidity and `false` when it rested.
> It matches `taker` from `GET /v3/portfolio/fills`.
>
> `price` is the traded price. It can be better than `limit`, the order's limit price.
>
> `qty` is the size of this fill. `remaining` is what still rests after it.
>
> `remaining: 0` means the order is `FILLED`.
>
> This event carries no fee.
>
> A self-match, where your order trades with your own order, also sends a `fill` here. The `trades` channel omits it.

> [tab] cancel
>
> ```json theme={"dark"}
> {
>   "kind": "cancel",
>   "orderId": "7c9e6679-…",
>   "clientId": "0b3f5f20-…",
>   "outcomeId": "3f2504e0-…",
>   "limit": "0.665",
>   "originalQty": 110,
>   "remaining": 70,
>   "reason": "GO_LIVE"
> }
> ```
>
> `limit` is the order's limit price.
>
> `remaining` is what was still resting when the order left the book.
> `originalQty` minus `remaining` is the quantity that filled.
>
> The event carries `reason` only when the engine states one: `GO_LIVE`, `MARKET_CLOSED`, `SETTLED`, or `NEUTRALIZED`.
>
> A cancel by you, by expiry, or by an admin carries no `reason`.

> [tab] reject
>
> ```json theme={"dark"}
> {
>   "kind": "reject",
>   "orderId": "7c9e6679-…",
>   "clientId": "0b3f5f20-…",
>   "outcomeId": "3f2504e0-…",
>   "limit": "0.665",
>   "originalQty": 110
> }
> ```
>
> `limit` is the order's limit price.
>
> We accepted the order, then refused it. **No HTTP status reports this refusal.**
>
> An unfilled `IOC`, `FOK`, or `PO` order also arrives as `reject`.

## Positions

The snapshot lists your nonzero positions under `positions`.
In a delta, `positions` holds a `seq` and a `deltas` list of the positions that changed.

Positions already account for washes and neutralizations.

* A wash is a self-match: your order traded against another of yours, possibly in another subaccount. You get a `fill`, but your position doesn't change and nothing appears on `trades`.
* A neutralization is a cash-out. We cancel your resting orders in the market with `reason: "NEUTRALIZED"`, and may place a hedge order that closes your position.

```json theme={"dark"}
"positions": {
  "seq": 12,
  "positions": [
    {
      "marketId": "6f9619ff-…",
      "outcomeId": "3f2504e0-…",
      "qty": 110,
      "cost": "0.73150"
    }
  ]
}
```

| Field | Meaning |
| - | - |
| `qty` | Contracts held |
| `cost` | Net dollars paid, as a decimal string |
| `seq` | Per subaccount |

* A `qty` of `0` in a delta means the position closed.
* `100 × cost / qty` is the average price.
* The positions `seq` is independent of `orders.seq`.

## Slow consumer

A slow consumer is a client that reads too slowly, so its queue fills up.

| Frame | Sent | On a full queue |
| - | - | - |
| Replies, heartbeats | Before market frames | Waits for room |
| `orders`, `positions` | Before market frames | Pauses every delta |
| Market channels | After private frames | Pauses every delta |

During a pause, we send no deltas on any channel.
Once you've read what we queued, we send a snapshot of every subscription with no `nonce`.
Replace your state with it. Deltas resume after it.

A write that stalls for 15 s closes the connection with code `1008` and reason `SLOW_CONSUMER`.

> [!WARNING]
>
> After `SLOW_CONSUMER`, reconnect, subscribe again, and take the snapshot. Don't resume from the old `seq`.

## Recovery

Every 15 s, we send a `heartbeat` with your last `seq` on each private channel you subscribe to.
A quiet channel still reports its `seq`.

A heartbeat `seq` above your last `seq` means you lost a message.

```json theme={"dark"}
{
  "heartbeat": {
    "orders": 911,
    "ts": 1757894415123
  }
}
```

Recovery sequence. The client subscribes to private orders. The exchange replies with a snapshot at seq 907 with open orders. The client places an order with POST /v3/orders over REST and gets 201 with the orderId. The exchange sends a delta at seq 908 with open, then a delta at seq 909 with a fill, remaining 70. The connection drops. The client reconnects and subscribes to private orders again. The exchange replies with a snapshot at seq 911 with open orders. The client diffs open against its state on orderId.

Arrows are messages, top to bottom in time.

On a seq gap, or a heartbeat `seq` above your last `seq`, send `snapshot` with the channel in `private`.

```json theme={"dark"}
{
  "nonce": 12,
  "snapshot": {
    "private": ["orders"]
  }
}
```

* After a reconnect, subscribe and take the snapshot. Then diff `open` against your state on `orderId`.
* If you get no `201` for an order, match `clientId` against `open` and `fill` before you resend.

Without a connection, `GET /v3/account/orders` and `GET /v3/account/positions` return the same snapshots.
Each comes with its `seq` and an `ETag`. A matching `If-None-Match` gets `304`.
A management key gets the snapshots keyed by key ID, with no `ETag`.

## Data on other routes

| Data | Route |
| - | - |
| Fees, settlements | `GET /v3/account/subaccounts/{keyId}/transactions` |
| Filtered positions | `GET /v3/portfolio/positions` |
| Balance | `GET /v3/account/subaccounts/{keyId}/balance` |


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.