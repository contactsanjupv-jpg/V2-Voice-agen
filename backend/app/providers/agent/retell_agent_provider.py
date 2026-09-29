"""
Real implementation against Retell's documented agent endpoints
(docs.retellai.com/api-references/create-agent, get-agent, update-agent,
delete-agent). An agent pairs a "response engine" (we use Retell's own
Retell-LLM response engine, not a custom LLM websocket — no need to run
our own inference infra) with voice + call-behavior settings.

`_build_prompt` is the one place our simple customer-facing toggles
(greeting, personality, tasks) get translated into an actual system
prompt — see app/services/agent_service.py for the higher-level
orchestration (including prompt-injection defenses, §40 of the spec: the
customer's business knowledge is inserted into the prompt as clearly
delimited, non-instructional context, never concatenated in a way an
attacker-controlled website could pass off as a system directive).
"""
from app.providers.agent.agent_provider import AgentConfig, AgentProvider, ProviderAgent
from app.providers.retell_client import RetellClient


class RetellAgentProvider(AgentProvider):
    def __init__(self, client: RetellClient | None = None):
        self.client = client or RetellClient()

    def create_agent(self, config: AgentConfig) -> ProviderAgent:
        payload = self._build_payload(config)
        raw = self.client.request("POST", "/create-agent", json=payload)
        return ProviderAgent(provider_agent_id=raw["agent_id"], raw=raw)

    def update_agent(self, provider_agent_id: str, config: AgentConfig) -> ProviderAgent:
        payload = self._build_payload(config)
        raw = self.client.request("PATCH", f"/update-agent/{provider_agent_id}", json=payload)
        return ProviderAgent(provider_agent_id=provider_agent_id, raw=raw)

    def delete_agent(self, provider_agent_id: str) -> None:
        self.client.request("DELETE", f"/delete-agent/{provider_agent_id}")

    @staticmethod
    def _build_payload(config: AgentConfig) -> dict:
        return {
            "agent_name": config.name,
            "voice_id": config.voice_provider_id,
            "language": config.language,
            "webhook_url": config.webhook_url,
            "response_engine": {
                "type": "retell-llm",
                "llm_id": None,  # created/managed as a linked Retell LLM resource; see agent_service
            },
            # Our simplified config, flattened into Retell's prompt/behavior
            # fields by agent_service._build_prompt() before this payload is
            # constructed — kept out of this low-level provider so provider
            # code stays a thin, swappable translation layer.
            "metadata": {
                "atla_tasks": config.tasks,
                "atla_transfer_number": config.transfer_number,
                "atla_business_hours": config.business_hours,
                "atla_after_hours_behavior": config.after_hours_behavior,
            },
        }
