// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Forecast AI Proof Network Registry
/// @notice Immutable forecast commitments with deterministic Brier scoring.
/// @dev Resolution is initially performed by approved resolvers. Forecast data
///      cannot be edited after commitment; only its resolution fields can be set.
contract ForecastRegistry {
    struct ForecastInput {
        bytes32 forecastId;
        bytes32 payloadHash;
        bytes32 marketIdHash;
        bytes32 agentId;
        bytes32 categoryId;
        uint16 probabilityBps;
        uint64 closesAt;
    }

    struct ForecastCommitment {
        bytes32 payloadHash;
        bytes32 marketIdHash;
        bytes32 agentId;
        bytes32 categoryId;
        uint16 probabilityBps;
        uint64 committedAt;
        uint64 closesAt;
        uint64 resolvedAt;
        uint32 brierScoreBps;
        bytes32 resolutionHash;
        address submitter;
        bool resolved;
        bool outcome;
    }

    struct ScoreStats {
        uint64 resolvedCount;
        uint256 totalBrierScoreBps;
    }

    address public owner;
    bool public commitmentsPaused;
    mapping(address => bool) public resolvers;
    mapping(bytes32 => address) public agentOwners;
    mapping(bytes32 => ForecastCommitment) public forecasts;
    mapping(bytes32 => ScoreStats) public agentStats;
    mapping(bytes32 => mapping(bytes32 => ScoreStats)) public categoryAgentStats;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event ResolverUpdated(address indexed resolver, bool enabled);
    event CommitmentsPaused(bool paused);
    event AgentRegistered(bytes32 indexed agentId, address indexed agentOwner);
    event AgentOwnerUpdated(bytes32 indexed agentId, address indexed previousOwner, address indexed newOwner);
    event ForecastCommitted(
        bytes32 indexed forecastId,
        bytes32 indexed marketIdHash,
        bytes32 indexed agentId,
        bytes32 payloadHash,
        uint16 probabilityBps,
        uint64 closesAt,
        address submitter
    );
    event ForecastResolved(
        bytes32 indexed forecastId,
        bool outcome,
        uint32 brierScoreBps,
        bytes32 indexed resolutionHash,
        uint64 resolvedAt
    );

    error Unauthorized();
    error InvalidInput();
    error AlreadyExists();
    error NotFound();
    error AlreadyResolved();
    error TooEarly();
    error Paused();

    constructor() {
        owner = msg.sender;
        resolvers[msg.sender] = true;
        emit OwnershipTransferred(address(0), msg.sender);
        emit ResolverUpdated(msg.sender, true);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier onlyResolver() {
        if (!resolvers[msg.sender]) revert Unauthorized();
        _;
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert InvalidInput();
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    function setResolver(address resolver, bool enabled) external onlyOwner {
        if (resolver == address(0)) revert InvalidInput();
        resolvers[resolver] = enabled;
        emit ResolverUpdated(resolver, enabled);
    }

    function setCommitmentsPaused(bool paused) external onlyOwner {
        commitmentsPaused = paused;
        emit CommitmentsPaused(paused);
    }

    function registerAgent(bytes32 agentId) external {
        _registerAgent(agentId, msg.sender);
    }

    function registerAgentFor(bytes32 agentId, address agentOwner) external onlyOwner {
        _registerAgent(agentId, agentOwner);
    }

    function registerAgentBatchFor(bytes32[] calldata agentIds, address agentOwner) external onlyOwner {
        if (agentIds.length == 0 || agentIds.length > 100) revert InvalidInput();
        for (uint256 i = 0; i < agentIds.length; ++i) {
            _registerAgent(agentIds[i], agentOwner);
        }
    }

    /// @notice Rotates an agent publisher without changing its immutable forecast history.
    function setAgentOwner(bytes32 agentId, address newAgentOwner) external onlyOwner {
        address previousOwner = agentOwners[agentId];
        if (agentId == bytes32(0) || previousOwner == address(0) || newAgentOwner == address(0)) {
            revert InvalidInput();
        }
        agentOwners[agentId] = newAgentOwner;
        emit AgentOwnerUpdated(agentId, previousOwner, newAgentOwner);
    }

    function _registerAgent(bytes32 agentId, address agentOwner) internal {
        if (agentId == bytes32(0) || agentOwner == address(0)) revert InvalidInput();
        if (agentOwners[agentId] != address(0)) revert AlreadyExists();
        agentOwners[agentId] = agentOwner;
        emit AgentRegistered(agentId, agentOwner);
    }

    function commitForecast(ForecastInput calldata input) external {
        _commit(input);
    }

    function commitForecastBatch(ForecastInput[] calldata inputs) external {
        if (inputs.length == 0 || inputs.length > 100) revert InvalidInput();
        for (uint256 i = 0; i < inputs.length; ++i) {
            _commit(inputs[i]);
        }
    }

    function _commit(ForecastInput calldata input) internal {
        if (commitmentsPaused) revert Paused();
        if (
            input.forecastId == bytes32(0)
                || input.payloadHash == bytes32(0)
                || input.marketIdHash == bytes32(0)
                || input.agentId == bytes32(0)
                || input.probabilityBps > 10_000
                || input.closesAt <= block.timestamp
        ) revert InvalidInput();
        if (agentOwners[input.agentId] != msg.sender) revert Unauthorized();

        ForecastCommitment storage existing = forecasts[input.forecastId];
        if (existing.committedAt != 0) {
            // Safe retries are idempotent; conflicting data remains impossible.
            if (
                existing.payloadHash == input.payloadHash
                    && existing.marketIdHash == input.marketIdHash
                    && existing.agentId == input.agentId
                    && existing.categoryId == input.categoryId
                    && existing.probabilityBps == input.probabilityBps
                    && existing.closesAt == input.closesAt
                    && existing.submitter == msg.sender
            ) return;
            revert AlreadyExists();
        }

        forecasts[input.forecastId] = ForecastCommitment({
            payloadHash: input.payloadHash,
            marketIdHash: input.marketIdHash,
            agentId: input.agentId,
            categoryId: input.categoryId,
            probabilityBps: input.probabilityBps,
            committedAt: uint64(block.timestamp),
            closesAt: input.closesAt,
            resolvedAt: 0,
            brierScoreBps: 0,
            resolutionHash: bytes32(0),
            submitter: msg.sender,
            resolved: false,
            outcome: false
        });
        emit ForecastCommitted(
            input.forecastId,
            input.marketIdHash,
            input.agentId,
            input.payloadHash,
            input.probabilityBps,
            input.closesAt,
            msg.sender
        );
    }

    function resolveForecast(bytes32 forecastId, bool outcome, bytes32 resolutionHash) external onlyResolver {
        _resolve(forecastId, outcome, resolutionHash);
    }

    function resolveForecastBatch(
        bytes32[] calldata forecastIds,
        bool outcome,
        bytes32 resolutionHash
    ) external onlyResolver {
        if (forecastIds.length == 0 || forecastIds.length > 100) revert InvalidInput();
        for (uint256 i = 0; i < forecastIds.length; ++i) {
            _resolve(forecastIds[i], outcome, resolutionHash);
        }
    }

    function _resolve(bytes32 forecastId, bool outcome, bytes32 resolutionHash) internal {
        if (resolutionHash == bytes32(0)) revert InvalidInput();
        ForecastCommitment storage forecast = forecasts[forecastId];
        if (forecast.committedAt == 0) revert NotFound();
        if (forecast.resolved) {
            if (forecast.outcome == outcome && forecast.resolutionHash == resolutionHash) return;
            revert AlreadyResolved();
        }
        if (block.timestamp < forecast.closesAt) revert TooEarly();

        uint256 errorBps = outcome ? 10_000 - forecast.probabilityBps : forecast.probabilityBps;
        uint32 brierScoreBps = uint32((errorBps * errorBps) / 10_000);
        forecast.resolved = true;
        forecast.outcome = outcome;
        forecast.resolvedAt = uint64(block.timestamp);
        forecast.brierScoreBps = brierScoreBps;
        forecast.resolutionHash = resolutionHash;

        ScoreStats storage total = agentStats[forecast.agentId];
        total.resolvedCount += 1;
        total.totalBrierScoreBps += brierScoreBps;
        ScoreStats storage category = categoryAgentStats[forecast.categoryId][forecast.agentId];
        category.resolvedCount += 1;
        category.totalBrierScoreBps += brierScoreBps;

        emit ForecastResolved(
            forecastId,
            outcome,
            brierScoreBps,
            resolutionHash,
            uint64(block.timestamp)
        );
    }

    function averageAgentBrierScoreBps(bytes32 agentId) external view returns (uint256) {
        ScoreStats memory stats = agentStats[agentId];
        if (stats.resolvedCount == 0) return 0;
        return stats.totalBrierScoreBps / stats.resolvedCount;
    }

    function averageCategoryBrierScoreBps(bytes32 categoryId, bytes32 agentId) external view returns (uint256) {
        ScoreStats memory stats = categoryAgentStats[categoryId][agentId];
        if (stats.resolvedCount == 0) return 0;
        return stats.totalBrierScoreBps / stats.resolvedCount;
    }
}
