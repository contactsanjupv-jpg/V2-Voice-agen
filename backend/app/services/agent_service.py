"""
Orchestrates turning our simple customer-facing agent config into a real
Retell agent — this is where AgentProvider, the prompt builder, and the
DB row all meet.
"""
import uuid

from sqlalchemy.orm import Session

from app.db.models.business import Business, KnowledgeItem
from app.db.models.voice_agent import Agent, AgentStatus, Voice
from app.providers.agent.agent_provider import AgentConfig
from app.providers.agent.retell_agent_provider import RetellAgentProvider
from app.services.prompt_builder import build_agent_prompt


def _flatten_knowledge(db: Session, business_id: uuid.UUID) -> str:
    items = db.query(KnowledgeItem).filter(KnowledgeItem.business_id == business_id).all()
    return "\n\n".join(f"[{i.type.value.upper()}] {i.title}\n{i.content}" for i in items)


def create_or_update_agent(
    db: Session,
    *,
    organization_id: uuid.UUID,
    business: Business,
    agent: Agent | None,
    voice: Voice,
    webhook_base_url: str,
    provider: RetellAgentProvider | None = None,
) -> Agent:
    provider = provider or RetellAgentProvider()
    knowledge_text = _flatten_knowledge(db, business.id)
    hours_text = str(business.hours) if business.hours else None

    prompt = build_agent_prompt(
        business_name=business.name,
        greeting=agent.greeting if agent else None,
        personality=agent.personality if agent else "professional",
        tasks=agent.tasks if agent else {},
        knowledge_text=knowledge_text,
        business_hours_text=hours_text,
    )

    config = AgentConfig(
        name=(agent.name if agent else business.name + " Receptionist"),
        greeting=agent.greeting if agent else None,
        personality=agent.personality if agent else "professional",
        language=agent.language if agent else "en-US",
        voice_provider_id=voice.retell_voice_id,
        tasks=agent.tasks if agent else {},
        transfer_number=agent.transfer_number if agent else None,
        business_hours=agent.business_hours if agent else None,
        after_hours_behavior=agent.after_hours_behavior if agent else None,
        knowledge_text=prompt,
        webhook_url=f"{webhook_base_url}/webhooks/retell",
    )

    if agent is None:
        provider_agent = provider.create_agent(config)
        agent = Agent(
            organization_id=organization_id,
            business_id=business.id,
            voice_id=voice.id,
            retell_agent_id=provider_agent.provider_agent_id,
            name=config.name,
            greeting=config.greeting,
            personality=config.personality,
            language=config.language,
            tasks=config.tasks,
            transfer_number=config.transfer_number,
            business_hours=config.business_hours,
            after_hours_behavior=config.after_hours_behavior,
            status=AgentStatus.draft,
        )
        db.add(agent)
    else:
        provider.update_agent(agent.retell_agent_id, config)
        agent.voice_id = voice.id
        agent.version += 1

    db.commit()
    db.refresh(agent)
    return agent
