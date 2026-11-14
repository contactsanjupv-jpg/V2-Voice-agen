"""
Re-push every saved receptionist to Retell so existing businesses pick up the mandatory AI/transcription opening
(Level 8D) and the current prompt rules. Nothing else changes: it uses the normal sync path, one agent at a time, and
a failure on one agent never stops the rest.

    python -m app.workers.resync_agents                              # dry run (default): lists the agents it WOULD sync
    python -m app.workers.resync_agents --org <organization uuid>    # dry run for one organization
    python -m app.workers.resync_agents --apply --org <organization uuid>    # really sync ONE organization
    python -m app.workers.resync_agents --apply --all-orgs                   # really sync EVERY organization

A dry run makes no Retell calls and writes nothing. `--apply` refuses to run without an explicit scope (`--org` or
`--all-orgs`), so it can never touch every customer by accident. Run it against the database you mean to update. It
prints agent and organization ids and counts, never prompts or keys.
"""

import argparse
import logging
import uuid

from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models.business import Business
from app.db.models.voice_agent import Agent, Voice
from app.providers.agent.agent_provider import AgentProvider
from app.services.agent_service import sync_agent_to_provider

logger = logging.getLogger("atla.resync_agents")


class ScopeRequired(ValueError):
    """--apply needs --org or --all-orgs."""


def resync_agents(
    db: Session,
    provider: AgentProvider | None = None,
    *,
    apply: bool = False,
    organization_id: uuid.UUID | None = None,
    all_organizations: bool = False,
) -> dict:
    if apply and organization_id is None and not all_organizations:
        raise ScopeRequired(
            "Refusing to apply without a scope: pass --org <uuid> or --all-orgs."
        )
    q = db.query(Agent).filter(
        Agent.retell_agent_id.isnot(None)
    )  # never-synced drafts have nothing to refresh
    if organization_id is not None:
        q = q.filter(Agent.organization_id == organization_id)
    agents = q.order_by(Agent.created_at, Agent.id).all()
    result = {
        "applied": apply,
        "scope": str(organization_id) if organization_id else "all organizations",
        "agents": len(agents),
        "targets": [
            {"agent_id": str(a.id), "organization_id": str(a.organization_id)}
            for a in agents
        ],
        "synced": 0,
        "failed": [],
    }
    if not apply:
        return result
    for agent in agents:
        agent_id = agent.id
        try:
            business = db.get(Business, agent.business_id)
            voice = db.get(Voice, agent.voice_id) if agent.voice_id else None
            if business is None or voice is None:
                raise RuntimeError("business or voice missing")
            sync_agent_to_provider(db, agent, business, voice, provider)
            result["synced"] += 1
        except Exception as e:  # noqa: BLE001 - keep going; the id is reported for a retry
            db.rollback()
            logger.warning("Resync failed [agent=%s]: %s", agent_id, type(e).__name__)
            result["failed"].append(str(agent_id))
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--apply", action="store_true", help="really sync (default is a dry run)"
    )
    ap.add_argument(
        "--org", type=uuid.UUID, default=None, help="only this organization"
    )
    ap.add_argument(
        "--all-orgs",
        action="store_true",
        help="with --apply: every organization (explicit opt-in)",
    )
    args = ap.parse_args()
    if args.all_orgs and args.org:
        ap.error("--org and --all-orgs are mutually exclusive")
    if args.apply and not (args.org or args.all_orgs):
        ap.error("--apply needs a scope: --org <uuid> or --all-orgs")
    db = SessionLocal()
    try:
        r = resync_agents(
            db,
            apply=args.apply,
            organization_id=args.org,
            all_organizations=args.all_orgs,
        )
    finally:
        db.close()
    if r["applied"]:
        print(
            f"APPLIED to {r['scope']}: {r['agents']} agents, {r['synced']} synced, {len(r['failed'])} failed"
        )
        for f in r["failed"]:
            print(f"  failed: {f}")
    else:
        print(
            f"DRY RUN for {r['scope']} (nothing changed): would sync {r['agents']} agents"
        )
        for t in r["targets"]:
            print(
                f"  would sync: agent {t['agent_id']} (organization {t['organization_id']})"
            )


if __name__ == "__main__":
    main()
