# ForecastRegistry

`ForecastRegistry.sol` is the private production contract for the Forecast AI
Proof Network. It is intended to be deployed to Robinhood Chain testnet first
(chain ID `46630`) and mainnet only after testnet verification.

The contract:

- commits an immutable payload hash before market close;
- supports batched consensus/agent commitments;
- resolves binary outcomes through approved resolver addresses;
- calculates Brier scores onchain;
- maintains global and category-specific agent score totals;
- never stores API keys, user credentials, research text, or private analysis.

Production deployment requires a dedicated deployer/resolver wallet with testnet
ETH. Never commit or paste its private key into source control or chat.
