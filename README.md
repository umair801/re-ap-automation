# RE-AP Automation System

**Enterprise Accounts Payable Automation for Real Estate Development**

Built by [Datawebify](https://datawebify.com) | Live: [ap-re.datawebify.com](https://ap-re.datawebify.com)

---

## What This System Does

A fully automated AP workflow for real estate development companies operating across multiple project LLCs. The system handles vendor invoice intake, four-document compliance verification, DocuSign envelope orchestration, CSI cost coding, tiered approval routing, QuickBooks Desktop IIF batch export, and QuickBooks Online credit card charge processing with intercompany reimbursement tracking.

---

## Architecture

```
Email Intake (invoices@company.com)
        │
        ▼
┌─────────────────────────────────────────┐
│         Path Classification              │
│   AP Invoice (Path 1) │ CC Receipt (Path 2)│
└─────────────────────────────────────────┘
        │                        │
        ▼                        ▼
Compliance Gate           Project Tagging
(4 documents)             (overhead vs billable)
        │                        │
        ▼                        ▼
DocuSign Envelopes        QBO API Push
(W-9, Master Policy,      (Purchase + BillableStatus
 COI, Indemnity)           + CustomerRef)
        │                        │
        ▼                        ▼
CSI Cost Coding           Month-End Reimbursement
(3-signal lookup)         (IIF for project LLC)
        │
        ▼
Tiered Approval
(Slack + email)
        │
        ▼
QB Desktop IIF Export
(per LLC entity)
```

---

## Two Processing Paths

### Path 1: AP Invoices (Check Payments)
- Full four-document compliance gate (W-9, Master Insurance Policy, COI, Indemnity Agreement)
- Vendor name cross-document normalization and mismatch detection
- DocuSign envelope orchestration with 3/7/14-day reminders
- CSI MasterFormat cost coding with three-signal confidence scoring
- Tiered approval routing via Slack with 48hr reminders and 96hr escalation
- QB Desktop IIF batch file generation per LLC entity

### Path 2: CC Charge Receipts (QBO Push)
- No compliance gate required
- Overhead vs project tagging decision engine
- Ambiguous charges parked for operator review — never auto-tagged
- QBO API push as Purchase with BillableStatus and CustomerRef
- Month-end intercompany reimbursement invoice generation
- IIF file for project LLC QB Desktop to record reimbursement

---

## Five Integration Gaps Completed

| Gap | Component | Description |
|-----|-----------|-------------|
| 1 | DocuSign SDK | JWT Grant auth, envelope sending, HMAC webhook verification |
| 2 | QB Desktop IIF Generator | Daily batch files per LLC, CS code in MEMO, project as CLASS |
| 3 | Compliance Agent | Four-document state machine, name normalization, COI verification |
| 4 | Cost Coding Agent | Three-signal CS code lookup with confidence levels |
| 5 | CC Charge Agent | Project tagging, QBO push, intercompany reimbursement |

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| API Server | FastAPI + Uvicorn |
| AI Extraction | Anthropic Claude API (claude-sonnet-4-20250514) |
| Compliance Envelopes | DocuSign eSignature SDK (JWT Grant) |
| Operator UI | Airtable (pyairtable) |
| Document Storage | Dropbox |
| Notifications | Slack (approval buttons) + Email |
| QB Online | QuickBooks Online API (OAuth2) |
| QB Desktop | IIF batch import files |
| Database | Airtable (7 tables) + Supabase (audit log) |
| Deployment | Railway (webhook server) + Ubuntu mini-PC (cron runner) |
| Tunnel | Cloudflare Tunnel |

---

## Corporate Entity Structure

```
Holding Entity
    │
    ├── Operating Entity (QBO) ← holds all credit cards
    │       └── Generates intercompany invoices to Project LLCs
    │
    ├── Project LLC 1 (QB Desktop) ← pays own vendors by check
    ├── Project LLC 2 (QB Desktop)
    └── Project LLC N (QB Desktop)
```

---

## Compliance Rule

**No bill proceeds to payment without all four documents on file. No exceptions.**

1. W-9 (vendor-level)
2. Master Insurance Policy — full policy, not just COI (vendor-level)
3. Certificate of Insurance — names specific project LLC as Additional Insured (project-level)
4. Indemnity Agreement — generated from project-specific template (project-level)

---

## Approval Tiers

| Tier | Approver | Limit |
|------|----------|-------|
| A | AP Manager | Up to $25,000 |
| B | Finance Director | Up to $50,000 |
| C | Principal | Up to $75,000 |
| D | Principal + Escalation | Above $75,000 |

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/webhooks/docusign` | DocuSign Connect envelope events |
| GET | `/webhooks/docusign/health` | DocuSign auth health check |
| GET | `/exports/iif/{entity_name}` | Daily IIF generation trigger |
| GET | `/exports/iif/{entity_name}/validate` | Validate generated IIF file |
| GET | `/docs` | Interactive API documentation |

---

## Environment Variables Required

```env
# DocuSign
DOCUSIGN_INTEGRATION_KEY=
DOCUSIGN_USER_ID=
DOCUSIGN_ACCOUNT_ID=
DOCUSIGN_PRIVATE_KEY_PATH=keys/docusign_private.key
DOCUSIGN_SANDBOX=true
DOCUSIGN_CONNECT_SECRET=

# Airtable
AIRTABLE_API_KEY=
AIRTABLE_BASE_ID=

# Slack
SLACK_BOT_TOKEN=
SLACK_APPROVAL_CHANNEL=

# QuickBooks Online
QUICKBOOKS_CLIENT_ID=
QUICKBOOKS_CLIENT_SECRET=
QUICKBOOKS_REFRESH_TOKEN=
QUICKBOOKS_REALM_ID=
```

---

## Deployment

- **Webhook server:** Railway (`ap-re.datawebify.com`)
- **Cron runner:** Ubuntu mini-PC (client office)
- **Tunnel:** Cloudflare Tunnel (DocuSign Connect → local runner)

---

*Built by [Datawebify](https://datawebify.com) — Enterprise Agentic AI Systems*
