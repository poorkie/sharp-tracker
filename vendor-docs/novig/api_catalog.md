> ## Documentation Index
> Fetch the complete documentation index at: https://docs.novig.com/llms.txt
> Use this file to discover all available pages before exploring further.

# Catalog

> The events and markets you can trade, and how to find one game

Model: [Universe model](https://docs.novig.com/api/concepts/universe-model.md) · [Event lifecycle](https://docs.novig.com/api/concepts/event-lifecycle.md).

| Route | Key | Throttle | Reply |
| - | - | - | - |
| `GET /v3/catalog/events` `GET /v3/catalog/markets` | `any` | `read` | 200 |
| `GET /v3/catalog/participants` `GET /v3/catalog/consensus` | `any` | `read` | 200 |
| `GET /v3/catalog/{events,markets}/{id}` | `any` | `read` | 200 404 |
| `GET /v3/catalog/markets/{id}/book` `GET /v3/catalog/markets/{id}/trades` | `any` | `read` | 200 404 |
| `GET /v3/types/{leagues,sports,markets,event-statuses}` | `any` | `read` | 200 |
| `GET /v3/types/markets/live` | `any` | `read` | 200 |
| `GET /v3/history/markets` | `any` | `history` | 200 |
| `GET /v3/history/markets/{id}` | `any` | `history` | 200 404 |
| `GET /v3/history/events` | `any` | `history` | 200 |
| `GET /v3/history/events/{id}` | `any` | `history` | 200 404 |
| `GET /v3/history/trades` | `any` | `history` | 200 |
| `GET /v3/history/markets/{id}/candles` | `any` | `history` | 200 400 404 |

Any key reads these routes, a management key included.

> [tab] Request
>
> ```bash theme={"dark"}
> curl -s "$NOVIG_HOST/v3/catalog/markets?league=NFL&limit=5" \
>   -H "Novig-Key-Id: $TRADING_KEY_ID" \
>   -H "Novig-Timestamp: $TS" \
>   -H "Novig-Signature: $SIG"
> ```

> [tab] 200
>
> ```json theme={"dark"}
> {
> "items": [
> {
>   "marketId": "6f9619ff-…",
>   "eventId": "110e8400-…",
>   "marketType": "MONEY",
>   "status": "OPEN",
>   "voids": "PUSH",
>   "description": "Kansas City Chiefs at Buffalo Bills — Moneyline",
>   "startsTs": 1758400800000,
>   "fee": {
>     "coefficient": "0.06",
>     "makerCredit": "0.25",
>     "charged": "WHEN_LIVE"
>   },
>   "outcomes": [
>     {
>       "outcomeId": "3f2504e0-…",
>       "name": "Kansas City Chiefs",
>       "status": "TBD",
>       "subjectId": "9b2d7a10-…",
>       "last": "0.550",
>       "bid": {
>         "price": "0.540",
>         "qty": 400
>       }
>     },
>     {
>       "outcomeId": "5a1c9e73-…",
>       "name": "Buffalo Bills",
>       "status": "TBD",
>       "subjectId": "c4e8f2b3-…",
>       "last": "0.450",
>       "bid": {
>         "price": "0.440",
>         "qty": 250
>       }
>     }
>   ],
>   "volume": 128400,
>   "openInterest": 61200
> }
> ],
> "next": "9c5b94b1-…"
> }
> ```

A market with a line carries `strike`, a decimal string. A spread's line is the home side's handicap, so `"-3.5"` makes the home side a 3.5-point favorite. A moneyline has no line and omits `strike`.

`voids` says how the market settles if it voids: `PUSH` refunds every fill at its cost, and `FMV` settles each outcome at a fair price.

An outcome about one competitor carries that competitor's ID as `subjectId`. Its names are in [Participants](#participants).

## Filters

| Query | Type | |
| - | - | - |
| `league` | `string` | From `GET /v3/types/leagues`. Comma-separated. |
| `marketType` | `string` | From `GET /v3/types/markets`. Comma-separated. |
| `eventStatus` | `string` | From `GET /v3/types/event-statuses`. Comma-separated. |
| `event` | `uuid` | Every market of these events. Comma-separated. |
| `startsAfter` | `int64 ms` | Exclusive lower bound on the event's start. |
| `startsBefore` | `int64 ms` | Exclusive upper bound. |
| `limit` | `1–5000` | Defaults to `500`, not the maximum. Outside the range → 400. |
| `after` | `string` | The previous page's `next`. Opaque. Do not build or parse one. |

## Ticker fields

Each catalog market carries its live trading numbers. They come from the exchange's own state, so they are current when you read them.

| Field | On | Holds |
| - | - | - |
| `volume` | Market | Contracts traded, all time |
| `openInterest` | Market | Contracts outstanding |
| `last` | Outcome | The price of the market's latest trade on this outcome |
| `bid` | Outcome | The best bid's `price`, and the total `qty` resting there |

`last` is the taker's price on the outcome the taker bought, and `1 − price` on the other outcome. It matches the close of the market's latest candle.

`openInterest` counts each contract once. We net each wallet's own positions, so a wallet that holds both outcomes counts only the difference.

The book holds only bids. To buy an outcome, you cross the best bid on the other outcome, so the ask on an outcome is `1 − bid` on the other.

An outcome with no fill yet has no `last`. An outcome with no resting bid has no `bid`.

## Participants

`GET /v3/catalog/participants` lists each event's competitors and players. A competitor is a team, or one side in a sport like tennis.

Each competitor and player carries the IDs of the markets about it, so you can map a market without reading its `description`.

> [tab] Request
>
> ```bash theme={"dark"}
> curl -s "$NOVIG_HOST/v3/catalog/participants?league=NFL" \
>   -H "Novig-Key-Id: $TRADING_KEY_ID" \
>   -H "Novig-Timestamp: $TS" \
>   -H "Novig-Signature: $SIG"
> ```

> [tab] 200
>
> ```json theme={"dark"}
> {
> "items": [
> {
>   "eventId": "110e8400-…",
>   "homeCompetitorId": "9b2d7a10-…",
>   "awayCompetitorId": "c4e8f2b3-…",
>   "competitors": [
>     {
>       "competitorId": "9b2d7a10-…",
>       "name": "Kansas City Chiefs",
>       "shortName": "Chiefs",
>       "symbol": "KC",
>       "marketIds": []
>     },
>     {
>       "competitorId": "c4e8f2b3-…",
>       "name": "Buffalo Bills",
>       "shortName": "Bills",
>       "symbol": "BUF",
>       "marketIds": ["7d1e44a2-…"]
>     }
>   ],
>   "players": [
>     {
>       "playerId": "e1f0c9d8-…",
>       "name": "Patrick Mahomes",
>       "position": "QB",
>       "competitorId": "9b2d7a10-…",
>       "marketIds": ["2c6a9f51-…"]
>     }
>   ]
> }
> ],
> "next": "9c5b94b1-…"
> }
> ```

| Field | Holds |
| - | - |
| `homeCompetitorId` | The home side of a game |
| `awayCompetitorId` | The away side of a game |
| `competitors` | Both sides, and each competitor a market is about |
| `players` | Each player a market is about |
| `marketIds` | The markets about that competitor or player alone |

A future isn't a game, so it has no home or away side.

A market on the whole game, like a moneyline or a total, is about neither side. It lists under no competitor.

A competitor or a player keeps its ID from event to event.

A player's `competitorId` is the team they play for now, which may not be either side of the event.

A competitor or a player we hold no details for still lists, by its ID alone.

The route takes the filters of `GET /v3/catalog/events`. It also takes `event`, a comma-separated list of event IDs.

## Consensus markets

A market family is the markets of one event and one market type that are about the same competitor or player, or about neither. Its markets differ only by `strike`.

The consensus market is the family's main line. `GET /v3/catalog/consensus` lists it for each family, at most once per page.

```json theme={"dark"}
{
  "items": [
    {
      "marketId": "2c6a9f51-…",
      "eventId": "110e8400-…",
      "marketType": "PASSING_YARDS",
      "playerId": "e1f0c9d8-…"
    }
  ],
  "next": "9c5b94b1-…"
}
```

A row carries `playerId` or `competitorId` when its family is about one.

You can narrow the list by `league`, `marketType`, and `event`.

The main line can move to another market. While it moves, a family can show its old line on one page and its new line on the next. Read the list again to see the change.

## Markets that trade live

Once an event goes live, we accept orders only on some market types. `GET /v3/types/markets/live` lists them for each league.

```json theme={"dark"}
{
  "NBA": [
    "MONEY",
    "SPREAD",
    "TOTAL",
    "MONEYLINE_3_WAY_WIN",
    "MONEYLINE_3_WAY_DRAW",
    "TO_ADVANCE",
    "TEAM_TOTAL",
    "THREE_POINTERS_MADE",
    "POINTS",
    "REBOUNDS",
    "POINTS_REBOUNDS_ASSISTS",
    "POINTS_ASSISTS",
    "POINTS_REBOUNDS",
    "REBOUNDS_ASSISTS",
    "ASSISTS"
  ]
}
```

Every league lists the main lines, even a type the league doesn't offer. Some leagues add player or game props.

An order on any other market of a live event returns `NOT_LIVE_TRADABLE`. See [Errors](https://docs.novig.com/api/errors.md).

The list changes only when we release. It also answers without a key at `/v3/public/types/markets/live`, where a reply can be up to two hours old.

## Market history

A market leaves the catalog when it closes. The history routes return every market of any age and status, so you can still read its grade.

* [List market history](https://docs.novig.com/api-reference/history/list-market-history.md) pages through every market, newest first. It takes `limit` and `after`, like the catalog.
* [Get a market from history](https://docs.novig.com/api-reference/history/get-a-market-from-history.md) returns one market by ID.

A graded outcome carries its grade in `status`. A settled market also carries `settledTs`, the time of its most recent settlement. Both routes spend the `history` throttle, and a larger page costs more.

### Fetch new settlements

To fetch only the markets that settled since your last sync, pass `settledAfter`. The page then lists the most recent settlement first.

```bash theme={"dark"}
curl -s "$NOVIG_HOST/v3/history/markets?settledAfter=1758400800000" \
  -H "Novig-Key-Id: $TRADING_KEY_ID" \
  -H "Novig-Timestamp: $TS" \
  -H "Novig-Signature: $SIG"
```

`settledAfter` is in Unix milliseconds and excludes the time you pass. Page through the results with `after`, as usual.

Save the highest `settledTs` you receive, and pass it as `settledAfter` on your next sync.

If we re-settle a market, its `settledTs` moves forward, and your next sync returns it with the new grade.

## Event history

An event leaves the catalog when its last market closes. The event history routes return every event of any age and status.

* [List event history](https://docs.novig.com/api-reference/history/list-event-history.md) pages through every event, newest first. It takes `limit` and `after`, like the catalog.
* [Get an event from history](https://docs.novig.com/api-reference/history/get-an-event-from-history.md) returns one event by ID.

A history market's `eventId` names its event here. Both routes spend the `history` throttle, and a larger page costs more.

## Trade history

`GET /v3/history/trades` lists trades across every market, newest first. Each row is one trade, from the taker's side: the outcome the taker bought, the average price the taker paid, and the quantity.

The other outcome traded at `1 − price`. A wash, where your order trades against another of yours, doesn't appear.

| Query | Type | |
| - | - | - |
| `event` | `uuid` | Trades on one event's markets. |
| `market` | `uuid` | Trades on one market. |
| `outcome` | `uuid` | Trades whose taker bought this outcome. |
| `startsAfter` | `int64 ms` | Exclusive lower bound on the trade time. |
| `startsBefore` | `int64 ms` | Exclusive upper bound. |
| `limit`, `after` | | Page through the results as in the catalog. |

You can combine the filters. The `market` must belong to the `event`, and the `outcome` to the `market`, or the page comes back empty.

`tradeId` is the `fillId` of the taker's fill. Its timestamp is `ts`, so you can page by time.

The tape reports the same trade from the resting side, with one print for each resting order.
Each print carries this ID as `takeId`.
`makeIds` lists the `tradeId` of each of those prints, oldest first, so you can join from either side.

## Candles

`GET /v3/history/markets/{id}/candles` returns price candles for one market, open or settled.

```bash theme={"dark"}
curl -s "$NOVIG_HOST/v3/history/markets/$MARKET_ID/candles?interval=1h" \
  -H "Novig-Key-Id: $TRADING_KEY_ID" \
  -H "Novig-Timestamp: $TS" \
  -H "Novig-Signature: $SIG"
```

`interval` is required: `1m`, `5m`, `1h`, or `1d`. `startsAfter` and `startsBefore` pick candles by their start time, and both exclude the time you pass. Without `startsBefore`, the current candle comes last.

One read covers at most 4,096 intervals, so each series holds at most 4,096 candles. A wider window keeps the newest intervals.

You get one series per outcome, oldest candle first. Each trade prices both outcomes, so one series mirrors the other: every price is `1 − price`, and the high and low trade places. A candle's `volume` is the contracts traded in it, the same on both series.

An interval with no trade has no candle. Carry the last close forward if your chart needs one point per interval.

A market that doesn't have exactly two outcomes returns an empty `series`.

## Finding one game

**1. Narrow by league and start window**

`?league=NFL&startsAfter=1758326400000&startsBefore=1758412800000` — a day of one league.

**2. Match description client-side, once, at start-up**

**3. Cache the outcome IDs and trade on them**

> [!NOTE]
>
> Build on IDs, not on `description`. `description` is unversioned prose, and it changes. An outcome ID stays stable for the life of its market.

## Depth and tape

| Route | Returns | Live alternative |
| - | - | - |
| `GET /v3/catalog/markets/{id}/book` | Resting orders by outcome, priority order | [Book channel](https://docs.novig.com/api/streaming/book.md) |
| `GET /v3/catalog/markets/{id}/trades` | Executions, newest first | [Tape channel](https://docs.novig.com/api/streaming/tape.md) |


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.