from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AgentConfig:
    name: str
    greeting: str | None
    personality: str
    language: str
    voice_provider_id: str  # Retell voice_id
    tasks: dict  # {"answer_questions": bool, "capture_leads": bool, ...}
    transfer_number: str | None
    business_hours: dict | None
    after_hours_behavior: str | None
    knowledge_text: str  # flattened business info + knowledge items, injected as agent context
    webhook_url: str  # per-agent webhook, overrides account default


@dataclass
class ProviderAgent:
    provider_agent_id: str
    raw: dict


class AgentProvider(ABC):
    @abstractmethod
    def create_agent(self, config: AgentConfig) -> ProviderAgent: ...

    @abstractmethod
    def update_agent(self, provider_agent_id: str, config: AgentConfig) -> ProviderAgent: ...

    @abstractmethod
    def delete_agent(self, provider_agent_id: str) -> None: ...
