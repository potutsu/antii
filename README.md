# antii — Overreaction Fading on Polymarket

Standalone strategy. No proba dependency.

## Strategy

Fade upward YES price spikes on Polymarket non-sports markets.

**Signal:** YES price pumped ≥ 7% in the last 60 minutes  
**Entry:** Buy NO when YES reverts ≥ 3% from signal price (or timeout at 120 min)  
**Exit:** Take profit at 10% revert | Stop loss at 15% adverse | Force close at 48h  
**Sizing:** $40 notional per trade | max 10 concurrent positions

## Structure

```
antii/
├── manager.py           TUI process manager (entry point)
├── antii_config.py      All tunable config + scripts registry
├── paths.py             Canonical file paths
├── polymarket.py        CLOB + Gamma API client
├── base_rate.py         Keyword → base rate lookup table
├── finnhub_client.py    News fetch at signal time
├── discovery.py         Scans markets → signal.jsonl
├── monitor_entry.py     Watches signals → verdicts.jsonl (ENTER/DISCARD)
├── monitor_position.py  Watches positions → verdicts.jsonl (EXIT)
├── trader.py            Consumes verdicts → paper_positions.jsonl
├── shadow.py            Price logger (always-on background)
├── postmortem.py        MFE/MAE/ROI analytics on closed positions
├── data/                Runtime data files (gitignore)
└── logs/                Worker log files (gitignore)
```

## Quickstart

```bash
cd ~/antii
pip install requests

# Optional: set env vars
export FINNHUB_API_KEY=your_key
export ANTII_BOT_TOKEN=your_telegram_bot_token
export ANTII_CHAT_ID=your_chat_id

python manager.py
```

In the TUI:
- `Ctrl+A` — start all pipeline workers
- `[1-6]` — switch log view
- `[r]` — restart viewed worker
- `[h]` — halt/resume viewed worker
- `[q]` — quit

Shadow starts automatically. Pipeline starts manually.

## Pipeline Flow

```
discovery → signal.jsonl
signal → shadow (immediate, always-on)
signal → monitor_entry (every 2 min) → verdicts.jsonl (ENTER | DISCARD)
ENTER verdict → trader → paper_positions.jsonl (open)
open position → monitor_position (every 5 min) → verdicts.jsonl (EXIT)
EXIT verdict → trader → paper_positions.jsonl (close) + trade.jsonl
close → postmortem → postmortem.jsonl
```

## Data Files

| File | Contents |
|---|---|
| `data/signal.jsonl` | One row per detected overreaction signal |
| `data/verdicts.jsonl` | ENTER / DISCARD / EXIT decisions |
| `data/paper_positions.jsonl` | Open and closed paper trades |
| `data/trade.jsonl` | Open/close event log |
| `data/shadow.jsonl` | Price series for every signal (15-min cadence) |
| `data/postmortem.jsonl` | MFE/MAE/ROI analytics per closed position |

## Key Config (antii_config.py)

| Param | Default | Meaning |
|---|---|---|
| `ENTRY_MIN_MOVE_60MIN` | 7% | YES pump threshold for signal |
| `ENTRY_MIN_REVERT_PCT` | 3% | Reversion needed to confirm entry |
| `ENTRY_MAX_WAIT_MIN` | 120 | Timeout entry if no reversion |
| `EXIT_REVERT_PCT` | 10% | Take profit threshold |
| `EXIT_STOP_LOSS_PCT` | 15% | Stop loss threshold |
| `EXIT_MAX_HOLD_HOURS` | 48 | Force close |
| `NOTIONAL_PER_TRADE` | $40 | Paper trade size |
| `MAX_OPEN_POSITIONS` | 10 | Concurrent position cap |

All params overridable via environment variables.

## Postmortem Batch

```bash
python postmortem.py --batch          # compute missing postmortems
python postmortem.py --batch --force  # recompute all
```

## Market Filters

- Binary markets only (YES/NO)
- Categories: politics, geopolitics, economics, crypto, tech
- Excludes all sports tags
- `volume_24h >= $5,000`, `liquidity >= $2,000`
- YES price: `0.05 – 0.45` (not near certainty)
