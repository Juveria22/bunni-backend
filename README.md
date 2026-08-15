# bunni backend

Text a number in plain English, and your Google Calendar does what you meant.

> Note: `PRIVACY.md` and `TERMS.md` refer to this service as "gcal agent". Pick one
> name and make it consistent — the legal docs are the user-facing ones.

Managing a calendar from a phone is annoying. The point of this service is that
you send something short and sloppy — "dentist friday at 3", "move saniyahs
party to noon", "office day*" — and it is handled, without being asked to
repeat yourself or spell anything exactly.

---

## what it does

**Manages events by text.** Create, reschedule, delete, and change reminders,
in natural language. The agent reads the actual calendar before acting, so it
resolves things like "the saturday one" or "the 11-6 one" against what is really
there rather than guessing.

**Texts you before things happen.** Every timed event gets a text about an hour
ahead and again as it starts. This is the part Google doesn't do well — its own
reminders are popups and emails, which is exactly what people miss. The wording
is generated per event, so a flight reads more urgently than a coffee.

**Asks before anything irreversible.** Deleting always asks first, and names the
event back. Creating or moving something onto an occupied slot asks first and
names what it collides with. Both confirmations are resolved in code, not by the
model — what would be written is fixed and shown before the question is asked, so
answering "yes" can only ever do the thing you were looking at.

**Works over SMS or WhatsApp.** Same endpoint, same logic. The channel is
detected from the sender and remembered, so replies and reminders go back the
way you came in.

---

## how a message flows

```
Twilio ──► POST /message
             │
             ├─ verify Twilio signature ──────────── reject if unsigned
             ├─ STOP / HELP keywords ─────────────── handled and returned
             ├─ rate limit (30/user/hour) ────────── first refusal answers, rest silent
             ├─ not onboarded? ───────────────────── reply with a one-use OAuth link
             │
             └─ hand off, return empty 200 immediately
                   │
                   ▼
             was there a parked yes/no question?
                   │
          ┌────────┴────────┐
         yes               no
          │                 │
   replay the exact    Claude tool loop:
   action shown to     find_events ─► create / reschedule
   the user            delete / update_reminders
          │                 │
          └────────┬────────┘
                   ▼
              reply via Twilio REST
```

The reply is delivered out of band rather than on the webhook response. Holding
the webhook open meant the whole agent run had to finish inside Twilio's ~15
second timeout, which capped how much thinking it could do.

A background sweep runs every two minutes on one replica, finds events entering
the reminder windows, claims each one in the database so no two workers send the
same text, and sends it.

---

## the trust boundary

Anyone can put an event on someone's Google calendar by sending an invite, so
event titles and locations are attacker-controlled text that ends up in two
sensitive places: the agent's context, and the body of a text sent from a number
the user trusts.

Three things hold that line. Titles and locations are stripped of newlines and
control characters and capped in length before they go anywhere. Tool results
are labelled as data rather than instructions, and the system prompt says so.
Anything that reads like an instruction, a link, or a phone number forces a
confirmation before a write, and drops the reminder writer to a fixed template.

Delete-always-confirms is the only unconditional guarantee; the rest is defence
in depth.

---

## stack

| | |
|---|---|
| **Railway** | hosts the app and Redis |
| **Supabase** | Postgres — users, transcript, reminder claims, parked confirmations, OAuth nonces |
| **Redis** | rate limiting and the sweep lock |
| **Twilio** | inbound webhook and outbound SMS/WhatsApp |
| **Google Calendar API** | per-user OAuth, calendar reads and writes |
| **Anthropic** | Sonnet for the agent loop, Haiku for reminder wording and yes/no reading |

Schema is created on boot under a Postgres advisory lock, so replicas starting
together don't race. Environment variables are documented in `.env.example`.

---

## layout

```
main.py                     app, background loops, schema prep, request size limit
routers/
  sms.py                    inbound webhook, STOP/HELP, rate limit, reply dispatch
  oauth.py                  google callback, redeems the one-use link
services/
  agent.py                  claude tool loop, tool schemas, write handlers
  phrasing.py               turning calendar data into the words we text back
  calendar.py               google calendar operations and event-shape helpers
  google_oauth.py           granting access: state, consent flow, revoke
  google_client.py          using access: token cache, per-user calendar client
  reminders.py              the sweep, and how a reminder is worded
  sanitize.py               untrusted-text scrubbing, injection heuristics, masking
  sms.py                    outbound twilio, GSM-7 substitution, length clamp
  rate_limit.py             redis fixed-window limiter, in-process fallback
  redis_client.py           shared connection, distributed lock
db/
  models.py                 5 tables: users, messages, sent_reminders,
                            pending_confirmations, oauth_states
  session.py                async engine and pooling
  repo.py                   every query, plus refresh-token encryption
```

---

## what it costs to run

Per-unit, so you can multiply by your own traffic. Order of magnitude, at list
prices, assuming the prompt cache is warm.

| | |
|---|---|
| One message answered by the agent | **~$0.027** |
| — inbound SMS | $0.0075 |
| — Claude (two calls, cached prefix) | ~$0.012 |
| — outbound SMS, one segment | $0.0079 |
| One reminder sent | **~$0.008** |
| Twilio number | $1.15/mo |

**Messaging is the larger half, not the model.** One extra SMS segment costs
more than an entire extra Claude round trip, which is why replies are clamped to
two segments, clash lists name at most three events, and a rate-limited user is
answered once rather than every time. WhatsApp bills per 24-hour conversation
window instead of per segment, so it gets materially cheaper as volume rises.

Prompt caching cuts the Claude portion by roughly half. The often-quoted 90%
figure is the discount on cached tokens specifically, not on the bill.

Reminders are usually the largest line item at any real user count, because they
are the only outbound messages the user didn't trigger.
