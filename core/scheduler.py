# core/scheduler.py
# APScheduler for AP Automation System
# Handles: 48hr reminders, 96hr escalations, daily IIF, month-end reimbursement

import logging
import os
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler singleton
# ---------------------------------------------------------------------------

_scheduler: AsyncIOScheduler = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


# ---------------------------------------------------------------------------
# Job: Check pending approvals for reminders and escalations
# Runs every hour
# ---------------------------------------------------------------------------

async def job_check_pending_approvals():
    """
    Scan all bills with Approval_Status = Pending.
    - 48hr since sent: send Slack reminder
    - 96hr since sent: send Slack escalation to Principal
    """
    try:
        from integrations.airtable_client import get_airtable_client
        from integrations.slack_client import send_approval_message, post_notification

        airtable = get_airtable_client()
        now = datetime.utcnow()

        # Get all pending bills
        formula = "{Approval_Status}='Pending'"
        records = airtable._table("Bills").all(formula=formula)

        reminded = 0
        escalated = 0

        for record in records:
            fields = record["fields"]
            bill_id = record["id"]
            checked_at_str = fields.get("Compliance_Checked_At", "")

            if not checked_at_str:
                continue

            try:
                sent_at = datetime.fromisoformat(checked_at_str.replace("Z", "+00:00"))
                sent_at = sent_at.replace(tzinfo=None)
            except ValueError:
                continue

            hours_elapsed = (now - sent_at).total_seconds() / 3600

            invoice_number = fields.get("Invoice_Number", bill_id)
            vendor_name = fields.get("Vendor_Name", "Unknown")
            amount = float(fields.get("Total_Amount", 0))
            entity_name = fields.get("Entity_Name", "")
            project_name = fields.get("Project_Name", "")

            # 96hr escalation (check first so we don't double-notify)
            if hours_elapsed >= 96 and not fields.get("Escalated_At"):
                send_approval_message(
                    bill_id=bill_id,
                    invoice_number=invoice_number,
                    vendor_name=vendor_name,
                    amount=amount,
                    entity_name=entity_name,
                    project_name=project_name,
                    gl_lines=[],
                    compliance_summary="ESCALATION: No response after 96 hours.",
                    is_escalation=True,
                )
                airtable.update_bill(bill_id, {
                    "Escalated_At": now.isoformat(),
                })
                escalated += 1
                logger.warning(f"Escalated bill {bill_id} after 96hr.")

            # 48hr reminder (only if not yet reminded and not yet escalated)
            elif hours_elapsed >= 48 and not fields.get("Reminded_At"):
                send_approval_message(
                    bill_id=bill_id,
                    invoice_number=invoice_number,
                    vendor_name=vendor_name,
                    amount=amount,
                    entity_name=entity_name,
                    project_name=project_name,
                    gl_lines=[],
                    compliance_summary="Reminder: This invoice is still awaiting approval.",
                    is_reminder=True,
                )
                airtable.update_bill(bill_id, {
                    "Reminded_At": now.isoformat(),
                })
                reminded += 1
                logger.info(f"Sent 48hr reminder for bill {bill_id}.")

        logger.info(
            f"Approval check complete: "
            f"{len(records)} pending | {reminded} reminded | {escalated} escalated"
        )

    except Exception as e:
        logger.error(f"job_check_pending_approvals failed: {e}")


# ---------------------------------------------------------------------------
# Job: Daily IIF generation
# Runs every day at 06:00 UTC (operator morning routine)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Job: Daily Positive Pay file generation
# Runs every day at 06:30 UTC (after IIF generation)
# ---------------------------------------------------------------------------

async def job_daily_positive_pay():
    """
    Generate Positive Pay check register files for all entities.
    Sends to exports/positive_pay/{entity_name}/YYYY-MM-DD.csv
    Operator uploads to bank portal each morning.
    """
    try:
        from integrations.airtable_client import get_airtable_client
        from integrations.positive_pay import (
            get_positive_pay_generator, PositivePayBatch, CheckRecord
        )
        from decimal import Decimal
        from datetime import date

        airtable = get_airtable_client()
        generator = get_positive_pay_generator()
        today = date.today()

        # Get all approved bills synced today (these generated checks)
        formula = (
            f"AND({{Approval_Status}}='Approved', "
            f"{{QB_Sync_Status}}='Synced')"
        )
        records = airtable._table("Bills").all(formula=formula)

        if not records:
            logger.info("Positive Pay: No synced bills today.")
            return

        # Group by entity
        by_entity: dict[str, list] = {}
        for record in records:
            entity = record["fields"].get("Entity_Name", "Unknown")
            if entity not in by_entity:
                by_entity[entity] = []
            by_entity[entity].append(record)

        generated = []
        for entity_name, bills in by_entity.items():
            checks = []
            for i, record in enumerate(bills):
                fields = record["fields"]
                check_num = fields.get("Check_Number", f"AUTO{i+1:04d}")
                amount = Decimal(str(fields.get("Total_Amount", 0)))
                vendor = fields.get("Vendor_Name", "Unknown")
                invoice = fields.get("Invoice_Number", "")

                checks.append(CheckRecord(
                    check_number=check_num,
                    amount=amount,
                    payee_name=vendor,
                    issue_date=today,
                    account_number=fields.get("Bank_Account", ""),
                    routing_number=fields.get("Routing_Number", ""),
                    memo=invoice,
                ))

            batch = PositivePayBatch(
                entity_name=entity_name,
                bank_name="",
                account_number=checks[0].account_number if checks else "",
                routing_number=checks[0].routing_number if checks else "",
                generation_date=today,
                checks=checks,
            )
            path = generator.generate(batch)
            if path:
                generated.append(path)
                logger.info(f"Positive Pay generated for {entity_name}: {path}")

        logger.info(f"Daily Positive Pay: {len(generated)} files generated.")

    except Exception as e:
        logger.error(f"job_daily_positive_pay failed: {e}")


async def job_daily_iif_generation():
    """
    Generate IIF files for all active entities with approved pending bills.
    Saves to exports/iif/{entity_name}/YYYY-MM-DD.iif
    """
    try:
        from integrations.airtable_client import get_airtable_client
        from integrations.iif_generator import (
            get_iif_generator, IIFBatch, ApprovedBill, BillLineItem
        )
        from decimal import Decimal
        from datetime import date
        import json

        airtable = get_airtable_client()
        generator = get_iif_generator()
        today = date.today()

        # Get all approved pending bills grouped by entity
        records = airtable.get_approved_bills()
        if not records:
            logger.info("Daily IIF: No approved bills pending sync.")
            return

        # Group by entity
        by_entity: dict[str, list] = {}
        for record in records:
            entity = record.get("Entity_Name", "Unknown")
            if entity not in by_entity:
                by_entity[entity] = []
            by_entity[entity].append(record)

        generated = []
        for entity_name, bills in by_entity.items():
            approved_bills = []
            for record in bills:
                # Parse line items from JSON field
                line_items_raw = record.get("Line_Items_JSON", "[]")
                try:
                    line_items_data = json.loads(line_items_raw)
                except (json.JSONDecodeError, TypeError):
                    line_items_data = []

                line_items = [
                    BillLineItem(
                        gl_account=item.get("gl_account", "6000"),
                        amount=Decimal(str(item.get("amount", 0))),
                        cs_code=item.get("cs_code", "UNKNOWN"),
                        description=item.get("description", ""),
                    )
                    for item in line_items_data
                ]

                if not line_items:
                    continue

                invoice_date_str = record.get("Invoice_Date", today.isoformat())
                due_date_str = record.get("Due_Date", "")

                try:
                    invoice_date = date.fromisoformat(invoice_date_str)
                except ValueError:
                    invoice_date = today

                due_date = None
                if due_date_str:
                    try:
                        due_date = date.fromisoformat(due_date_str)
                    except ValueError:
                        pass

                approved_bills.append(ApprovedBill(
                    bill_id=record["id"],
                    entity_name=entity_name,
                    vendor_name=record.get("Vendor_Name", "Unknown"),
                    invoice_number=record.get("Invoice_Number", "UNKNOWN"),
                    invoice_date=invoice_date,
                    due_date=due_date,
                    ap_account=record.get("AP_Account", "2000"),
                    project_name=record.get("Project_Name", ""),
                    line_items=line_items,
                ))

            if not approved_bills:
                continue

            batch = IIFBatch(
                entity_name=entity_name,
                generation_date=today,
                bills=approved_bills,
            )
            path = generator.generate(batch)

            if path:
                generated.append(path)
                # Mark bills as synced
                for bill in approved_bills:
                    airtable.mark_bill_qb_synced(bill.bill_id)

                logger.info(
                    f"IIF generated for {entity_name}: "
                    f"{len(approved_bills)} bills → {path}"
                )

        logger.info(
            f"Daily IIF complete: "
            f"{len(by_entity)} entities | {len(generated)} files generated"
        )

    except Exception as e:
        logger.error(f"job_daily_iif_generation failed: {e}")


# ---------------------------------------------------------------------------
# Job: Month-end intercompany reimbursement
# Runs on 1st of each month at 07:00 UTC
# ---------------------------------------------------------------------------

async def job_month_end_reimbursement():
    """
    Generate intercompany reimbursement invoices for all project LLCs
    that had billable CC charges in the prior month.
    """
    try:
        from agents.cc_charge_agent import get_cc_charge_agent

        now = datetime.utcnow()
        # Prior month
        if now.month == 1:
            year, month = now.year - 1, 12
        else:
            year, month = now.year, now.month - 1

        agent = get_cc_charge_agent()
        summary = agent.generate_month_end_reimbursement(year=year, month=month)

        if summary:
            from integrations.slack_client import post_notification
            lines = "\n".join(
                f"• {proj}: {data['charge_count']} charges, "
                f"${data['total_amount']:,.2f} | IIF: {data.get('iif_path', 'N/A')}"
                for proj, data in summary.items()
            )
            post_notification(
                f"📊 Month-end reimbursement complete for *{year}-{month:02d}*:\n{lines}"
            )
            logger.info(f"Month-end reimbursement: {len(summary)} projects processed.")
        else:
            logger.info(f"Month-end reimbursement: No billable charges for {year}-{month:02d}.")

    except Exception as e:
        logger.error(f"job_month_end_reimbursement failed: {e}")


# ---------------------------------------------------------------------------
# Job: DocuSign compliance reminder check
# Runs every 6 hours
# ---------------------------------------------------------------------------

async def job_compliance_reminder_check():
    """
    Check for vendors with pending DocuSign envelopes.
    DocuSign handles its own reminders (3/7/14 days) via envelope settings.
    This job logs envelope status and flags expired ones.
    """
    try:
        from integrations.airtable_client import get_airtable_client
        from integrations.docusign_client import get_docusign_client

        airtable = get_airtable_client()
        docusign = get_docusign_client()

        formula = "{Compliance_Status}='Pending'"
        records = airtable._table("Bills").all(formula=formula)

        for record in records:
            fields = record["fields"]
            envelope_id = fields.get("DocuSign_Envelope_ID", "")
            if not envelope_id:
                continue

            try:
                status = docusign.get_envelope_status(envelope_id)
                if status.get("status") == "voided":
                    airtable.update_bill_compliance_status(
                        record["id"],
                        "Awaiting Docs",
                        f"Envelope {envelope_id} expired or voided. Re-send required.",
                    )
                    logger.warning(f"Envelope {envelope_id} voided for bill {record['id']}.")
            except Exception as e:
                logger.warning(f"Could not check envelope {envelope_id}: {e}")

        logger.info(f"Compliance check: {len(records)} pending bills checked.")

    except Exception as e:
        logger.error(f"job_compliance_reminder_check failed: {e}")


# ---------------------------------------------------------------------------
# Start and stop scheduler
# ---------------------------------------------------------------------------

def start_scheduler():
    """Register all jobs and start the scheduler."""
    scheduler = get_scheduler()

    if scheduler.running:
        logger.info("Scheduler already running.")
        return

    # Approval reminder/escalation check — every hour
    scheduler.add_job(
        job_check_pending_approvals,
        trigger=IntervalTrigger(hours=1),
        id="check_pending_approvals",
        name="Check Pending Approvals (48hr reminder / 96hr escalation)",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Daily IIF generation — 06:00 UTC
    scheduler.add_job(
        job_daily_iif_generation,
        trigger=CronTrigger(hour=6, minute=0),
        id="daily_iif_generation",
        name="Daily IIF File Generation",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Daily Positive Pay — 06:30 UTC (after IIF)
    scheduler.add_job(
        job_daily_positive_pay,
        trigger=CronTrigger(hour=6, minute=30),
        id="daily_positive_pay",
        name="Daily Positive Pay File Generation",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Month-end reimbursement — 1st of month, 07:00 UTC
    scheduler.add_job(
        job_month_end_reimbursement,
        trigger=CronTrigger(day=1, hour=7, minute=0),
        id="month_end_reimbursement",
        name="Month-End Intercompany Reimbursement",
        replace_existing=True,
        misfire_grace_time=1800,
    )

    # DocuSign compliance envelope check — every 6 hours
    scheduler.add_job(
        job_compliance_reminder_check,
        trigger=IntervalTrigger(hours=6),
        id="compliance_reminder_check",
        name="DocuSign Compliance Envelope Status Check",
        replace_existing=True,
        misfire_grace_time=600,
    )

    scheduler.start()
    logger.info(
        f"Scheduler started with {len(scheduler.get_jobs())} jobs: "
        + ", ".join(j.name for j in scheduler.get_jobs())
    )


def stop_scheduler():
    """Gracefully stop the scheduler."""
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")
