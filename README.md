# bunni backend 

Text a number, and it adds events to Google Calendar! 

Follow the instructions below to recreate/contribute to this project 

---

## what you need accounts for

before making changes to the code, create accounts on these four services. all have free tiers.

- **Railway** — railway.app (hosts your server + redis)
- **Supabase** — supabase.com (postgres database)
- **Twilio** — twilio.com (phone number + SMS)
- **Google Cloud** — console.cloud.google.com (calendar API access)
- **Anthropic** — console.anthropic.com (Claude API)

---

## step 1: google cloud setup

this is the most involved step. takes about 10 minutes.

1. go to console.cloud.google.com and create a new project, name it anything
2. in the left sidebar go to **APIs and Services > Library**
3. search "Google Calendar API" and click **Enable**
4. search "Google OAuth2 API" and click **Enable**
5. go to **APIs and Services > OAuth consent screen**
   - choose **External**
   - fill in app name (e.g. "gcal agent"), your email for support and developer contact
   - click Save and skip through the rest of the screens
6. go to **APIs and Services > Credentials > Create Credentials > OAuth 2.0 Client ID**
   - application type: **Web application**
   - under "Authorized redirect URIs" add: `https://YOUR_RAILWAY_URL/oauth/callback`
   - you don't have your railway URL yet so put a placeholder and come back to update it after step 4
7. click Create and copy the **Client ID** and **Client Secret** somewhere

---

## step 2: supabase setup

1. create a new project at supabase.com
2. go to **Settings > Database > Connection string > URI**
3. copy it. looks like: `postgresql://postgres:PASSWORD@db.xxx.supabase.co:5432/postgres`
4. change `postgresql://` to `postgresql+asyncpg://`
5. save this as your `DATABASE_URL`

tables are created automatically on first boot. no SQL needed.

---

## step 3: twilio setup

1. sign up at twilio.com
2. from the console copy your **Account SID** and **Auth Token**
3. go to **Phone Numbers > Manage > Buy a number**, get a US number (~$1.15/mo)
4. copy the number in E.164 format like `+18005550142`
5. you'll set the webhook after deploying in step 4

---

## step 4: deploy to railway

1. install Railway CLI:
   ```
   npm install -g @railway/cli
   ```

2. in this project folder:
   ```
   railway login
   railway init
   railway up
   ```

3. add Redis: Railway dashboard > your project > **New > Database > Add Redis**
   copy the `REDIS_URL` from its Variables tab

4. in Railway dashboard go to your service > **Variables** and add all of these:

   ```
   ANTHROPIC_API_KEY      sk-ant-...
   DATABASE_URL           postgresql+asyncpg://postgres:PASSWORD@db.xxx.supabase.co:5432/postgres
   REDIS_URL              redis://default:PASSWORD@redis.railway.internal:6379
   GOOGLE_CLIENT_ID       xxxxxxxx.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET   GOCSPX-xxxxxxxx
   GOOGLE_REDIRECT_URI    https://YOUR_RAILWAY_URL/oauth/callback
   STATE_SECRET           (run: python3 -c "import secrets; print(secrets.token_hex(32))")
   TWILIO_ACCOUNT_SID     ACxxxxxxxx
   TWILIO_AUTH_TOKEN      xxxxxxxx
   TWILIO_PHONE_NUMBER    +18005550142
   ```

5. your Railway URL is in the dashboard under your service, looks like:
   `https://gcal-agent-production.up.railway.app`

6. go back to Google Cloud and update the redirect URI to your real URL:
   `https://gcal-agent-production.up.railway.app/oauth/callback`
   update `GOOGLE_REDIRECT_URI` in Railway to match

---

## step 5: wire up twilio

1. twilio.com > **Phone Numbers > Manage > Active Numbers** > click your number
2. under Messaging Configuration set "A message comes in" to **Webhook**:
   `https://YOUR_RAILWAY_URL/message`
3. method: **HTTP POST**
4. save

---

## step 6: test it

text your Twilio number anything. you should get:

```
hey to get started connect your google calendar real quick: https://...

also save this number as "gcal" so you can find it later
```

tap the link, sign in with Google, approve access. then text:

```
meeting with jake friday at 3pm
```

it should show up in your Google Calendar.

---

## file structure

```
gcal-agent/
├── main.py                    fastapi app, starts everything
├── requirements.txt           python dependencies
├── routers/
│   ├── sms.py                 handles incoming texts + onboarding
│   └── oauth.py               google oauth callback, stores tokens
├── services/
│   ├── agent.py               claude tool routing + reply generation
│   ├── calendar.py            google calendar api operations
│   ├── google_oauth.py        oauth flow + per-user calendar clients
│   ├── rate_limit.py          redis rate limiter (30 msg/user/hr)
│   └── sms.py                 outbound twilio sms helper
└── db/
    ├── models.py              users table schema
    ├── session.py             database connection pool
    └── repo.py                all database queries
```

---

## running locally

1. copy `.env.example` to `.env` and fill in values
2. `pip install -r requirements.txt`
3. install ngrok at ngrok.com
4. terminal 1: `uvicorn main:app --reload`
5. terminal 2: `ngrok http 8000`
6. set your Twilio webhook to the ngrok HTTPS URL + `/message`

---

## monthly cost estimates

| users | msgs/day | monthly |
|-------|----------|---------|
| 10    | 3        | ~$19    |
| 50    | 3        | ~$45    |
| 100   | 3        | ~$75    |

prompt caching is enabled so Claude costs are ~80% cheaper than without it.
