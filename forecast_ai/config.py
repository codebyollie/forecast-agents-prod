"""
Unified Configuration for Forecast AI.

Dataclass-based configuration settings for LLMs, Polymarket endpoints,
Consensus Engine calibration, specialized agents, memory, and user profiles.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List

@dataclass
class ProviderConfig:
    provider: str = "openai"  # "openai" | "anthropic" | "gemini" | "ollama" | "openrouter"
    api_key: str = ""
    api_base: str = ""
    model_id: str = "gpt-4o"
    temperature: float = 0.2
    max_tokens: int = 2000

@dataclass
class PolymarketConfig:
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"

@dataclass
class KalshiConfig:
    api_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    api_key: str = ""   # Optional, for authenticated endpoints

@dataclass
class RobinhoodAgenticConfig:
    mcp_endpoint: str = "https://agent.robinhood.com/mcp/trading"
    enabled: bool = False  # Per-user manual connection

@dataclass
class RobinhoodChainConfig:
    stock_tokens_enabled: bool = True
    stock_token_api_url: str = "https://api.robinhood.com/rhj"
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    chain_id: int = 4663
    explorer_url: str = "https://robinhoodchain.blockscout.com"
    registry_address: str = ""
    proof_enabled: bool = False
    publisher_private_key: str = ""
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    publish_interval_seconds: int = 15
    resolution_interval_seconds: int = 300

@dataclass
class AgentSettings:
    enabled: bool = True
    provider: str = "openai"    # Provider name to route queries to
    fallback_providers: List[str] = field(default_factory=list)
    temperature: float = 0.3
    max_sources_to_query: int = 5
    weight: float = 1.0         # Default reliability weight for consensus

@dataclass
class ConsensusConfig:
    min_evidence_score: float = 0.3
    uncertainty_penalty: float = 0.1
    default_agent_weight: float = 1.0
    calibration_alpha: float = 0.05  # Learning rate to adjust agent weight based on historical error

@dataclass
class MemoryConfig:
    store_dir: str = "memory_data"
    max_history_entries: int = 1000
    enable_reputation_updates: bool = True

@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 30000
    api_key: str = ""

@dataclass
class FactsAIConfig:
    enabled: bool = False  # Off by default to prevent unintended API costs
    api_key: str = ""
    api_url: str = "https://deep-research-api.degodmode3-33.workers.dev/answer"
    query_max_length: int = 1000
    rate_limit_per_min: int = 100
    coarse_refresh_interval_cycles: int = 2

@dataclass
class FalconConfig:
    """Falcon / Polymarket Analytics partner intelligence."""
    enabled: bool = False
    api_token: str = ""
    api_url: str = "https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized"
    timeout_seconds: float = 30.0
    market_insights_agent_id: int = 575
    kalshi_markets_agent_id: int = 565
    social_pulse_agent_id: int = 585
    falcon_score_agent_id: int = 584
    polymarket_trades_agent_id: int = 556
    social_enabled: bool = False
    smart_money_enabled: bool = False

@dataclass
class BravadoConfig:
    """Bravado Polymarket trader analytics partner layer."""
    enabled: bool = False
    api_token: str = ""
    api_url: str = "https://partner-api.bravadotrade.com/trader-analytics"
    timeout_seconds: float = 20.0
    leaderboard_window: str = "all"
    scan_limit: int = 12
    min_trades: int = 20

@dataclass
class TavilyConfig:
    enabled: bool = False
    api_key: str = ""

@dataclass
class SourcesConfig:
    news_api_key: str = ""
    twitter_bearer_token: str = ""
    polygonscan_key: str = ""

@dataclass
class ForecastConfig:
    providers: Dict[str, ProviderConfig] = field(default_factory=lambda: {
        "openai": ProviderConfig(provider="openai", model_id="gpt-4o"),
        "anthropic": ProviderConfig(provider="anthropic", model_id="claude-3-5-sonnet-latest"),
        "gemini": ProviderConfig(provider="gemini", model_id="gemini-flash-latest"),
        "ollama": ProviderConfig(provider="ollama", api_base="http://localhost:11434", model_id="llama3"),
        "openrouter": ProviderConfig(provider="openrouter", model_id="meta-llama/llama-3.1-405b"),
    })
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    kalshi: KalshiConfig = field(default_factory=KalshiConfig)
    robinhood_agentic: RobinhoodAgenticConfig = field(default_factory=RobinhoodAgenticConfig)
    robinhood_chain: RobinhoodChainConfig = field(default_factory=RobinhoodChainConfig)
    facts_ai: FactsAIConfig = field(default_factory=FactsAIConfig)
    falcon: FalconConfig = field(default_factory=FalconConfig)
    bravado: BravadoConfig = field(default_factory=BravadoConfig)
    tavily: TavilyConfig = field(default_factory=TavilyConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    agents: Dict[str, AgentSettings] = field(default_factory=lambda: {
        "news": AgentSettings(enabled=True, weight=1.2),
        "social": AgentSettings(enabled=True, weight=0.8),
        "reddit": AgentSettings(enabled=True, weight=0.6),
        "research": AgentSettings(enabled=True, weight=1.4),
        "macro": AgentSettings(enabled=True, weight=1.1),
        "onchain": AgentSettings(enabled=True, weight=1.3),
        "market": AgentSettings(enabled=True, weight=1.5),
    })
    consensus: ConsensusConfig = field(default_factory=ConsensusConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    default_provider: str = "openai"
    fallback_providers: List[str] = field(default_factory=lambda: [
        "gemini",
        "anthropic",
        "openrouter"
    ])
