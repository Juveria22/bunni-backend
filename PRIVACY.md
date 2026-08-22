# Privacy Policy

**Last updated: August 19, 2026**

## Overview

bunni ("we", "us", "our") operates an AI-powered calendar assistant that you use by text message. This policy explains how we collect, use, and protect your information.

## Information We Collect

- **Phone number** — used to identify you and send replies
- **Messaging channel** — whether you reach us over SMS or WhatsApp, so replies and reminders go back the same way
- **Google account email** — collected when you connect your Google Calendar
- **Google Calendar access** — we read and write to your calendar on your behalf via OAuth2
- **Message history** — the text you send us and our replies, so the assistant can understand follow-up messages

## How We Use Your Information

- To create, update, and manage your Google Calendar events
- To invite people to your events, when you ask us to and give us their email address
- To send you confirmations and reminders about upcoming events
- To identify you across messages, so a follow-up like "make it 3pm instead" works

## Data Storage

Your phone number and Google account email are stored in our database. Your Google OAuth refresh token — the credential that lets us act on your calendar — is encrypted before it is written, using a key held separately from the database.

We also store the messages you exchange with us — your texts and our replies — in that same database. This is what lets the assistant understand follow-ups or answer a question it asked you. Only messages from the last hour are ever sent back to the AI model, and stored messages are automatically deleted after 7 days.

**Calendar content.** We do not keep a copy of your calendar. Your events are read from Google when we need them and are not retained afterwards. Two narrow exceptions:

- When we ask you to confirm something ("delete dentist?", "you already have standup then, still add lunch?"), the event's name and time are held for up to 30 minutes so we can carry out exactly what you agreed to. If you are inviting someone, the address you gave us is held the same way. It is deleted once you answer, or when it expires.
- Our replies to you often mention an event by name, and those replies are stored in your message history under the 7-day rule above. A guest's email address appears there too when you invite them, because we read the address back to you before sending anything.

## Data Sharing

We do not sell, trade, or share your personal information with third parties except:

- **Anthropic** — Claude AI processes the text you send us in order to understand your request. It also receives the names, times, and locations of the calendar events relevant to that request, and the name and location of an event we are about to remind you about. If you ask about an event's notes or who is invited to it, that one event's description and guest email addresses are sent as well — only for the event you asked about, and only on the request that asked. Anthropic does not receive your phone number or your Google account email.
- **Google** — your calendar is read and written via the Google Calendar API. When you invite someone to an event, Google sends them an invitation email on your behalf, which shows them the event and that you invited them.
- **Twilio** — your phone number is used to send and receive messages.

We do not share your mobile number with third parties for marketing purposes.

## Message Frequency

Message frequency varies based on your usage.

You will normally receive a reply to each message you send. If you send an unusual volume of messages in a short period, we may stop replying until the next hour to keep the service running for everyone.

**Separately, and automatically, we text you about upcoming events** — about an hour before a timed event starts, and again as it begins. These are not something you switch on individually; they apply to timed events on the calendar you connected. All-day events do not trigger them. Reply STOP at any time to end all messages.

## Message and Data Rates

Message and data rates may apply. Contact your carrier for details.

## Opt Out

Reply **STOP** at any time. Reply **HELP** for help.

Your carrier or messaging provider may send you a confirmation that you have opted out; we do not send one ourselves, because your number is blocked from receiving further messages at that point.

## Data Retention

Your account details (phone number, Google email, and encrypted access token) are retained for as long as you have an active account. Your message history is deleted automatically after 7 days.

Replying **STOP** revokes our access to your Google Calendar with Google, then deletes your account, your stored access token, your message history, and any pending confirmations. This is immediate and cannot be undone; texting us again afterwards starts a new account from scratch. You can also contact us at the email below.

You can revoke our access to your calendar at any time directly from your Google account, at [myaccount.google.com/permissions](https://myaccount.google.com/permissions), independently of anything you do here.

Note that operational logs — which record that a message was received, its length, and a partially masked phone number, but not the message contents — are retained separately by our hosting provider and are not covered by the deletion above.

## Security

Your Google access token is encrypted at rest. Messages from Twilio are cryptographically verified, so nobody can act on your calendar by pretending to be your number. Setup links are single-use and expire after 15 minutes.

However, no method of transmission over the internet is 100% secure, and we cannot guarantee absolute security.

## Contact

For questions about this privacy policy, contact us at: aminjuveria00@gmail.com
