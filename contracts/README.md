# ForecastRegistry deployment

`ForecastRegistry.sol` commits immutable SHA-256 forecast payloads, resolves
official binary outcomes, calculates Brier Scores, and maintains per-agent and
per-category score totals.

The first deployment must use Robinhood Chain testnet. Mainnet should only be
selected after a successful commit/resolve test and contract review.

## GitHub deployment

1. Create a dedicated low-balance EVM service wallet. Do not use a personal or
   treasury wallet.
2. Add its private key to the private backend repository as the GitHub Actions
   secret `FORECAST_DEPLOYER_PRIVATE_KEY`.
3. Fund that wallet with testnet gas.
4. Open **Actions → ForecastRegistry → Run workflow** and select
   `robinhood-testnet`.
5. Copy the `contractAddress` from the deploy job output.

The deployment registers these submitters to the same service wallet:
`consensus`, `market`, `news`, `macro`, `social`, `reddit`, `research`, and
`onchain`. Consensus is an aggregate record, not an eighth forecasting agent.

## Railway backend variables for testnet

```env
ROBINHOOD_CHAIN_RPC_URL=https://rpc.testnet.chain.robinhood.com
ROBINHOOD_CHAIN_ID=46630
FORECAST_REGISTRY_ADDRESS=0x_DEPLOYED_CONTRACT
FORECAST_PROOF_ENABLED=true
FORECAST_PUBLISHER_PRIVATE_KEY=0x_DEDICATED_SERVICE_WALLET_PRIVATE_KEY
FORECAST_PROOF_PUBLISH_INTERVAL_SECONDS=15
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVER_ONLY_SERVICE_ROLE_KEY
```

Never expose the publisher key or Supabase service-role key in frontend
variables. Apply the website migration that creates `forecast_proof_outbox`
before enabling the publisher.
