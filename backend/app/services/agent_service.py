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
from app.services.disclosure import opening_message
from app.services.inbound_routing import routing_enabled
from app.services.prompt_builder import (
    build_agent_prompt,
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


def build_agent_config(
    db: Session, agent: Agent, business: Business, voice: Voice, *, closed: bool = False
) -> AgentConfig:
    """`closed=True` builds the after-hours twin: same voice and knowledge, never a transfer number or tool."""
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
        has_transfer=bool(agent.transfer_number) and not closed,
        knowledge_text=knowledge,
        timezone=business.timezone,
        after_hours=closed,
    )
    return AgentConfig(
        name=agent.name,
        language=agent.language,
        voice_provider_id=voice.retell_voice_id,
        webhook_url=f"{settings.BACKEND_PUBLIC_URL.rstrip('/')}/webhooks/retell",
        general_prompt=prompt,
        begin_message=opening_message(business.name, agent.greeting),
        transfer_number=agent.transfer_number if tasks.get("transfer_calls") and not closed else None,
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

        if routing_enabled(business):
            _sync_closed_twin(db, agent, build_agent_config(db, agent, business, voice, closed=True), provider)

        agent.synced_version = target_version
        db.commit()
        db.refresh(agent)
        return agent


def twin_missing(db: Session, agent: Agent, business: Business | None = None) -> bool:
    """After-hours routing is on for this business but its closed twin has not been created (yet)."""
    business = business or db.get(Business, agent.business_id)
    return routing_enabled(business) and agent.closed_retell_agent_id is None


def is_fully_synced(db: Session, agent: Agent, business: Business | None = None) -> bool:
    """What the owner is told "applied" means: the normal receptionist is current AND, when routing needs it, the twin exists."""
    return (
        agent.retell_agent_id is not None
        and agent.synced_version == agent.version
        and not twin_missing(db, agent, business)
    )


def _sync_closed_twin(db: Session, agent: Agent, config: AgentConfig, provider: AgentProvider) -> None:
    """Create or update the after-hours twin (no transfer tool). Same id-persisting order as the normal agent."""
    if agent.closed_retell_llm_id is None:
        agent.closed_retell_llm_id = provider.create_llm(config)
        db.commit()
    else:
        provider.update_llm(agent.closed_retell_llm_id, config)
    if agent.closed_retell_agent_id is None:
        agent.closed_retell_agent_id = provider.create_agent(config, agent.closed_retell_llm_id)
        db.commit()
    else:
        provider.update_agent(agent.closed_retell_agent_id, config, agent.closed_retell_llm_id)