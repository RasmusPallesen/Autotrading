# 🤖 Trading Agent

An AI-powered autonomous trading agent for US stocks (Alpaca) and crypto (Coinbase).  
Uses Claude as the decision brain, fed by technical indicators computed on live market data.

---

## Architecture

```
main.py  ←  orchestrates the loop
│
├── data/
│   ├── alpaca_fetcher.py     # OHLCV bars, account info, positions
│   └── coinbase_fetcher.py   # Crypto candles (Coinbase Advanced Trade)
│
├── signals/
│   └── technical.py          # RSI, MACD, EMA, Bollinger Bands, ATR, Volume
│
├── agent/
│   └── decision_engine.py    # Claude-powered BUY/SELL/HOLD decisions
│
├── risk/
│   └── risk_manager.py       # Hard guardrails: drawdown, position limits, kill switch
│
├── execution/
│   └── alpaca_executor.py    # Places bracket orders via Alpaca API
│
├── storage/
│   └── trade_store.py        # SQLite logging of all decisions + executions
│
└── config.py                 # All configuration (loaded from env vars)
```

---

## Quickstart

### 1. Clone & install

```bash
git clone https://github.com/YOUR_USERNAME/trading-agent.git
cd trading-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env and add your API keys
```

Get your keys:
- **Alpaca**: https://app.alpaca.markets → Paper Trading → API Keys
- **Coinbase**: https://www.coinbase.com/settings/api
- **Anthropic**: https://console.anthropic.com

### 3. Run in paper trading mode

```bash
# Load env vars and run
source .env && python main.py
# OR use python-dotenv (auto-loaded if you add load_dotenv() to main.py)
```

The agent will:
1. Connect to your Alpaca paper account
2. Fetch live market data for the watchlist
3. Compute technical indicators (RSI, MACD, EMA, BB, ATR)
4. Ask Claude for a BUY/SELL/HOLD decision per symbol
5. Check risk rules before executing
6. Place bracket orders (with stop-loss + take-profit)
7. Log everything to `logs/trades.db`

---

## Configuration

Edit `config.py` or set environment variables:

| Setting | Default | Description |
|---|---|---|
| `ALPACA_PAPER` | `true` | Use paper trading endpoint |
| `loop_interval_seconds` | 60 | Seconds between agent ticks |
| `min_confidence` | 0.65 | Min AI confidence to act |
| `max_position_pct` | 10% | Max portfolio % per position |
| `stop_loss_pct` | 5% | Max stop-loss per trade |
| `take_profit_pct` | 15% | Max take-profit per trade |
| `max_daily_drawdown_pct` | 3% | Daily loss limit before kill switch |
| `max_open_positions` | 10 | Max simultaneous positions |

Customise the watchlist in `config.py`:
```python
stocks: ["AAPL", "NVDA", "TSLA", ...]
crypto: ["BTC-USD", "ETH-USD", ...]
```

---

## Risk Controls

The risk manager enforces hard limits **before** every trade:

- ✅ Confidence threshold check
- ✅ Max open positions
- ✅ Max position size (% of portfolio)
- ✅ Buying power check
- ✅ Daily drawdown kill switch (auto-halts agent if exceeded)
- ✅ Manual kill switch (`risk.activate_kill_switch()`)

---

## Going Live

1. Ensure you've run the agent in **paper mode** and are happy with performance
2. Set `ALPACA_PAPER=false` in `.env`
3. Set `COINBASE_SANDBOX=false` in `.env`
4. Start small — consider reducing `max_position_pct` and `max_daily_drawdown_pct` initially
5. Monitor `logs/agent.log` and `logs/trades.db`

⚠️ **You can lose real money. Never trade more than you can afford to lose.**

---

## Roadmap

- [ ] Coinbase execution layer
- [ ] News/sentiment signal integration
- [ ] Backtesting harness
- [ ] Web dashboard (Flask or Streamlit)
- [ ] Telegram/email alerts on trade execution
- [ ] Multi-timeframe signal fusion
- [ ] Position sizing based on Kelly criterion

---

## License

MIT
