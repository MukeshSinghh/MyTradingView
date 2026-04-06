# MyTradingView

Personal portfolio tracker — hosted on GitHub Pages. No backend, no login. All data lives in two JSON files you maintain.

## Live site
**https://MukeshSinghh.github.io/MyTradingView**

---

## File structure

```
MyTradingView/
├── index.html      ← app (do not edit)
├── trades.json     ← your trade history
├── prices.json     ← current market prices
└── README.md
```

---

## trades.json format

Each trade is an object in a JSON array:

```json
[
  { "id": 1, "ticker": "RELIANCE", "type": "buy",  "qty": 10, "price": 2450.00, "date": "2025-01-15" },
  { "id": 2, "ticker": "TCS",      "type": "buy",  "qty": 5,  "price": 3800.00, "date": "2025-01-22" },
  { "id": 3, "ticker": "TCS",      "type": "sell", "qty": 2,  "price": 3950.00, "date": "2025-03-10" }
]
```

| Field    | Type   | Description                      |
|----------|--------|----------------------------------|
| `id`     | number | Unique integer per trade         |
| `ticker` | string | Stock symbol (e.g. `RELIANCE`)   |
| `type`   | string | `"buy"` or `"sell"`              |
| `qty`    | number | Number of shares / units         |
| `price`  | number | Trade price in ₹                 |
| `date`   | string | ISO date `YYYY-MM-DD`            |

---

## prices.json format

Update this file whenever you want to refresh current market prices:

```json
{
  "updated": "2026-04-06",
  "prices": {
    "RELIANCE":   2680.00,
    "TCS":        3720.00,
    "INFY":       1610.00,
    "HDFC":       1755.00,
    "WIPRO":      445.00,
    "BAJFINANCE": 7250.00
  }
}
```

| Field     | Description                                  |
|-----------|----------------------------------------------|
| `updated` | Date string shown in the top bar             |
| `prices`  | Map of ticker → current price in ₹          |

Every ticker that appears in `trades.json` should have a matching entry here for P&L to show correctly.

---

## Views

| View        | What it shows                                              |
|-------------|------------------------------------------------------------|
| Dashboard   | Holdings snapshot table + performance chart                |
| P & L       | Cumulative P&L line, per-trade bars, P&L by ticker         |
| Portfolio   | Running portfolio value over time + holding cards          |
| Weekly      | Weekly gain/loss bars + cumulative line + weekly table     |
| Trades      | Full trade log with CMP and per-trade P&L                  |

---

## Deploying to GitHub Pages

```bash
git clone https://github.com/MukeshSinghh/MyTradingView.git
cd MyTradingView

# Add or update your files
cp /path/to/index.html .
cp /path/to/trades.json .
cp /path/to/prices.json .

git add .
git commit -m "Update trades and prices"
git push origin main
```

Then go to **Settings → Pages → Source: main / (root)** and save.

---

## Updating prices

Edit `prices.json` locally and push:

```bash
# Edit prices.json with your editor, then:
git add prices.json
git commit -m "Update prices $(date +%Y-%m-%d)"
git push
```

The site updates within ~30 seconds.
