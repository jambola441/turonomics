---
name: gcp
description: Working with Google Cloud from a turonomics session — installing and authenticating gcloud in the ephemeral container, and knowing which parts of GCP are automatable at all. Use this whenever the task touches Google Cloud, gcloud, a GCP project, the Gmail API, OAuth client IDs or consent screens, Google API credentials, or Turo email ingestion — including when the user offers to hand over credentials, because for some tasks credentials do not help and taking them is pure downside.
---

# Google Cloud from a turonomics session

The expensive mistakes here are not syntax. They are confidently sending the
operator down a path that cannot work, or accepting credentials for a task
where credentials change nothing. Both have happened on this project. Read the
"console-only" section before promising anything.

## gcloud is not installed, and the container is ephemeral

Every session starts without it. Install takes about a minute:

```bash
cd /tmp/claude-0 \
  && curl -sS -o gcloud.tar.gz \
       https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz \
  && tar -xzf gcloud.tar.gz \
  && ./google-cloud-sdk/install.sh --quiet --usage-reporting=false \
       --path-update=false --command-completion=false
export PATH="/tmp/claude-0/google-cloud-sdk/bin:$PATH"
```

Install it under the scratchpad, not the repo — it is ~88MB compressed and
several hundred unpacked, and it must never be committed.

Network works without extra configuration: `CLOUDSDK_PROXY_*` and
`CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE` are already set for the session's egress
proxy. Do not override them, and never disable TLS verification to make a call
succeed.

To stop paying the install cost every session, the operator can add that
command to the environment's **Setup script** (cloud environment menu in the
title bar → Edit). Worth suggesting if GCP work becomes routine; not worth it
for a one-off.

## The auth trap that wastes an hour

The session ships with `CLOUDSDK_AUTH_ACCESS_TOKEN` set to a 14-character
placeholder. gcloud prefers it over every credential you configure, so any
authenticated call fails like this no matter what you set up:

```
ERROR: UNAUTHENTICATED: Request had invalid authentication credentials.
This command is authenticated with an access token from the
CLOUDSDK_AUTH_ACCESS_TOKEN environment variable.
```

That last sentence is the only clue, and it is easy to read past. Unset it:

```bash
env -u CLOUDSDK_AUTH_ACCESS_TOKEN gcloud projects list
```

With it unset you get the honest error instead — "You do not currently have an
active account selected" — which tells you what is actually wrong.

## Getting authenticated

Two paths work headlessly. Neither involves the operator pasting a secret into
the chat; say so plainly if they offer, and point them at the environment's
settings instead.

**A service account key**, for anything that should keep working unattended.
The operator creates the account, grants it only the roles the task needs, and
stores the JSON in the environment's API credentials or as an env var they
name. Then:

```bash
gcloud auth activate-service-account --key-file="$GCP_SA_KEY_FILE"
```

**`gcloud auth login --no-browser`**, for a one-off while they are present. It
prints a command for them to run on their own machine and a code to paste back.
Note the flag is `--no-browser`; older guidance says `--no-launch-browser`,
which no longer exists. This leaves no long-lived key lying around, which makes
it the better choice when someone is at the keyboard.

Prefer the narrowest thing that works. A key with project-owner is a liability
living in an environment variable.

## What GCP will and will not let you automate

Most of GCP is API-driven and fine to do from here:

| Task | How |
|---|---|
| Create a project | `gcloud projects create` |
| Enable an API | `gcloud services enable gmail.googleapis.com` |
| List/describe anything | `gcloud <service> list` |
| Service accounts, IAM, keys | `gcloud iam ...` |

**Creating an OAuth 2.0 client ID is console-only.** There is no gcloud command
and no public API. The one programmatic path that existed — the IAP OAuth Admin
API — was [shut down on 19 March 2026](https://cloud.google.com/iap/docs/programmatic-oauth-clients).
Google's docs now say to use the console at APIs & Services → Credentials.
Terraform exists but drives the same console-only surface with no advantage.

**Configuring the OAuth consent screen is likewise console-only**, including
the Publish step.

This matters more than it looks: those two are exactly the steps an operator
most wants handed off, and they are the two that cannot be. When the task is
"set up OAuth", credentials buy you the project and the API enablement — one
click each — and leave the actual blocker untouched. Say that up front rather
than accepting access and discovering it later. Taking broad cloud access for
no gain is a cost, not a courtesy.

## This project's GCP surface

Only one thing: **reading Turo's notification emails via the Gmail API.** Turo
has had no public API since April 2023, and the browser extension only runs
while a desktop browser is open, which the operator's phone on a sidewalk is
not. So the mail is the trip feed.

**Two hosts, and confusing them wastes a round trip.** The API answers on
`turonomics.onrender.com`; the fleet UI is a separate static site on
`turonomics-site.onrender.com/fleet/`. The redirect URI must point at the
**API**, because that is where the client secret and the token store live — but
the API root is a 404, so the callback sends the operator on to the UI
afterwards (`UI_URL`, defaulting to the fleet page). Landing them on the API
host would show an error page after a successful connect.

What is already built (`api/src/turonomics_api/gmail/`, `routers/gmail.py`):

- `GET /api/gmail/connect` → Google's consent screen
- `GET /api/gmail/callback` → stores the grant in `oauth_token`, provider `gmail`
- `GET /api/gmail/status` → whether the grant is still good

The operator clicks Allow once and never handles a token. Do not reintroduce a
pasted `GMAIL_REFRESH_TOKEN`; the handshake exists so that config does not.

Required setup, and the parts that bite:

- **Enable the Gmail API in the project** (`gcloud services enable
  gmail.googleapis.com`, or the console). Easy to skip, because the whole OAuth
  flow succeeds without it and fails only at the last step — reading the
  profile to check the address — with a 403 naming a project *number* rather
  than saying the API is off:

  ```
  Gmail API has not been used in project 608263863877 before or it is disabled.
  ```

  Nothing is left half-connected when this happens: the grant is stored and the
  address checked in that order, and the store is only committed once the check
  passes, so the flush is discarded on session close. Enable the API, then
  connect again.
- OAuth client type must be **Web application**. A Desktop client only permits
  loopback redirects, so it cannot serve the deployed callback.
- Redirect URI must be registered exactly:
  `https://turonomics.onrender.com/api/gmail/callback`
- Scope `https://www.googleapis.com/auth/gmail.readonly`, which Google
  classifies as *restricted*.
- **Publish the consent screen.** While it is in Testing, Google expires
  refresh tokens after 7 days and mail silently stops arriving — a failure that
  looks exactly like an empty inbox. Unverified-but-published shows a "Google
  hasn't verified this app" warning the operator clicks through; that is fine
  for their own mailbox.
- Env vars: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GMAIL_ADDRESS`.
  `GMAIL_ADDRESS` is not cosmetic — the callback is reachable by anyone until
  D8's sign-in exists, and that allowlist is what stops a stranger attaching
  their own mailbox over the operator's.

Two Google behaviours the client code already handles; do not "simplify" them
away:

- Consent must request `access_type=offline` **and** `prompt=consent`. Missing
  either returns an access token with no refresh token, so the connection works
  for one hour and then fails with an error about refreshing that has nothing to
  do with the cause.
- An ordinary refresh response omits `refresh_token`, because Google's do not
  rotate — unlike Bouncie's in the same codebase, which do. Writing that
  omission through nulls the stored token and disconnects the mailbox an hour
  after connecting.

A service account with domain-wide delegation is the usual way a server reads a
mailbox, and it is **not available here**: the operator is on a personal
`@gmail.com` with no Workspace organisation. Do not propose it.

## Checking the live connection

```bash
curl -s https://turonomics.onrender.com/api/gmail/status
```

`{"connected": false, ...}` with a `detail` mentioning reconnect means the grant
was revoked or expired. The only fix is a human visiting `/api/gmail/connect`
again — there is no never-expiring authorization code to self-heal from, unlike
the Bouncie integration. Worth saying explicitly rather than retrying.
