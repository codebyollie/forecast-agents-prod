# Architecture Reference

Forecast AI acts as modular multi-agent intelligence infrastructure for Robinhood assets and prediction markets.

## Module Map

*   `forecast_ai/kalshi/`: Primary market-data client for Kalshi & Robinhood Predict event contracts.
*   `forecast_ai/robinhood_agentic/`: Recommendation formatter for hand-off to Robinhood Agentic Trading MCP.
*   `forecast_ai/services/robinhood_stock_tokens.py`: Official Robinhood Stock Token quotes and RWA resolution prices.
*   `forecast_ai/services/robinhood_crypto.py`: Keyless Top 10 Robinhood Chain ecosystem catalog, live DEX quotes and coin resolution prices.
*   `forecast_ai/polymarket/`: Read-only market data client for Polymarket.
*   `forecast_ai/agents/`: Individual, specialized AI agents (News, Social, Reddit, Research, Macro, On-chain, Market).
*   `forecast_ai/consensus/`: Consensus Engine integrating evidence and calibrations.
*   `forecast_ai/intelligence/`: Algorithms for signal scoring, anomaly detection, and probability calibration.
*   `forecast_ai/memory/`: Long-term forecast history, accuracy metrics, and agent reputations.
*   `forecast_ai/sources/`: Data connector adapters.
*   `forecast_ai/providers/`: Unified wrappers for LLM APIs.
*   `forecast_ai/pipelines/`: Running forecasting and watching loops.
*   `forecast_ai/api/`: FastAPI server.
*   `forecast_ai/cli/`: Interactive configuration and click commands.
