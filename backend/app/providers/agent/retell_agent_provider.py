"""
Retell implementation. Retell splits a voice agent into two resources:
  POST /create-retell-llm  -> general_prompt, begin_message, tools  (the "brain")
  POST /create-agent       -> voice, webhook, limits, post-call analysis, and a
                              response_engine pointing at the LLM
The prompt and business knowledge live on the LLM, NOT the agent — an agent
created without an LLM has no instructions at all.

Shapes follow Retell's API reference (create-retell-llm, create-agent,
update-*). Anything not confirmed against a live account is marked
UNVERIFIED so the first real run tells us immediately.
"""
from app.providers.agent.agent_provider import AgentConfig, AgentProvider
from app.providers.retell_client import RetellClient

_MIN_CALL_MS = 60_000  # Retell: max_call_duration_ms accepts 1 minute ...
_MAX_CALL_MS = 7_200_000  # ... to 2 hours

# Post-call analysis: how leads get extracted (read back in the call_analyzed
# webhook as call_analysis.custom_analysis_data). UNVERIFIED: exact accepted
# `type` values — string/boolean are documented for Retell analysis fields.
LEAD_ANALYSIS_FIELDS = [
    {
        "type": "boolean",
        "name": "captured_lead",
        "description": (
            "True if the caller left contact details or asked for a callback, quote, "
            "appointment or follow-up from the business. False for hang-ups and simple questions."
        ),
    },
    {"type": "string", "name": "caller_name", "description": "The caller's name if they gave it, otherwise empty."},
    {
        "type": "string",
        "name": "callback_number",
        "description": "The phone number the caller asked to be called back on, otherwise empty.",
    },
    {"type": "string", "name": "reason", "description": "One short sentence: why the caller called."},
]


class RetellAgentProvider(AgentProvider):
    def __init__(self, client: RetellClient | None = None):
        self.client = client or RetellClient()

    # --- LLM (prompt + tools) ---
    def create_llm(self, config: AgentConfig) -> str:
        raw = self.client.request("POST", "/create-retell-llm", json=self._llm_payload(config))
        return raw["llm_id"]

    def update_llm(self, llm_id: str, config: AgentConfig) -> None:
        self.client.request("PATCH", f"/update-retell-llm/{llm_id}", json=self._llm_payload(config))

    def delete_llm(self, llm_id: str) -> None:
        self.client.request("DELETE", f"/delete-retell-llm/{llm_id}")

    # --- Agent (voice + behaviour) ---
    def create_agent(self, config: AgentConfig, llm_id: str) -> str:
        raw = self.client.request("POST", "/create-agent", json=self._agent_payload(config, llm_id))
        return raw["agent_id"]

    def update_agent(self, agent_id: str, config: AgentConfig, llm_id: str) -> None:
        self.client.request("PATCH", f"/update-agent/{agent_id}", json=self._agent_payload(config, llm_id))

    def delete_agent(self, agent_id: str) -> None:
        self.client.request("DELETE", f"/delete-agent/{agent_id}")

    # --- payload builders (pure functions — unit-tested directly) ---
    @staticmethod
    def _llm_payload(config: AgentConfig) -> dict:
        tools: list[dict] = [
            {
                "type": "end_call",
                "name": "end_call",
                "description": "End the call once the caller's needs are handled and you have said goodbye.",
            }
        ]
        if config.transfer_number:
            # Phone calls only — Retell does not run transfers on browser (web) calls.
            tools.append(
                {
                    "type": "transfer_call",
                    "name": "transfer_to_human",
                    "description": "Transfer the call to a person at the business when the caller asks for one.",
                    "transfer_destination": {"type": "predefined", "number": config.transfer_number},
                    "transfer_option": {"type": "cold_transfer"},
                }
            )
        payload: dict = {
            "general_prompt": config.general_prompt,
            "begin_message": config.begin_message,
            "start_speaker": "agent",
            "general_tools": tools,
        }
        if config.llm_model:
            payload["model"] = config.llm_model
        return payload

    @staticmethod
    def _agent_payload(config: AgentConfig, llm_id: str) -> dict:
        max_ms = min(max(config.max_call_seconds * 1000, _MIN_CALL_MS), _MAX_CALL_MS)
        payload: dict = {
            "agent_name": config.name,
            "voice_id": config.voice_provider_id,
            "language": config.language,
            "webhook_url": config.webhook_url,
            "response_engine": {"type": "retell-llm", "llm_id": llm_id},
            "max_call_duration_ms": max_ms,
        }
        if config.capture_leads:
            payload["post_call_analysis_data"] = LEAD_ANALYSIS_FIELDS
        return payload