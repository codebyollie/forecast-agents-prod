const { createHash } = require("node:crypto");
const hre = require("hardhat");

function sha256Bytes32(value) {
  return `0x${createHash("sha256").update(value, "utf8").digest("hex")}`;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function waitUntilChainTimestamp(targetTimestamp) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const block = await hre.ethers.provider.getBlock("latest");
    if (block && Number(block.timestamp) >= targetTimestamp) return;
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
  throw new Error(`Timed out waiting for chain timestamp ${targetTimestamp}.`);
}

async function main() {
  const [signer] = await hre.ethers.getSigners();
  if (!signer) throw new Error("DEPLOYER_PRIVATE_KEY is not configured.");

  const network = await hre.ethers.provider.getNetwork();
  assert(Number(network.chainId) === 46630, "Smoke test is restricted to Robinhood Chain testnet.");

  const registry = await hre.ethers.deployContract("ForecastRegistry");
  await registry.waitForDeployment();
  const contractAddress = await registry.getAddress();

  const agentId = sha256Bytes32("smoke-test-agent");
  await (await registry.registerAgent(agentId)).wait();

  const latestBlock = await hre.ethers.provider.getBlock("latest");
  if (!latestBlock) throw new Error("Unable to read the latest testnet block.");
  const closesAt = Number(latestBlock.timestamp) + 15;
  const uniqueSeed = `${contractAddress}:${latestBlock.number}:${Date.now()}`;
  const forecastId = sha256Bytes32(`forecast:${uniqueSeed}`);
  const payloadHash = sha256Bytes32(`payload:${uniqueSeed}`);
  const marketIdHash = sha256Bytes32(`market:${uniqueSeed}`);
  const categoryId = sha256Bytes32("Smoke Test");

  const commitTx = await registry.commitForecast({
    forecastId,
    payloadHash,
    marketIdHash,
    agentId,
    categoryId,
    probabilityBps: 6_000,
    closesAt,
  });
  await commitTx.wait();

  await waitUntilChainTimestamp(closesAt);
  const resolutionHash = sha256Bytes32(`resolution:${uniqueSeed}:true`);
  const resolveTx = await registry.resolveForecast(forecastId, true, resolutionHash);
  await resolveTx.wait();

  const forecast = await registry.forecasts(forecastId);
  const stats = await registry.agentStats(agentId);
  const averageScore = await registry.averageAgentBrierScoreBps(agentId);

  assert(forecast.resolved === true, "Forecast was not marked resolved.");
  assert(forecast.outcome === true, "Resolved outcome was not stored.");
  assert(Number(forecast.brierScoreBps) === 1_600, "Unexpected Brier score.");
  assert(Number(stats.resolvedCount) === 1, "Agent resolution count was not updated.");
  assert(Number(averageScore) === 1_600, "Agent average Brier score was not updated.");

  console.log(JSON.stringify({
    network: hre.network.name,
    chainId: Number(network.chainId),
    contractAddress,
    forecastId,
    commitTransactionHash: commitTx.hash,
    resolutionTransactionHash: resolveTx.hash,
    outcome: true,
    probabilityBps: 6_000,
    brierScoreBps: Number(forecast.brierScoreBps),
    resolvedCount: Number(stats.resolvedCount),
    status: "passed",
  }, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
