// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IAgentBondToken {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @title Forecast AI Agent Bonding
/// @notice Locks a fixed amount of FORAI behind one user-owned agent identity.
/// @dev The bond is deliberately immutable. Unlocking permanently retires the
///      identity, while the ForecastRegistry keeps its historical reputation.
contract AgentBonding {
    struct Bond {
        address owner;
        uint64 lockedAt;
        bool active;
    }

    IAgentBondToken public immutable token;
    uint256 public immutable bondAmount;
    address public owner;
    address public pendingOwner;
    bool public lockPaused;
    uint256 private _reentrancy = 1;

    mapping(bytes32 => Bond) public bonds;
    mapping(bytes32 => bool) public retired;

    event OwnershipTransferStarted(address indexed previousOwner, address indexed pendingOwner);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event LockPauseUpdated(bool paused);
    event AgentLocked(bytes32 indexed agentId, address indexed agentOwner, uint256 amount, uint64 lockedAt);
    event AgentRetired(bytes32 indexed agentId, address indexed agentOwner, uint256 amount, uint64 retiredAt);

    error Unauthorized();
    error InvalidInput();
    error AlreadyLocked();
    error RetiredIdentity();
    error NotAgentOwner();
    error Locked();
    error TransferFailed();
    error Paused();

    constructor(address tokenAddress, uint256 amount) {
        if (tokenAddress == address(0) || amount == 0) revert InvalidInput();
        token = IAgentBondToken(tokenAddress);
        bondAmount = amount;
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier nonReentrant() {
        if (_reentrancy != 1) revert Locked();
        _reentrancy = 2;
        _;
        _reentrancy = 1;
    }

    /// @notice Pauses new locks without blocking users from recovering their bonds.
    function setLockPaused(bool paused) external onlyOwner {
        lockPaused = paused;
        emit LockPauseUpdated(paused);
    }

    /// @notice Starts a two-step ownership transfer to reduce operator mistakes.
    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert InvalidInput();
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(owner, newOwner);
    }

    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert Unauthorized();
        address previousOwner = owner;
        owner = msg.sender;
        pendingOwner = address(0);
        emit OwnershipTransferred(previousOwner, msg.sender);
    }

    /// @notice Locks the immutable bond behind a new identity owned by msg.sender.
    function lockAgent(bytes32 agentId) external nonReentrant {
        if (lockPaused) revert Paused();
        if (agentId == bytes32(0)) revert InvalidInput();
        if (retired[agentId]) revert RetiredIdentity();
        if (bonds[agentId].owner != address(0)) revert AlreadyLocked();
        _safeTransferFrom(msg.sender, address(this), bondAmount);
        bonds[agentId] = Bond({ owner: msg.sender, lockedAt: uint64(block.timestamp), active: true });
        emit AgentLocked(agentId, msg.sender, bondAmount, uint64(block.timestamp));
    }

    /// @notice Returns the bond and permanently retires this identity.
    function unlockAgent(bytes32 agentId) external nonReentrant {
        Bond memory bond = bonds[agentId];
        if (bond.owner != msg.sender || !bond.active) revert NotAgentOwner();
        delete bonds[agentId];
        retired[agentId] = true;
        _safeTransfer(msg.sender, bondAmount);
        emit AgentRetired(agentId, msg.sender, bondAmount, uint64(block.timestamp));
    }

    function _safeTransferFrom(address from, address to, uint256 amount) internal {
        (bool success, bytes memory returndata) = address(token).call(
            abi.encodeWithSelector(IAgentBondToken.transferFrom.selector, from, to, amount)
        );
        if (!success || (returndata.length > 0 && !abi.decode(returndata, (bool)))) revert TransferFailed();
    }

    function _safeTransfer(address to, uint256 amount) internal {
        (bool success, bytes memory returndata) = address(token).call(
            abi.encodeWithSelector(IAgentBondToken.transfer.selector, to, amount)
        );
        if (!success || (returndata.length > 0 && !abi.decode(returndata, (bool)))) revert TransferFailed();
    }
}
