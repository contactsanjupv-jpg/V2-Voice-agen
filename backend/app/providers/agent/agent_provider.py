from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """Everything the provider needs to build one working receptionist."""

    name: str
    language: str
    voice_provider_id: str  # Retell voice_id
    webhook_url: str  # per-agent webhook, overrides the account default
    general_prompt: str  # the FULL runtime prompt (rules + business knowledge)
    begin_message: str  # first thing the receptionist says
    transfer_number: str | None = None  # E.164; enables the transfer tool when set
    capture_leads: bool = True  # enables post-call lead extraction
    max_call_seconds: int = 900
    llm_model: str | None = None  # None = provider default
    extra: dict = field(default_factory=dict)


class AgentProvider(ABC):
    """
    Two provider-side resources per receptionist: an LLM (prompt + tools) and
    an agent (voice + call behaviour) that points at it. They are separate
    calls, so the caller persists the LLM id before creating the agent — a
    failed second call must be retryable without creating a second LLM.
    """

    @abstractmethod
    def create_llm(self, config: AgentConfig) -> str: ...

    @abstractmethod
    def update_llm(self, llm_id: str, config: AgentConfig) -> None: ...

    @abstractmethod
    def create_agent(self, config: AgentConfig, llm_id: str) -> str: ...

    @abstractmethod
    def update_agent(self, agent_id: str, config: AgentConfig, llm_id: str) -> None: ...

    @abstractmethod
    def delete_agent(self, agent_id: str) -> None: ...

    @abstractmethod
    def delete_llm(self, llm_id: str) -> None: ...