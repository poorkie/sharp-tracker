# Sharp Tracker

Follows *proven* smart money on Polymarket sports. It alerts your phone when sharp wallets pile onto one side of a game, checks that side against sportsbook lines, and keeps a public, auto-graded record of every alert.

## What makes it different from whale trackers

| Typical tracker | Sharp Tracker |
|---|---|
| "Whale" = big wallet | "Sharp" = wallet with a **proven, sport-specific** record: shrunk ROI plus a t-stat across 25+ graded game bets |
| Reads only closed positions | Also counts **losing tickets nobody redeemed**, which makes most wallets look worse than naive tools show |
| Alerts on one big trade | Alerts on **consensus**: 3+ sharps net-long the same side, little sharp money on the other side, and sellers netted out |
| Polymarket price only | Shows the **no-vig fair price from US sportsbooks**, so you see whether Poly is cheap or expensive |
| Track record is marketing | **RECORD.md** grades every alert automatically: W-L, ROI, and closing line value (CLV), losses included |

## How it runs

GitHub Actions runs `run.py` about every 10 minutes for free, as long as the repo is **public**. Private repos only get 2,000 free minutes a month, which isn't enough for this schedule. Each run:

1. Seeds candidate wallets from Polymarket's sports leaderboard, and queues any unknown wallet that makes a $1,000+ bet.
2. Grades up to 25 wallets per run from their full resolved history (re-grading each one weekly).
3. Pulls every $200+ trade from the last 24h on games starting in the next 36h.
4. Nets each sharp wallet's buys and sells per side, then applies the alert rules in `config.yaml`.
5. Sends alerts, logs them, updates closing prices, grades finished games, and rewrites `RECORD.md`.
6. Commits `state/` and `RECORD.md` back to the repo, which is how it remembers things between runs.

**Expect a quiet first day or two.** Wallet grading builds up about 25 wallets per run, so it takes a few hours to grade the leaderboard and longer to discover sharps outside it.

## Setup (about 20 minutes)

### 1. Put the code on GitHub
1. Create a free account at github.com if you don't have one.
2. Click **New repository** and name it something like `sharp-tracker`. Choose **Public**.
3. Click **uploading an existing file**, then drag in everything from this folder, including the hidden `.github` folder. On a Mac, press Cmd+Shift+. in Finder to show hidden folders. Commit.

### 2. Get alerts on your phone (ntfy, free)
1. Install the **ntfy** app (iOS or Android).
2. Tap **+** and subscribe to a topic with a long, random name, e.g. `sharp-mj-7f3k9q2x`. Anyone who knows the name can read it, so don't use something guessable.
3. In GitHub, go to your repo → **Settings → Secrets and variables → Actions → New repository secret**. Set Name to `NTFY_TOPIC` and Value to your topic name.

### 3. Sportsbook odds (optional, free)
1. Sign up at the-odds-api.com. The free tier gives 500 credits a month. The tracker only pulls odds when an alert fires and caches them for 2 hours, so this usually lasts.
2. Add the secret `ODDS_API_KEY`.

### 4. Turn it on
1. Go to the **Actions** tab and enable workflows if GitHub asks.
2. Click **scan → Run workflow** to run it once now. Open the run and read the log. You should see lines like `nhl: 14 events, 9 upcoming game markets`.
3. After that, it runs on its own.

### Optional: real SMS (Twilio)
Twilio texts to US numbers need a paid account and a verified toll-free number. Verification takes days, and Twilio now asks for privacy-policy and terms URLs. If you go that route, add the secrets `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM`, and `ALERT_TO_PHONE` (format: +15551234567), then set `channels: [ntfy, twilio]` in `config.yaml`.

### Optional: email
Add the secrets `SMTP_USER` (your Gmail), `SMTP_PASS` (a Gmail **app password**, not your normal password), and `ALERT_EMAIL_TO`. Then add `email` to `channels`.

## Reading an alert

```
SHARP CONSENSUS · NHL
Rangers @ 46¢ (+117)
Rangers vs. Bruins · Tue 7:00 PM CST
4 sharps · $8,450 in · $600 other side
Books fair 49% (+104) · Poly cheaper by 3.0¢
0x3fa1… ELITE ROI +9% n=212 · $4,000 @ 44¢
```

- **46¢ (+117):** the current Polymarket price and its American-odds equivalent.
- **Books fair:** the median no-vig probability across US sportsbooks. "Poly cheaper" means the books agree with the sharps and Poly hasn't caught up yet, which is the best case. "Poly pricier" means the sharps are paying above the books' number.
- **ROI / n:** that wallet's shrunk ROI and its number of graded bets **in this sport**.
- **@ 44¢:** the sharp's average entry. If the price has already moved well past it, you're late.

## Tuning

Everything is in `config.yaml` and commented. These settings matter most:
- `consensus.min_wallets` / `min_total_usd`: raise them for fewer, stronger alerts.
- `grading.sharp.min_roi` / `min_t`: control how strict "sharp" is.
- `market_types`: add `spreads` and `totals` to cover more markets.
- `sports`: add or remove leagues. If a league logs `0 events`, its Polymarket tag differs from the key. Add `tag: <slug>` to that league's entry, using the tag slug from the league's URL on polymarket.com.

## Testing locally

```
pip install -r requirements.txt pytest
python -m pytest -q                 # offline tests of the logic
python run.py --console --no-save   # live scan, prints alerts, writes nothing
```

## Honest limitations

- **It isn't instant.** GitHub's scheduler often runs late, so treat alerts as 5–20 minutes behind. That's fine for pre-game positioning, but you won't beat bots to a line move.
- **Not every big Polymarket winner is a better.** Some top wallets are market makers or arbitrage bots. Netting buys against sells filters out most of them, but not all.
- **The edge can disappear.** Once lots of people follow the same wallets, the price moves before you get in. Watch the **Avg CLV** column in RECORD.md. If it stays near zero or negative after 100+ alerts, the signals aren't beating the market.
- **Built against Polymarket's Data API v2** (v1 was retired on November 2, 2026). Field names come from the published docs. If Polymarket changes them, the run log will show errors or zero counts.
- Prediction markets and sports betting may be restricted where you live. Check before you trade, and only bet money you can afford to lose.
