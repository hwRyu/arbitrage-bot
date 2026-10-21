# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

KIMP (Kimchi Premium) Arbitrage Bot - An automated cryptocurrency arbitrage trading system that exploits price differences (kimchi premium) between Korean exchanges (Upbit) and international exchanges (Bybit).

**Strategy (역전 전략 - Reversal Strategy)**:
- **Entry**: When kimchi premium is LOW or NEGATIVE (international price higher than domestic)
- **Exit**: When kimchi premium RISES and reaches target profit
- **Hedge**: Spot buy on Upbit + Short position on Bybit (complete hedge, no directional risk)
- **Profit**: Premium difference (exit premium - entry premium) + funding fees - fees

**IMPORTANT**: Do NOT update PROJECT_SPEC.md when making code changes. The user will manage PROJECT_SPEC.md updates directly. Focus only on implementing code changes as requested.

## Development Commands

**Requirements**: Python 3.10 or higher

### Environment Setup
```bash
# 1. Copy environment template
cp .env.example .env

# 2. Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Edit .env file with your API keys (for live trading)
# Exchange rate is fetched automatically via yfinance (no API key needed)
# For paper trading, no exchange API keys needed - just set PAPER_TRADING=true
```

### Running the Bot
```bash
# Paper trading (simulation mode - safe for testing)
python main.py

# Before live trading, verify .env settings:
# - PAPER_TRADING=false
# - All API keys configured
# - Position sizes appropriate
```

### Database Operations
```bash
# Access SQLite database
sqlite3 kimp_bot.db

# View active strategies
sqlite3 kimp_bot.db "SELECT id, coin, state, entry_premium, current_premium FROM strategies WHERE state != 'EXITED' ORDER BY created_at DESC;"

# View profit statistics
sqlite3 kimp_bot.db "SELECT coin, COUNT(*) as count, AVG(realized_pnl_percent) as avg_profit, SUM(realized_pnl_krw) as total_pnl FROM strategies WHERE state = 'EXITED' GROUP BY coin;"

# Recent market scans
sqlite3 kimp_bot.db "SELECT coin, premium, is_opportunity, scanned_at FROM market_scans WHERE scanned_at > datetime('now', '-1 hour') ORDER BY premium DESC LIMIT 20;"

# View recent errors
sqlite3 kimp_bot.db "SELECT created_at, level, message FROM bot_logs WHERE level = 'ERROR' ORDER BY created_at DESC LIMIT 10;"

# Check funding fees collected
sqlite3 kimp_bot.db "SELECT coin, SUM(fee_amount) as total_funding, COUNT(*) as payments FROM funding_fees GROUP BY coin;"

# Analyze slippage per exchange
sqlite3 kimp_bot.db "SELECT exchange, AVG(actual_price - expected_price) as avg_slippage FROM trades WHERE side = 'buy' GROUP BY exchange;"

# Find strategies stuck in non-terminal states
sqlite3 kimp_bot.db "SELECT id, coin, state, updated_at FROM strategies WHERE state NOT IN ('EXITED', 'ERROR') AND updated_at < datetime('now', '-1 hour');"
```

### Utility Scripts

```bash
# Check OHLC data completeness (if OHLC collection enabled)
python scripts/check_ohlc_gaps.py
```

### Log Monitoring

**Log Configuration:**
- Location: `logs/bot.log`
- Rotation: 10MB per file, keeps 5 backup files
- Format: `[timestamp] [level] [module] - message`

```bash
# Real-time log viewing
tail -f logs/bot.log

# Search for specific events
grep "PROFITABLE" logs/bot.log
grep "ERROR" logs/bot.log
grep "liquidation" logs/bot.log

# View last 100 lines
tail -n 100 logs/bot.log

# Monitor specific coin
tail -f logs/bot.log | grep "SOL"
```

## Architecture

### 2-Layer Design

**Layer 1: Market Scanner** ([core/scanner.py](core/scanner.py))
- Stateless, lightweight scanner running continuously
- Scans all target coins every 10 seconds
- Calculates kimchi premium (excluding USDT premium)
- Identifies entry opportunities
- Creates Layer 2 strategy instances when opportunities found
- Saves scan results to `market_scans` table

**Layer 2: Strategy Instance** ([core/strategy.py](core/strategy.py))
- One instance per coin, runs in separate thread
- Manages complete lifecycle: entry → monitoring → exit
- State-driven execution via state machine
- Persists state to database for crash recovery
- Independent from other strategies (isolated risk)

### State Machine Flow

The bot uses a strict state machine ([core/state_machine.py](core/state_machine.py)) with defined transitions:

```
INIT → CHECK_CAPITAL → VALIDATE_ENTRY → ENTERING → HEDGED → MONITORING
  → PROFITABLE → EXITING → SETTLEMENT → EXITED
```

Emergency paths:
- MONITORING → LIQUIDATION_RISK → REDUCE_POSITION (50% position reduction)
- MONITORING → STOP_LOSS → EXITING (force exit on -5% loss)

All state transitions are validated against `StateMachine.ALLOWED_TRANSITIONS`. Invalid transitions raise `StateTransitionError`.

### Key Architectural Concepts

**Complete Hedge Strategy**: Every position maintains two sides:
- Upbit: Spot buy (KRW)
- Bybit: Short position (USDT)
- Position sizes matched to eliminate directional price risk

**USDT Premium Separation**: Premium calculation uses real-time USD/KRW exchange rate from Yahoo Finance (yfinance). The bot does NOT use USDT/KRW rates because users manually manage USDT transfers:

- User buys USDT when USDT premium < 0.5%, transfers to Bybit
- User sells USDT when USDT premium > 0.5%, transfers from Bybit to Upbit
- Bot only tracks coin premium, not USDT premium

Formula:
```python
premium = (upbit_krw_price / (bybit_usd_price × official_usd_krw_rate)) - 1
```

**Crash Recovery**: On startup, `BotManager.recover_active_strategies()` loads all non-terminal strategies from database and resumes monitoring from their last state.

## OHLC Data Collection (Optional)

The bot includes an optional premium data collection system for analysis and backtesting.

**Purpose**: Aggregate premium snapshots from market scans into OHLC (Open-High-Low-Close) candles for historical analysis.

**Features:**
- Runs independently in a separate thread (no impact on trading performance)
- Collects daily premium OHLC data (1d timeframe)
- Stores both premium and price candles
- Automatic data retention management
- Can be disabled without affecting trading

**Configuration (.env):**
```bash
OHLC_ENABLED=true              # Enable/disable data collection
OHLC_TIMEFRAMES=1d             # Currently supports daily candles
OHLC_UPDATE_INTERVAL=60        # Update every 60 seconds
OHLC_RETENTION_DAYS=365        # Keep 1 year of data
```

**Database Table:** `premium_ohlc` - Stores OHLC data per coin/date
- Premium OHLC (open, high, low, close %)
- KRW price OHLC (Upbit)
- USD price OHLC (Bybit)
- Scan count, finalized flag

**Utility Script:**
```bash
# Check for missing dates in OHLC data
python scripts/check_ohlc_gaps.py
```

**Use Cases:**
- Historical premium analysis
- Pattern recognition
- Backtesting strategy modifications
- Performance analytics

## Configuration

### Environment Variables (.env)

Key settings from [config/settings.py](config/settings.py):

```bash
# Trading mode
PAPER_TRADING=true  # Set to false for live trading

# Target coins (comma-separated)
TARGET_COINS=SOL,XRP,DOGE,ADA

# Profit targets
TARGET_PROFIT_PERCENT=1.0  # Target profit percentage (after fees)
MIN_ENTRY_PREMIUM=0.0      # Max premium for entry (default 0%, enter when premium ≤ 0%)

# Position sizing
MAX_POSITION_KRW=1000000
MAX_POSITION_USDT=1000

# Risk management
LIQUIDATION_BUFFER=1.7  # Liquidation price must be 1.7x entry price
MIN_ORDERBOOK_DEPTH_MULTIPLIER=10
BYBIT_LEVERAGE=1  # FIXED at 1x, do NOT change!

# API Keys (required for live trading)
UPBIT_ACCESS_KEY=your_key
UPBIT_SECRET_KEY=your_secret
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
```

### Configuration Classes

All settings are centralized in [config/settings.py](config/settings.py):
- `APIKeys`: Exchange and external API credentials
- `BotConfig`: Trading targets, position sizes, profit goals
- `Fees`: Exchange fee structures (Upbit: 0.05%, Bybit Taker: 0.075%)
- `RiskParams`: Liquidation buffers, slippage limits, stop-loss thresholds
- `BybitSettings`: Leverage (default 1x), margin mode (ISOLATED)
- `ScannerConfig`: Scan interval (10s), max concurrent strategies (3)
- `States`: All state machine states

### Database-Based Configuration Management

The bot supports two configuration modes controlled by the `CONFIG_SOURCE` environment variable:

**Mode 1: ENV (default)** - Traditional .env file configuration
```bash
CONFIG_SOURCE=env  # Load from .env file (default behavior)
```

**Mode 2: DB** - Database-based configuration with real-time updates
```bash
CONFIG_SOURCE=db  # Load from database config_settings table
```

**Key Features:**
- Real-time configuration updates (5-second auto-reload)
- No bot restart required after changing DB settings
- Automatic migration from .env to DB on first run
- API keys remain in .env for security (never stored in DB)

**Supported Settings (11 total):**
- `BotConfig`: TARGET_COINS, TARGET_PROFIT_PERCENT, MIN_ENTRY_PREMIUM, MAX_POSITION_KRW, MAX_POSITION_USDT
- `RiskParams`: LIQUIDATION_BUFFER, MIN_ORDERBOOK_DEPTH_MULTIPLIER, STOP_LOSS_PERCENT
- `ScannerConfig`: SCAN_INTERVAL, MAX_CONCURRENT_STRATEGIES, MONITORING_INTERVAL

**Migration to DB Mode:**
```bash
# 1. Edit .env
CONFIG_SOURCE=db

# 2. Restart bot (first time only)
python main.py
# Output: "11개 설정을 DB로 마이그레이션 완료"

# 3. Verify migration
sqlite3 kimp_bot.db "SELECT config_class, key, value FROM config_settings;"
```

**Changing Settings via SQL:**
```bash
# Update target profit
sqlite3 kimp_bot.db "UPDATE config_settings SET value='1.5', version=version+1 WHERE key='TARGET_PROFIT_PERCENT';"

# Update target coins (JSON format for lists)
sqlite3 kimp_bot.db "UPDATE config_settings SET value='[\"BTC\",\"ETH\",\"SOL\"]' WHERE key='TARGET_COINS';"

# Update position size
sqlite3 kimp_bot.db "UPDATE config_settings SET value='2000000' WHERE key='MAX_POSITION_KRW';"

# Changes are auto-detected within 5 seconds (no restart needed)
```

**View Configuration:**
```bash
# All settings
sqlite3 kimp_bot.db "SELECT config_class, key, value, description, updated_at FROM config_settings ORDER BY config_class, key;"

# BotConfig only
sqlite3 kimp_bot.db "SELECT key, value, description FROM config_settings WHERE config_class='BotConfig';"

# Recently modified settings
sqlite3 kimp_bot.db "SELECT config_class, key, value, version, updated_at FROM config_settings WHERE version > 1 ORDER BY updated_at DESC;"
```

**Implementation Details:**

The DB config system uses a transparent proxy pattern:
- Original config classes in [config/settings.py](config/settings.py) load from .env
- When `CONFIG_SOURCE=db`, proxy classes from [config/config_manager.py](config/config_manager.py) intercept attribute access
- Proxies query the `config_settings` table, falling back to .env if key not found
- Existing code like `BotConfig.TARGET_COINS` works unchanged
- ConfigManager singleton handles caching and auto-reload

**Architecture:**
```
config/settings.py        → Original classes (load from .env)
       ↓ (if CONFIG_SOURCE=db)
config/config_manager.py  → ConfigManager singleton + Proxy classes
       ↓
models/models.py          → ConfigSetting table
       ↓
Transparent to existing code (BotConfig.TARGET_COINS works the same)
```

**Reverting to ENV Mode:**
```bash
# Edit .env
CONFIG_SOURCE=env

# Restart bot
python main.py
# DB settings are preserved but not used
```

## Database Schema

SQLite database with 7 main tables (defined in [models/models.py](models/models.py)):

**strategies**: Complete strategy lifecycle (one row per coin strategy)
- Entry data: premium, prices, amounts
- Position info: order IDs, liquidation price
- Exit data: realized P&L, exit reason
- State tracking for recovery

**trades**: Individual trade executions (entry/exit on each exchange)

**funding_fees**: Bybit funding fee collections (every 8 hours)

**market_scans**: Historical scan data for analysis

**bot_logs**: Important events and errors

**premium_ohlc**: Premium OHLC candles for analysis (optional feature)
- Premium OHLC data (open, high, low, close %)
- Price OHLC data (KRW and USD)
- Aggregated from market_scans table
- Used for historical analysis and backtesting

**config_settings**: DB-based configuration (optional, when CONFIG_SOURCE=db)
- config_class: Configuration class name (BotConfig, RiskParams, ScannerConfig)
- key: Setting key (TARGET_COINS, MAX_POSITION_KRW, etc.)
- value: Setting value (JSON-encoded for lists)
- value_type: Data type (str, int, float, bool, list)
- version: Change tracking (incremented on updates)
- Unique constraint on (config_class, key)

Query patterns in PROJECT_SPEC.md section 11.5.

## Service Layer

Exchange integrations in [services/](services/):

**upbit.py**: Upbit spot trading
- `fetch_ticker(coin)`: Get current price
- `create_market_order(coin, side, amount=None, cost=None)`: Execute trades
- `fetch_balance()`: Get KRW and coin balances
- Note: Upbit buy orders use `cost` parameter (KRW amount), sell orders use `amount` (coin quantity)

**bybit.py**: Bybit futures trading
- `open_short_position(coin, amount)`: Enter short with positionIdx=2 (Hedge Mode)
- `close_short_position(coin, amount)`: Close with reduceOnly=True
- `fetch_positions(symbols)`: Get position details including liquidation price
- Uses ISOLATED margin mode, default 1x leverage

**exchange_rate.py**: Yahoo Finance (yfinance)
- `get_usd_krw_rate()`: Real-time USD/KRW exchange rate
- No API key required, 24/7 availability with 1-hour caching

All services support paper trading mode via constructor parameter.

## Key Calculations

### Premium Calculation ([utils/calculator.py](utils/calculator.py))

```python
def calculate_premium(krw_price, usd_price, exchange_rate):
    """
    Calculate premium using official USD/KRW exchange rate
    NOT USDT/KRW! (User manages USDT manually)
    """
    return (krw_price / (usd_price * exchange_rate)) - 1
```

**Why USD/KRW instead of USDT/KRW?**
- User manually manages USDT: buys when USDT premium < 0.5%, sells when > 0.5%
- Bot does NOT handle inter-exchange USDT transfers
- Uses real-time exchange rate from Yahoo Finance (yfinance)

### Liquidation Price (Bybit Short, 1x leverage FIXED)

```python
liquidation_price = entry_price × (2 - MMR)
# MMR fetched from Bybit API dynamically
# Fallback values in config:
# - BTC, ETH: 0.5%
# - SOL, XRP, ADA: 1.0%
# - DOGE: 2.0%
```

Example: SOL short at 100 USD with MMR 1.0%
- Liquidation: 100 × (2 - 0.01) = 199 USD
- Price must nearly double to trigger liquidation (very safe with 1x leverage)

### Entry Requirements (Reversal Strategy)

Must satisfy ALL conditions:
1. **Premium ≤ MIN_ENTRY_PREMIUM** (default 0%, enter when premium is low/negative)
2. Liquidation price ≥ entry price × 1.7
3. Orderbook depth ≥ position size × 10 (both exchanges)
4. Expected slippage < 0.5%
5. Sufficient KRW and USDT balances

### Exit Condition

```python
# Exit when: current_premium >= target_exit_premium
target_exit_premium = entry_premium + target_profit + fees + slippage

Example:
  Entry premium: -0.5%
  Target profit: 1.0%
  Fees: 0.25%
  Slippage: 0.1%
  → Target exit premium: -0.5% + 1.0% + 0.25% + 0.1% = 0.85%
```

## Working with This Codebase

### Adding New Coins

1. Add to `.env`: `TARGET_COINS=SOL,XRP,DOGE,ADA,NEWCOIN`
2. Verify MMR in `RiskParams.MMR_BY_COIN` (add if not present)
3. Test in paper trading first
4. Check orderbook depth is sufficient

### Modifying Strategy Logic

**REQUIRED FIRST STEP**: Update [PROJECT_SPEC.md](PROJECT_SPEC.md) with the planned changes (see line 7 of PROJECT_SPEC.md).

1. Identify which state(s) are affected
2. Modify handler methods in `StrategyInstance` (e.g., `_handle_monitoring()`)
3. If adding new states, update:
   - `States` class in [config/settings.py](config/settings.py)
   - `StateMachine.ALLOWED_TRANSITIONS` in [core/state_machine.py](core/state_machine.py)
   - Add handler method in [core/strategy.py](core/strategy.py)
4. Test thoroughly in paper trading mode

### Paper Trading vs Live Trading

Paper trading mode simulates all exchange operations:
- Uses real market data (prices, orderbooks)
- Simulates order execution at current market prices
- Maintains virtual balances in memory
- No actual API calls to place orders
- Fully safe for testing logic changes

Enable via `.env`: `PAPER_TRADING=true`

Initial virtual capital:
- `PAPER_INITIAL_KRW=10000000` (10M KRW)
- `PAPER_INITIAL_USDT=10000` (10K USDT)

### Signal Handling and Shutdown

The bot implements graceful shutdown:
- Catches SIGINT (Ctrl+C) and SIGTERM
- Sets `shutdown_event` to signal all threads
- Waits for strategy threads to complete (10s timeout)
- Strategies remain in database with current state
- Resume automatically on restart

### Common Gotchas

**Reversal Strategy**: This bot enters when premium is LOW/NEGATIVE and exits when premium RISES. This is opposite to traditional kimchi premium arbitrage.

**USD/KRW vs USDT/KRW**: Always use official USD/KRW exchange rate for premium calculation, NOT USDT/KRW. User handles USDT transfers manually.

**Leverage is FIXED at 1x**: Do NOT change `BYBIT_LEVERAGE` from 1. The entire strategy is designed around 1x leverage for maximum safety.

**MMR is Dynamic**: MMR (Maintenance Margin Rate) is fetched from Bybit API in real-time, not hardcoded. Fallback values exist in config for API failures.

**State Persistence**: Strategy state is saved to database after each transition. If bot crashes, strategies resume from last saved state.

**Position Sizing**: Actual position size is `min(MAX_POSITION_KRW / price, MAX_POSITION_USDT / price)` to ensure complete hedge with available capital on both sides.

**Bybit Position Index**: Always use `positionIdx=2` for short positions (Hedge Mode). Using wrong index will fail or create long positions.

**Rate Limits**: Both exchanges enforce rate limits. The bot uses `enableRateLimit=True` in ccxt, but adding aggressive polling may trigger limits.

## File Organization

```
kimp/
├── config/settings.py      # All configuration, constants, and States
├── core/
│   ├── scanner.py          # Layer 1: Market scanning
│   ├── strategy.py         # Layer 2: Strategy execution
│   ├── state_machine.py    # State transition validation
│   └── ohlc_collector.py   # OHLC data collection (optional)
├── models/
│   ├── database.py         # SQLAlchemy setup
│   ├── models.py           # DB table definitions
│   └── position.py         # Data classes (MarketPrice, etc.)
├── services/               # Exchange and external API wrappers
├── scripts/                # Utility scripts
│   └── check_ohlc_gaps.py  # Check OHLC data completeness
├── utils/
│   ├── calculator.py       # Premium, P&L calculations
│   ├── validator.py        # Entry validation logic
│   ├── logger.py           # Logging configuration
│   └── ohlc_utils.py       # OHLC data utilities
├── main.py                 # Entry point, BotManager
├── PROJECT_SPEC.md         # Complete specification (MUST read before changes)
└── README.md               # User guide
```

## Testing

**Current Status**: No automated testing infrastructure exists yet.

**To add tests** (future development):
1. Install pytest: `pip install pytest pytest-asyncio`
2. Create `tests/` directory structure:
   ```
   tests/
   ├── test_calculator.py     # Unit tests for premium, liquidation calculations
   ├── test_validator.py      # Unit tests for entry validation
   ├── test_state_machine.py  # State transition tests
   └── conftest.py            # Pytest fixtures
   ```
3. Run tests: `pytest tests/`
4. Run with coverage: `pytest --cov=. tests/`

**Recommended test coverage:**
- `utils/calculator.py`: All calculation functions
- `utils/validator.py`: Entry validation logic
- `core/state_machine.py`: State transitions
- `services/`: Mock API responses for Upbit, Bybit

## Code Style and Guidelines

### Comment Language
**CRITICAL**: All code comments MUST be written in Korean.

- Docstrings: Korean
- Inline comments: Korean
- Function/class descriptions: Korean
- TODO comments: Korean

This applies to ALL new code and modifications. The codebase uses Korean comments consistently, and all future code must follow this convention.

Example:
```python
def calculate_premium(krw_price, usd_price, exchange_rate):
    """
    김치 프리미엄 계산

    Args:
        krw_price: 업비트 가격 (KRW)
        usd_price: 바이비트 가격 (USD)
        exchange_rate: USD/KRW 환율

    Returns:
        프리미엄 (%)
    """
    # 이론적 원화 가격 계산
    theoretical_krw = usd_price * exchange_rate

    # 프리미엄 계산 및 반환
    return ((krw_price / theoretical_krw) - 1) * 100
```

## Development Workflow

1. **Code changes**: Implement requested changes directly without updating PROJECT_SPEC.md (user manages spec updates)
2. **Testing**: Always test in paper trading mode first (`PAPER_TRADING=true`)
3. **Monitoring**: Use `tail -f logs/bot.log` to watch bot behavior in real-time
4. **Database inspection**: Query `strategies` table to verify state transitions
5. **Live trading**: Start with small position sizes (e.g., 100K KRW, 100 USDT)

**Note**: PROJECT_SPEC.md is maintained by the user. Do not suggest or attempt to update it during code changes.

## Important References

- [PROJECT_SPEC.md](PROJECT_SPEC.md): Complete technical specification (76 pages, 1650+ lines) - comprehensive system design, formulas, risk parameters, database schema, API integration details
- [README.md](README.md): User-facing documentation and setup guide
- [config/settings.py](config/settings.py): Single source of truth for all configurable values (580 lines, extensively documented)
