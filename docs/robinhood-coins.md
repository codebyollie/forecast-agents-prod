# Robinhood Coins Intelligence

Coins Intelligence is a read-only discovery and forecasting layer for native Robinhood Chain ecosystem tokens. It is separate from both Robinhood Stock Tokens and the list of cryptocurrencies available through the Robinhood brokerage application.

## Top 10 selection

The service reads live Robinhood Chain pools from GeckoTerminal, attaches the token contract to every quote, and excludes:

- USDG, WETH and ETH quote assets;
- Robinhood Stock Tokens;
- leveraged products;
- assets without a valid contract or positive USD price.

Duplicate contracts are collapsed to their strongest pool. The remaining assets are ranked by market cap or FDV, then liquidity and 24-hour volume. The list is intentionally dynamic: PONS and other ecosystem assets remain visible only while they qualify against current market data.

## Forecast lifecycle

`Coin Asset Outlooks` support 24H, 7D, 30D, 90D and 180D horizons. A server-captured pool price becomes the immutable reference. At resolution, a fresh post-horizon quote determines `Higher` or `Not Higher`, produces consensus and agent-level Brier Scores, and can be attached to the Forecast AI Proof Network.

Polymarket and Kalshi are optional event context, never the coin forecast target.

## Execution boundary

Coin analysis, proofs and scoring are live independently of autonomous execution. The Stock Token Execution Agent does not accept ecosystem coins. Any future coin execution requires a contract allowlist, route simulation, liquidity minimums, slippage limits and operator or governance approval.
