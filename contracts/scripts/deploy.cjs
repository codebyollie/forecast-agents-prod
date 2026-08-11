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

  const publisherAddress = process.env.PUBLISHER_ADDRESS || deployer.address;
  const resolverAddress = process.env.RESOLVER_ADDRESS || publisherAddress;
  const ownerAddress = process.env.OWNER_ADDRESS || deployer.address;
  for (const [label, value] of Object.entries({ publisherAddress, resolverAddress, ownerAddress })) {
    if (!hre.ethers.isAddress(value)) throw new Error(`${label} is not a valid EVM address.`);
  }
  const network = await hre.ethers.provider.getNetwork();
  if (Number(network.chainId) === 4663) {
    for (const required of ["PUBLISHER_ADDRESS", "RESOLVER_ADDRESS", "OWNER_ADDRESS"]) {
      if (!process.env[required]) throw new Error(`${required} is required for mainnet deployment.`);
    }
    if (deployer.address.toLowerCase() !== publisherAddress.toLowerCase()) {
      throw new Error("Mainnet deployer must match PUBLISHER_ADDRESS for this deployment.");
    }
  }

  console.log("Deploying ForecastRegistry from:", deployer.address);
  const registry = await hre.ethers.deployContract("ForecastRegistry");
  await registry.waitForDeployment();
  const address = await registry.getAddress();

  const agentIds = AGENT_NAMES.map(sha256Bytes32);
  const registration = await registry.registerAgentBatchFor(agentIds, publisherAddress);
  await registration.wait();

  if (resolverAddress.toLowerCase() !== deployer.address.toLowerCase()) {
    await (await registry.setResolver(resolverAddress, true)).wait();
    await (await registry.setResolver(deployer.address, false)).wait();
  }
  if (ownerAddress.toLowerCase() !== deployer.address.toLowerCase()) {
    await (await registry.transferOwnership(ownerAddress)).wait();
  }

  console.log(JSON.stringify({
    network: hre.network.name,
    chainId: Number(network.chainId),
    contractAddress: address,
    owner: ownerAddress,
    publisher: publisherAddress,
    resolver: resolverAddress,
    registeredAgents: AGENT_NAMES,
  }, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
