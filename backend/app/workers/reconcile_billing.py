"""
Compare local subscriptions with Paddle and (optionally) repair drift.

    python -m app.workers.reconcile_billing            # report only
    python -m app.workers.reconcile_billing --apply    # repair, stale-safe

Prints subscription ids and field names only — never keys or secrets.
"""
import argparse

from app.db.base import SessionLocal
from app.providers.billing.paddle_billing_provider import PaddleBillingProvider
from app.services.billing_reconcile import reconcile_subscriptions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="repair drift (default: report only)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = reconcile_subscriptions(db, PaddleBillingProvider(), apply=args.apply)
    finally:
        db.close()

    print(f"mode: {'APPLY' if args.apply else 'REPORT ONLY'}")
    print(f"checked: {report.checked} | in sync: {report.in_sync} | drifted: {len(report.drifted)} | "
          f"missing at Paddle: {len(report.missing_at_paddle)} | read errors: {len(report.errors)}")
    for d in report.drifted:
        fields = ", ".join(f"{k}: local={v[0]} paddle={v[1]}" for k, v in d.differences.items())
        state = "repaired" if d.repaired else (d.note or "needs --apply")
        print(f"  DRIFT {d.external_subscription_id} (org {d.organization_id}): {fields} -> {state}")
    for sid in report.missing_at_paddle:
        print(f"  MISSING AT PADDLE {sid} — not changed; review manually")
    for oid in report.duplicate_active_orgs:
        print(f"  DUPLICATE ACTIVE SUBSCRIPTIONS for org {oid} — customer may be charged twice; review manually")
    return 1 if (report.drifted and not args.apply) or report.duplicate_active_orgs else 0


if __name__ == "__main__":
    raise SystemExit(main())
