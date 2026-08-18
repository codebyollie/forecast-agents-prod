const hre = require("hardhat");

async function main() {
  const tokenAddress = process.env.FORAI_TOKEN_ADDRESS;
  if (!tokenAddress) throw new Error("FORAI_TOKEN_ADDRESS is required");
  const token = await hre.ethers.getContractAt(
    ["function decimals() view returns (uint8)"],
    tokenAddress,
  );
  const decimals = await token.decimals();
  const bondTokens = process.env.AGENT_BOND_TOKENS || "200000";
  const bondAmount = hre.ethers.parseUnits(bondTokens, decimals);
  const deployer = (await hre.ethers.getSigners())[0];
  console.log("Deploying AgentBonding from:", deployer.address);
  console.log("FORAI token:", tokenAddress);
  console.log("Bond tokens:", bondTokens, "decimals:", decimals.toString());
  const bonding = await hre.ethers.deployContract("AgentBonding", [tokenAddress, bondAmount]);
  await bonding.waitForDeployment();
  console.log("AgentBonding:", bonding.target);
  console.log("Token approval target:", bonding.target);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
