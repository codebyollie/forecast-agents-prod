# ForecastRegistry deployment

`ForecastRegistry.sol` commits immutable SHA-256 forecast payloads, resolves
official binary outcomes, calculates Brier Scores, and maintains per-agent and
per-category score totals.

`AgentBonding.sol` is the separate mainnet-ready `$FORAI` bond contract for
user-created identities. It locks a fixed amount per identity, permits only
the agent owner to unlock, permanently retires an identity after unlock, and
has no admin token-withdrawal function. The owner can pause new locks and
transfer ownership in two steps, but cannot change the bond amount.

The first deployment must use Robinhood Chain testnet. Mainnet should only be
selected after a successful commit/resolve test and contract review.

## GitHub deployment

1. Create a temporary deployer wallet, a dedicated low-balance publisher
   wallet, and a separate owner wallet. Do not use a treasury wallet in Railway.
2. Add only the deployer private key to the private backend repository as the
   GitHub environment secret `FORECAST_DEPLOYER_PRIVATE_KEY`.
3. Add `FORECAST_PUBLISHER_ADDRESS`, `FORECAST_RESOLVER_ADDRESS`, and
   `FORECAST_OWNER_ADDRESS` as GitHub environment variables. Publisher and
   resolver may be the same automation wallet.
4. Fund the deployer and publisher wallets with testnet gas.
5. Open **Actions → ForecastRegistry → Run workflow** and select
   `robinhood-testnet`.
6. Copy the `contractAddress` from the deploy job output.

The deployment registers these identities to the publisher wallet:
`consensus`, `market`, `news`, `macro`, `social`, `reddit`, `research`, and
`onchain`. Consensus is an aggregate record, not an eighth forecasting agent.

## Railway backend variables for testnet

```env
ROBINHOOD_CHAIN_RPC_URL=https://rpc.testnet.chain.robinhood.com
ROBINHOOD_CHAIN_ID=46630
ROBINHOOD_CHAIN_EXPLORER_URL=https://explorer.testnet.chain.robinhood.com
FORECAST_REGISTRY_ADDRESS=0x_DEPLOYED_CONTRACT
FORECAST_PROOF_ENABLED=true
FORECAST_PUBLISHER_PRIVATE_KEY=0x_DEDICATED_SERVICE_WALLET_PRIVATE_KEY
FORECAST_PROOF_PUBLISH_INTERVAL_SECONDS=15
FORECAST_PROOF_RESOLUTION_INTERVAL_SECONDS=300
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVER_ONLY_SERVICE_ROLE_KEY
```

Never expose the publisher key or Supabase service-role key in frontend
variables. Apply the website migration that creates `forecast_proof_outbox`
and the later proof-network hardening migration before enabling the publisher.

The contract includes an owner circuit breaker for new commitments, publisher
key rotation, idempotent transaction retries, resolver authorization, and
deterministic per-agent/per-category Brier statistics. Resolution remains
available while new commitments are paused.

## AgentBonding mainnet deployment

Deploy only after independent contract review and a dry-run against the real
FORAI token. The deployment script reads the token decimals and fixes the bond
at `AGENT_BOND_TOKENS` (default `200000`).

```env
FORAI_TOKEN_ADDRESS=0xcc9c1ec224c3824ae5ea699ec72ef5fad4165e49
AGENT_BOND_TOKENS=200000
AGENT_BOND_OWNER_ADDRESS=0x_OWNER_WALLET
RH_MAINNET_RPC_URL=https://YOUR_ALCHEMY_MAINNET_ENDPOINT
DEPLOYER_PRIVATE_KEY=0x_MAINNET_DEPLOYER_KEY
```

```bash
npm install
npm test
npm run compile
npm run deploy:agent-bond:mainnet
```

Record the resulting `AgentBonding` address. Never put the deployer private
key in the frontend or Railway frontend variables.
