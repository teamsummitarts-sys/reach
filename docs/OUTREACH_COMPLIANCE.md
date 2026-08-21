# REACH — Outreach Compliance

**This engine does not replace legal advice.** It encodes a conservative,
auditable process. Anything it cannot place cleanly becomes `LEGAL_REVIEW` or a
human task rather than a send.

Implementation: `reach/compliance.py`, `reach/contacts.py`, `reach/sender.py`,
`reach/approvals.py`.

## The decision, before anything can be queued

Every prospective message produces a persisted `ComplianceDecisionRecord`
naming the recipient country, sender country, contact category, outreach
category, source of the address, permission signal, lawful basis, whether
suppression / provider policy / sender health were checked, every rule that
fired, the policy version and the timestamp.

Decisions: `ALLOW` · `APPROVAL_REQUIRED` · `DRAFT_ONLY` · `LEGAL_REVIEW` · `BLOCK`.

The engine returns **the most restrictive outcome any rule produced**. Rules
never cancel each other out. In Copilot the floor is `APPROVAL_REQUIRED` — REACH
never sends without a human approval.

## Contact categories, and which may be emailed

| Category | Sendable | Meaning |
| --- | :-: | --- |
| `EXPLICIT_SUBMISSION_ROUTE` | ✅ | Published on a page whose purpose is music submissions |
| `VERIFIED_PUBLIC_BUSINESS` | ✅ | Business address corroborated by independent sources |
| `ROLE_BASED_ADDRESS` | ✅ | `submissions@`, `music@`, `programming@`, `editorial@`, `promos@`… |
| `UNVERIFIED_PUBLIC_BUSINESS` | ❌ | Seen once, not corroborated |
| `PROFESSIONAL_SOCIAL_ROUTE` | ❌ | Not a Phase One send channel |
| `PERSONAL_ADDRESS` | ❌ | Free-mail domain with a personal mailbox |
| `SPOTIFY_ONLY_SOURCE` | ❌ | Found only through Spotify |
| `DATA_BROKER_SOURCE` | ❌ | Unlicensed list |
| `PROHIBITED_PRIVATE_INFORMATION` | ❌ | Hidden-markup honeypot or private data |
| `UNKNOWN` | ❌ | Could not be established |

Rules enforced in `contacts.py`:

* Explicitly published submission routes are preferred over anything else.
* Role addresses are preferred over named individuals.
* Where the address was published is recorded as an evidence packet, always.
* Purchased or scraped lists are never imported.
* A personal address is never used because a search result displayed it.
* An address is never inferred from a person's name plus a company domain —
  `contacts.never_guess` exists so the prohibition is testable, not just documented.
* A failed address is never silently replaced with a guess.
* Domain checks are non-intrusive DNS only; no SMTP callbacks.
* Person, organization and contact method are separate records.
* Values are encrypted at rest and hashed for dedup and suppression.
* One curator controlling several playlists produces **one** consolidated target.

## Outreach categories

`REQUESTED_SUBMISSION` · `PUBLISHED_SUBMISSION_ADDRESS` · `EXISTING_RELATIONSHIP` ·
`RELATIONSHIP_REPLY` · `COLD_PROFESSIONAL_OUTREACH` · `PAID_CONSIDERATION` ·
`SPONSORSHIP_OR_ADVERTISING` · `SOCIAL_DM_DRAFT` · `UNKNOWN`

## Territory rule sets

Selected by recipient country, inferred from the outlet's territory or its ccTLD.
**When the country is UNKNOWN, the most restrictive path applies** — the decision
is never relaxed by missing information.

| Rule set | Lawful basis recorded | Additional controls |
| --- | --- | --- |
| **US** — CAN-SPAM | Commercial email permitted with truthful headers, clear business identity, a valid postal address and a working opt-out | Postal address is required before a footer can be built |
| **EU/EEA** — GDPR + ePrivacy | Art. 6(1)(f) legitimate interest for B2B outreach to a published professional role address | First contact identifies the sender, states where the address was found, and honours objection immediately |
| **UK** — UK GDPR + PECR | Corporate-subscriber outreach to a published professional address | Immediate opt-out |
| **CA** — CASL | Implied consent from a business address conspicuously published without a stated restriction, for a message relevant to the recipient's role | Implied consent expires; re-contact limits enforced |
| **Other** | Professional outreach to an address published for exactly this purpose | Immediate opt-out |

California privacy rights are supported through the same access, correction,
deletion and suppression workflows (`evidence.delete_provider_data`,
`contacts.suppress`, the audit trail).

## Hard blocks

Any one of these produces `BLOCK`, regardless of every other signal:

* the emergency send stop is engaged;
* the address was found only through Spotify;
* the address is suppressed;
* the contact is a personal address, a honeypot, or from a data broker;
* the risk band is `BLOCK` (≥75);
* the offer is guaranteed placement or guaranteed streams.

## Paid opportunities

Cost models are distinguished: `FREE_SUBMISSION`, `PAID_CONSIDERATION`,
`ADVERTISING_OR_SPONSORSHIP`, `SERVICE_FEE`, `GUARANTEED_PLACEMENT`,
`GUARANTEED_STREAMS`, `UNKNOWN`.

* Paid consideration does not equal placement, and the decision record says so
  in as many words.
* Guaranteed streams and guaranteed placement are **blocked**.
* Undisclosed paid airplay is blocked; sponsorship and advertising route to
  `LEGAL_REVIEW`.
* No money is spent automatically. Approving a paid action additionally requires
  the `spend.approve` permission.
* Total cost, currency and the evidence for them are shown before approval.

## Sender-health gate

No direct outreach is enabled until every required check passes:
sending domain · SPF · DKIM · DMARC · TLS · bounce webhook · complaint handling ·
unsubscribe · postal address · email provider.

Also reported: dedicated outreach subdomain, DMARC alignment, forward DNS,
reverse DNS (N/A for API sending), one-click unsubscribe, suppression
integration, domain reputation (UNKNOWN — that belongs to the provider's
postmaster tools, not to REACH).

**A check that cannot be established reports UNKNOWN and fails the gate.** There
is no state in which REACH renders green for something it did not verify. When
the gate fails the UI shows `OUTREACH DISABLED — DRAFTS ONLY`.

Additional send-time controls: conservative warm-up schedule
(10/20/40/80/150/300/500 per day), per-domain throttling, 30-day per-recipient
cooldown, per-campaign and per-tenant daily limits, automatic pause above a 5%
bounce rate, global emergency stop, permanent-bounce suppression, retry only for
temporary failures. **No open-tracking pixel by default.** Large attachments are
avoided in favour of a single link.

## Message content

Every message carries a truthful sender identity, a truthful subject, a clear
business identity, the physical postal address, a visible opt-out, and
`List-Unsubscribe` / `List-Unsubscribe-Post` headers. On first contact it states
where the address was found.

`compliance.unsubscribe_footer` **raises** rather than emitting a message without
a postal address — a fabricated address would be worse than no send at all.

The generator will not: pretend to know the recipient, claim the artist follows
them, claim to have listened to something that was not analyzed, fabricate prior
placements or audience statistics, manufacture praise, use false urgency or a
deceptive reply prefix, or mention scraped personal details. Every factual
sentence records its source basis in `facts_json`, shown on the approval screen.

Local-language drafts are produced only where translation confidence is high;
otherwise REACH drafts in English and flags the target for human translation.

## Objection handling

An opt-out is honoured immediately and permanently: the address is globally
suppressed, the target moves to `OPTED_OUT`, pending follow-ups are cancelled,
and re-contact is blocked at the relationship level. A reply containing
"unsubscribe", "opt out", "remove me" or "do not contact" is treated as an
opt-out even when recorded as an ordinary reply. Follow-ups are never sent after
a decline, an opt-out or a bounce.

**Contact data is never sold, licensed or shared in Phase One.**
`reach.contact_graph_export` is a feature flag set to `False`. If that business
model is ever pursued, it requires a separate privacy and data-broker legal
review before the flag is touched.
