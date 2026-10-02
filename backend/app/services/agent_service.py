"""
Turns the customer's saved receptionist settings into a working Retell
agent. The chain is:

    saved settings (DB)  ->  prompt  ->  Retell LLM  ->  Retell agent

Invariants:
  * Settings are saved to our DB FIRST, so nothing the customer typed is lost
    if Retell is down.
  * Every successful provider step is committed immediately (llm_id, then
    agent_id), so a retry resumes instead of creating duplicates.
  * `synced_version == version` is the ONLY proof Retell has the current
    config. Nothing may tell the customer "saved" or let them test a config
    that isn't synced.
"""
import uuid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.locks import redis_lock
from app.db.models.business import Business, KnowledgeItem
from app.db.models.voice_agent import Agent, AgentStatus, Voice
from app.providers.agent.agent_provider import AgentConfig, AgentProvider
from app.providers.agent.retell_agent_provider import RetellAgentProvider
from app.schemas.catalog import AgentConfigRequest
from app.services.prompt_builder import (
    build_agent_prompt,
    build_default_greeting,
    format_hours,
    truncate_knowledge,
)


def _flatten_knowledge(db: Session, business_id: uuid.UUID) -> str:
    items = (
        db.query(KnowledgeItem)
        .filter(KnowledgeItem.business_id == business_id)
        .order_by(KnowledgeItem.created_at, KnowledgeItem.id)
        .all()
    )
    return "\n\n".join(f"[{i.type.value.upper()}] {i.title}\n{i.content}" for i in items)


def build_agent_config(db: Session, agent: Agent, business: Business, voice: Voice) -> AgentConfig:
    settings = get_settings()
    tasks = agent.tasks or {}
    knowledge = truncate_knowledge(_flatten_knowledge(db, business.id), settings.AGENT_KNOWLEDGE_MAX_CHARS)
    prompt = build_agent_prompt(
        business_name=business.name,
        industry=business.industry,
        address=business.address,
        phone=business.phone,
        website=business.website_url,
        description=business.description,
        hours_text=format_hours(business.hours),
        personality=agent.personality,
        tasks=tasks,
        has_transfer=bool(agent.transfer_number),
        knowledge_text=knowledge,
    )
    return AgentConfig(
        name=agent.name,
        language=agent.language,
        voice_provider_id=voice.retell_voice_id,
        webhook_url=f"{settings.BACKEND_PUBLIC_URL.rstrip('/')}/webhooks/retell",
        general_prompt=prompt,
        begin_message=agent.greeting or build_default_greeting(business.name),
        transfer_number=agent.transfer_number if tasks.get("transfer_calls") else None,
        capture_leads=bool(tasks.get("capture_leads")),
        max_call_seconds=settings.AGENT_MAX_CALL_SECONDS,
        llm_model=settings.RETELL_LLM_MODEL or None,
    )


def save_agent_settings(
    db: Session,
    *,
    organization_id: uuid.UUID,
    business: Business,
    voice: Voice,
    payload: AgentConfigRequest,
) -> Agent:
    """Persist what the customer chose (create or update) — no provider call."""
    agent = (
        db.query(Agent)
        .filter(Agent.business_id == business.id, Agent.organization_id == organization_id)
        .first()
    )
    is_new = agent is None
    if is_new:
        agent = Agent(
            organization_id=organization_id,
            business_id=business.id,
            status=AgentStatus.draft,
            version=1,
            synced_version=0,
        )
        db.add(agent)
    agent.voice_id = voice.id
    agent.name = payload.name
    agent.greeting = payload.greeting
    agent.personality = payload.personality
    agent.language = payload.language
    agent.tasks = payload.tasks
    agent.transfer_number = payload.transfer_number
    agent.business_hours = payload.business_hours
    agent.after_hours_behavior = payload.after_hours_behavior
    if not is_new:
        agent.version += 1
    db.commit()
    db.refresh(agent)
    return agent


def sync_agent_to_provider(
    db: Session,
    agent: Agent,
    business: Business,
    voice: Voice,
    provider: AgentProvider | None = None,
) -> Agent:
    """Push the saved settings to Retell. Raises on failure; safe to retry."""
    with redis_lock(f"agent-sync:{agent.id}", ttl_seconds=60):
        provider = provider or RetellAgentProvider()
        config = build_agent_config(db, agent, business, voice)
        target_version = agent.version

        if agent.retell_llm_id is None:
            agent.retell_llm_id = provider.create_llm(config)
            db.commit()  # persist immediately: a failed agent create must reuse this LLM
        else:
            provider.update_llm(agent.retell_llm_id, config)

        if agent.retell_agent_id is None:
            agent.retell_agent_id = provider.create_agent(config, agent.retell_llm_id)
            db.commit()
        else:
            provider.update_agent(agent.retell_agent_id, config, agent.retell_llm_id)

        agent.synced_version = target_version
        db.commit()
        db.refresh(agent)
        return agent