"""
Activation = make the receptionist answer the org's number, and only report
"live" once the PROVIDER confirms it. Safe to call repeatedly.
"""
import logging
import uuid

from sqlalchemy.orm import Session

from app.core.locks import LockNotAcquired, redis_lock
from app.db.models.telephony import PhoneNumber, PhoneNumberStatus
from app.db.models.voice_agent import Agent, AgentStatus
from app.providers.phone.phone_provider import PhoneProvider
from app.providers.phone.retell_phone_provider import RetellPhoneProvider

logger = logging.getLogger("atla.activation")


class ActivationNotFound(Exception):
    pass


class ActivationBlocked(Exception):
    """A precondition isn't met; `reason` is a customer-safe message."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ActivationUnverified(Exception):
    """The provider did not confirm the assignment."""


def activate_receptionist(
    db: Session, org_id: uuid.UUID, agent_id: uuid.UUID, phone_id: uuid.UUID, provider: PhoneProvider | None = None
) -> Agent:
    try:
        with redis_lock(f"activate:{org_id}", ttl_seconds=60):
            agent = db.query(Agent).filter(Agent.id == agent_id, Agent.organization_id == org_id).first()
            phone = db.query(PhoneNumber).filter(PhoneNumber.id == phone_id, PhoneNumber.organization_id == org_id).first()
            if agent is None or phone is None:
                raise ActivationNotFound()
            if agent.retell_agent_id is None or agent.synced_version != agent.version:
                raise ActivationBlocked("Your latest receptionist changes haven't been applied yet. Save it again, then go live.")
            if phone.status != PhoneNumberStatus.active:
                raise ActivationBlocked("This phone number is no longer active. Get a new number to go live.")

            provider = provider or RetellPhoneProvider()
            current = provider.get_number(phone.retell_phone_number_id)
            if current is None:
                phone.status, phone.agent_id = PhoneNumberStatus.released, None
                db.commit()
                raise ActivationBlocked("We couldn't find this phone number with our provider. Get a new number to go live.")

            if current.inbound_agent_id != agent.retell_agent_id:
                provider.assign_agent(phone.retell_phone_number_id, agent.retell_agent_id)
                current = provider.get_number(phone.retell_phone_number_id)
                if current is None or current.inbound_agent_id != agent.retell_agent_id:
                    raise ActivationUnverified()

            phone.agent_id = agent.id
            agent.status = AgentStatus.active
            db.commit()
            db.refresh(agent)
            return agent
    except LockNotAcquired as e:
        raise ActivationBlocked("Activation is already in progress — one moment.") from e
