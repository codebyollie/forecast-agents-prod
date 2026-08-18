const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("AgentBonding", function () {
  async function fixture() {
    const [owner, user, other] = await ethers.getSigners();
    const token = await ethers.deployContract("MockERC20");
    const bondAmount = ethers.parseUnits("200000", 18);
    const bonding = await ethers.deployContract("AgentBonding", [token.target, bondAmount, owner.address]);
    await token.mint(user.address, bondAmount * 2n);
    return { owner, user, other, token, bonding, bondAmount };
  }

  it("locks one fixed bond and prevents reuse after unlock", async function () {
    const { user, token, bonding, bondAmount } = await fixture();
    const agentId = ethers.id("agent-one");
    await token.connect(user).approve(bonding.target, bondAmount);
    await expect(bonding.connect(user).lockAgent(agentId)).to.emit(bonding, "AgentLocked");
    expect(await token.balanceOf(bonding.target)).to.equal(bondAmount);
    await expect(bonding.connect(user).lockAgent(agentId)).to.be.revertedWithCustomError(bonding, "AlreadyLocked");
    await expect(bonding.connect(user).unlockAgent(agentId)).to.emit(bonding, "AgentRetired");
    expect(await token.balanceOf(user.address)).to.equal(bondAmount * 2n);
    await expect(bonding.connect(user).lockAgent(agentId)).to.be.revertedWithCustomError(bonding, "RetiredIdentity");
  });

  it("does not let another wallet unlock an agent", async function () {
    const { user, other, token, bonding, bondAmount } = await fixture();
    const agentId = ethers.id("agent-two");
    await token.connect(user).approve(bonding.target, bondAmount);
    await bonding.connect(user).lockAgent(agentId);
    await expect(bonding.connect(other).unlockAgent(agentId)).to.be.revertedWithCustomError(bonding, "NotAgentOwner");
  });

  it("pauses new locks but keeps unlock available", async function () {
    const { owner, user, token, bonding, bondAmount } = await fixture();
    const agentId = ethers.id("agent-three");
    await token.connect(user).approve(bonding.target, bondAmount * 2n);
    await bonding.connect(owner).setLockPaused(true);
    await expect(bonding.connect(user).lockAgent(agentId)).to.be.revertedWithCustomError(bonding, "Paused");
    await bonding.connect(owner).setLockPaused(false);
    await bonding.connect(user).lockAgent(agentId);
    await bonding.connect(owner).setLockPaused(true);
    await expect(bonding.connect(user).unlockAgent(agentId)).to.emit(bonding, "AgentRetired");
  });
});
