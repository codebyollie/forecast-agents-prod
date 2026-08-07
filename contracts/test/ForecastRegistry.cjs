const { expect } = require("chai");
const { ethers } = require("hardhat");
const { time } = require("@nomicfoundation/hardhat-network-helpers");

describe("ForecastRegistry", function () {
  let registry;
  let owner;
  let publisher;
  let resolver;
  let input;
  let resolutionHash;

  const digest = (value) => ethers.sha256(ethers.toUtf8Bytes(value));

  beforeEach(async function () {
    [owner, publisher, resolver] = await ethers.getSigners();
    registry = await ethers.deployContract("ForecastRegistry");
    await registry.waitForDeployment();
    await registry.registerAgentFor(digest("market"), publisher.address);
    await registry.setResolver(resolver.address, true);
    input = {
      forecastId: digest("forecast-1"),
      payloadHash: digest("payload-1"),
      marketIdHash: digest("market-1"),
      agentId: digest("market"),
      categoryId: digest("Politics"),
      probabilityBps: 6000,
      closesAt: (await time.latest()) + 3600,
    };
    resolutionHash = digest("official-resolution");
  });

  it("commits immutable data and permits an identical retry", async function () {
    await registry.connect(publisher).commitForecast(input);
    await expect(registry.connect(publisher).commitForecast(input)).not.to.be.reverted;

    await expect(
      registry.connect(publisher).commitForecast({ ...input, probabilityBps: 6100 })
    ).to.be.revertedWithCustomError(registry, "AlreadyExists");
    await expect(
      registry.connect(resolver).commitForecast({ ...input, forecastId: digest("forecast-2") })
    ).to.be.revertedWithCustomError(registry, "Unauthorized");
  });

  it("supports an owner circuit breaker and publisher-key rotation", async function () {
    await registry.setCommitmentsPaused(true);
    await expect(
      registry.connect(publisher).commitForecast(input)
    ).to.be.revertedWithCustomError(registry, "Paused");

    await registry.setCommitmentsPaused(false);
    await registry.setAgentOwner(digest("market"), resolver.address);
    await expect(
      registry.connect(publisher).commitForecast(input)
    ).to.be.revertedWithCustomError(registry, "Unauthorized");
    await expect(registry.connect(resolver).commitForecast(input)).not.to.be.reverted;
  });

  it("resolves after close and records deterministic Brier stats once", async function () {
    await registry.connect(publisher).commitForecast(input);
    await expect(
      registry.connect(resolver).resolveForecast(input.forecastId, true, resolutionHash)
    ).to.be.revertedWithCustomError(registry, "TooEarly");

    await time.increaseTo(input.closesAt);
    await registry.connect(resolver).resolveForecast(input.forecastId, true, resolutionHash);
    expect(await registry.averageAgentBrierScoreBps(digest("market"))).to.equal(1600);

    await expect(
      registry.connect(resolver).resolveForecast(input.forecastId, true, resolutionHash)
    ).not.to.be.reverted;
    expect((await registry.agentStats(digest("market"))).resolvedCount).to.equal(1);
    await expect(
      registry.connect(resolver).resolveForecast(input.forecastId, false, resolutionHash)
    ).to.be.revertedWithCustomError(registry, "AlreadyResolved");
  });

  it("rejects unauthorised or unverifiable resolution", async function () {
    await registry.connect(publisher).commitForecast(input);
    await time.increaseTo(input.closesAt);
    await expect(
      registry.connect(publisher).resolveForecast(input.forecastId, true, resolutionHash)
    ).to.be.revertedWithCustomError(registry, "Unauthorized");
    await expect(
      registry.connect(resolver).resolveForecast(input.forecastId, true, ethers.ZeroHash)
    ).to.be.revertedWithCustomError(registry, "InvalidInput");
  });
});
