# Support Escalation Runbook

**Document owner:** Support Engineering
**Last reviewed:** 2026-02-18
**Audience:** Internal support and on-call engineers

## Purpose

This runbook describes how incoming customer issues are triaged, who owns them, and how they escalate.
It is the operational companion to the Enterprise Service Level Agreement; where the SLA states what we
promise the customer, this runbook states how we deliver it.

## Severity triage

The first responder assigns a severity within 10 minutes of ticket creation.

- **Severity 1** — the customer's production is down or actively losing data. Target first response is
  15 minutes, around the clock. S1 always pages; it is never left in the queue.
- **Severity 2** — a major capability is broken with no workaround. First response within 1 hour.
- **Severity 3** — degraded behaviour with a documented workaround. First response within 4 business hours.
- **Severity 4** — questions, cosmetic issues, feature requests. First response within 1 business day.

If the responder and the customer disagree on severity, the higher severity applies until an engineering
lead reviews it. Never downgrade a customer-reported S1 without a lead's sign-off.

## On-call rotation

The on-call rotation runs weekly, handing over on Mondays at 10:00 in the primary support region. Each
rotation has a **primary** and a **secondary** engineer. The primary carries the pager; the secondary is
the first escalation target and must be reachable within 30 minutes.

## Escalation path

1. **Primary on-call** is paged automatically when an S1 is opened.
2. If the primary does not acknowledge within **15 minutes**, the page automatically escalates to the
   **secondary on-call**.
3. If the secondary does not acknowledge within a further **15 minutes**, the page escalates to the
   **engineering manager on duty**.
4. If the incident is still unresolved 60 minutes after it was opened, the **VP of Engineering** is
   notified and an incident commander is appointed.

Acknowledging a page means taking ownership, not merely silencing it. An engineer who cannot take
ownership must explicitly reassign before the escalation timer expires.

## Communication during an incident

For any S1, the incident commander posts an update to the customer's Slack Connect channel and to the
public status page every 30 minutes, even when the update is "no change yet". Silence is treated as a
process failure in the post-incident review.

## Post-incident review

Every S1 and every S2 that breaches its resolution target requires a written post-incident review within
5 business days. Reviews are blameless and focus on contributing conditions, not individuals. Action
items must have a named owner and a due date.

## Paging etiquette

The emergency paging line is reserved for Severity 1 only. If a customer pages for a lower severity, the
responder handles the issue, then reminds the customer of the correct channel. Repeated misuse is
escalated to the account manager rather than handled by on-call.
