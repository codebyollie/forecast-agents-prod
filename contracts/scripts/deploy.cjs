const { createHash } = require("node:crypto");
const hre = require("hardhat");

const AGENT_NAMES = [
  "consensus",
  "market",
  "news",
  "macro",
  "social",
  "reddit",
  "research",
  "onchain",
];

function sha256Bytes32(value) {
  return `0x${createHash("sha256").update(value, "utf8").digest("hex")}`;
}

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  if (!deployer) throw new Error("DEPLOYER_PRIVATE_KEY is not configured.");

  console.log("Deploying ForecastRegistry from:", deployer.address);
  const registry = await hre.ethers.deployContract("ForecastRegistry");
  await registry.waitForDeployment();
  const address = await registry.getAddress();

  const agentIds = AGENT_NAMES.map(sha256Bytes32);
  const registration = await registry.registerAgentBatchFor(agentIds, deployer.address);
  await registration.wait();

  console.log(JSON.stringify({
    network: hre.network.name,
    chainId: Number((await hre.ethers.provider.getNetwork()).chainId),
    contractAddress: address,
    owner: deployer.address,
    publisher: deployer.address,
    registeredAgents: AGENT_NAMES,
  }, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
