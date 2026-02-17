# leNNyTP

**An NNTP server that bridges to [Lemmy](https://join-lemmy.org/), letting you read and post to Lemmy communities using any Usenet/newsreader client (like thunderbird, clawsmail or slrn).**

leNNyTP polls a Lemmy instance, mirrors communities, posts, and comments into a local spool on disk, and serves them over the NNTP protocol. Your newsreader never talks to Lemmy directly — it talks to leNNyTP, which feels like a classic Usenet server. Posting and authentication are the only operations that touch the Lemmy API in real time.

```
┌──────────────┐         NNTP          ┌──────────────┐      HTTP/API      ┌──────────────┐
│  Newsreader  │ ◄───────────────────► │   leNNyTP    │ ◄────────────────► │    Lemmy     │
│  (Thunderbird│       read/post       │              │   poll / post      │   instance   │
│   slrn, etc) │                       │  local spool │                    │              │
└──────────────┘                       └──────────────┘                    └──────────────┘
```

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [Command-Line Options](#command-line-options)
- [Authentication](#authentication)
- [SSL / NNTPS](#ssl--nntps)
- [Newsgroup Mapping](#newsgroup-mapping)
- [Reading Articles](#reading-articles)
- [Posting Articles](#posting-articles)
- [Admin & Moderation](#admin--moderation)
  - [Registration Applications](#registration-applications)
  - [Reports](#reports)
  - [Moderation Log](#moderation-log)
  - [Inline Moderation in Any Group](#inline-moderation-in-any-group)
- [Newsreader Configuration](#newsreader-configuration)
- [Examples](#examples)
- [Spool Directory Structure](#spool-directory-structure)
- [Limitations](#limitations)
- [License](#license)

---

## Features

- **Full NNTP protocol** — works with Thunderbird, slrn, tin, Pan, Gnus, and any RFC 3977-compliant newsreader.
- **Zero API calls on read** — all reading is served from the local disk spool. Fast and offline-friendly.
- **Background polling** — a poller thread syncs communities, posts, and comments at a configurable interval.
- **Posting support** — new posts and replies are sent to Lemmy in real time via the API.
- **NNTP authentication** — maps directly to Lemmy username/password login.
- **Admin/moderation via NNTP** — approve registrations, handle reports, delete posts, ban users, and lock threads, all from your newsreader.
- **SSL/TLS support** — optional NNTPS for encrypted connections.
- **No-auth mode** — optionally allow anonymous reading (posting always requires auth).
- **Single-file, zero dependencies** — pure Python 3 standard library. No pip install needed.

---

## Requirements

- Python 3.8 or later (uses `asyncio`, `ssl`, `pathlib`, `urllib`).
- A running Lemmy instance (any version with the v3 API).
- No third-party Python packages required.

---

## Quick Start

```bash
# Clone the repo
git clone https://github.com/youruser/lenntp.git
cd lenntp

# Run against a public instance (anonymous reading disabled by default)
python3 lemmy_nntp.py --instance https://lemmy.ml

# Run with anonymous reading enabled
python3 lemmy_nntp.py --instance https://lemmy.ml --no-auth

# Run with poller authentication (needed for Subscribed listing or admin features)
python3 lemmy_nntp.py --instance https://lemmy.ml \
    --username mybot --password secret123
```

Then point your newsreader at `localhost:1190`. That's it.

---

## How It Works

leNNyTP has two halves that run concurrently:

### 1. Background Poller

A background thread wakes up every `--interval` seconds (default: 900 = 15 minutes) and:

1. Fetches the list of communities from Lemmy (up to 1000, paginated).
2. For each community, fetches the 50 newest posts.
3. For each post, fetches up to 200 comments (threaded, up to 20 levels deep).
4. Converts everything into NNTP-style articles and stores them in the on-disk spool.
5. If the poller account is a Lemmy admin, also syncs registration applications, reports, and the moderation log into special admin newsgroups.

Articles that already exist in the spool are skipped. The poller only adds new content.

### 2. NNTP Server

An asyncio TCP server speaks the NNTP protocol (RFC 3977). When a newsreader connects and issues commands like `GROUP`, `ARTICLE`, `OVER`, etc., leNNyTP reads entirely from the disk spool. **No Lemmy API calls happen during reading.** This makes reads fast and resilient to Lemmy downtime.

The only commands that hit the Lemmy API are:

- `AUTHINFO` — logs in to Lemmy and gets a JWT.
- `POST` — creates a post or comment on Lemmy (or executes admin actions).

After a successful `POST`, leNNyTP triggers an immediate background sync so the new content appears in the spool quickly.

---

## Command-Line Options

| Option | Default | Description |
|---|---|---|
| `--instance URL` | *(required)* | Lemmy instance URL, e.g. `https://lemmy.ml`. |
| `--host ADDR` | `127.0.0.1` | Address to bind to. Use `0.0.0.0` to listen on all interfaces. |
| `--port PORT` | `1190` | TCP port for the NNTP server. Standard NNTP is 119, NNTPS is 563. |
| `--spool DIR` | `./spool` | Directory for the local article spool. Created automatically if it doesn't exist. |
| `--interval SECS` | `900` | How often the background poller syncs with Lemmy, in seconds. |
| `--listing-type TYPE` | `Local` | Which communities to fetch: `Local` (this instance only), `All` (federated), or `Subscribed` (requires poller auth). |
| `--username USER` | *(none)* | Lemmy username for the poller's authenticated session. Needed for `Subscribed` listing and admin features. |
| `--password PASS` | *(none)* | Lemmy password for the poller's authenticated session. |
| `--no-auth` | *(off)* | Allow reading articles without NNTP authentication. Posting still requires auth. |
| `--ssl-cert FILE` | *(none)* | Path to an SSL certificate in PEM format. Enables NNTPS. Must be used with `--ssl-key`. |
| `--ssl-key FILE` | *(none)* | Path to the SSL private key. Must be used with `--ssl-cert`. |
| `--debug` | *(off)* | Enable verbose debug logging (logs every NNTP command and API call). |

---

## Authentication

leNNyTP supports two levels of authentication:

### Poller Authentication (`--username` / `--password`)

This is the account the background poller uses to fetch content from Lemmy. You need this if:

- You want to use `--listing-type Subscribed` (requires a logged-in session).
- You want admin groups (registrations, reports, modlog) — the poller account must be a Lemmy site admin.

The poller logs in once at startup and reuses the JWT for all API calls.

### Client Authentication (NNTP `AUTHINFO`)

Individual users authenticate from their newsreader using NNTP's `AUTHINFO USER` / `AUTHINFO PASS` commands. The username and password are sent to the Lemmy API's login endpoint, and the resulting JWT is used for that NNTP session.

**By default, authentication is required for all operations.** Use `--no-auth` to allow anonymous reading.

| Mode | Reading | Posting | Admin |
|---|---|---|---|
| Default (auth required) | Requires login | Requires login | Requires admin login |
| `--no-auth` | Open to anyone | Requires login | Requires admin login |

---

## SSL / NNTPS

To enable encrypted connections, provide both `--ssl-cert` and `--ssl-key`:

```bash
python3 lemmy_nntp.py --instance https://lemmy.ml \
    --host 0.0.0.0 --port 563 \
    --ssl-cert /etc/ssl/certs/example.pem \
    --ssl-key /etc/ssl/private/example.key
```

This wraps the NNTP server in TLS (NNTPS), similar to how HTTPS wraps HTTP. The minimum TLS version is 1.2. Configure your newsreader to use SSL/TLS on the specified port.

If you're running leNNyTP behind a reverse proxy that handles TLS termination (e.g. stunnel, HAProxy), you don't need these options — just run plain NNTP on localhost.

---

## Newsgroup Mapping

Lemmy communities are mapped to NNTP newsgroup names using the pattern:

```
<instance-domain>.<community-name>
```

For example, on `lemmy.ml`:

| Lemmy Community | NNTP Newsgroup |
|---|---|
| `lemmy.ml/c/linux` | `lemmy.ml.linux` |
| `lemmy.ml/c/asklemmy` | `lemmy.ml.asklemmy` |
| `lemmy.ml/c/worldnews` | `lemmy.ml.worldnews` |

Admin groups (visible only to admins) follow the same pattern:

| Admin Group | NNTP Newsgroup |
|---|---|
| Registration applications | `lemmy.ml.admin.registrations` |
| Reports | `lemmy.ml.admin.reports` |
| Moderation log | `lemmy.ml.admin.modlog` |

---

## Reading Articles

### Posts

Each Lemmy post becomes an NNTP article. The mapping:

| NNTP Header | Source |
|---|---|
| `Subject` | Post title |
| `From` | `username <username@instance>` |
| `Date` | Post publish date |
| `Message-ID` | `<post-{id}@{domain}>` |
| `X-Lemmy-Post-Id` | Lemmy post ID |
| `X-Lemmy-Score` | Post score (upvotes minus downvotes) |
| `X-Lemmy-Author-Id` | Author's Lemmy user ID |

The body is the post's Markdown content, converted to plain text with a link reference section at the bottom. If the post has a URL, it appears as the first link. Images are referenced as `[image N]`.

### Comments

Each comment becomes a reply article with:

- `Subject: Re: <post title>`
- `References` header containing the post's Message-ID and all parent comment Message-IDs (preserving the thread tree).
- `In-Reply-To` style threading that any newsreader can follow.

This means your newsreader's threading view shows the exact same tree structure as Lemmy's nested comments.

### Body Conversion

Lemmy uses Markdown. leNNyTP converts it to readable plain text:

- **Bold** and *italic* markers are simplified.
- `[links](url)` and `![images](url)` become numbered references at the bottom of the article.
- Headings become UPPERCASE lines.
- Block quotes are prefixed with `> `.
- Lists use `  - ` indentation.
- Text is wrapped at 72 columns (Usenet convention).

---

## Posting Articles

### New Posts

To create a new Lemmy post, compose a new message in your newsreader:

- **Newsgroups:** Set to the target community, e.g. `lemmy.ml.linux`.
- **Subject:** Becomes the post title.
- **Body:** Becomes the post body (sent as-is to Lemmy, so you can use Markdown).

### Replies (Comments)

To reply to a post or comment, use your newsreader's Reply/Followup function. leNNyTP uses the `References` and `In-Reply-To` headers to determine the parent:

- Replying to a post creates a top-level comment on that post.
- Replying to a comment creates a nested reply to that comment.

The body of your reply becomes the comment content on Lemmy.

After posting, leNNyTP triggers an immediate background sync so the new article appears in the spool within seconds.

---

## Admin & Moderation

If the poller account (`--username`) is a Lemmy site admin, leNNyTP exposes three special admin newsgroups and supports inline moderation commands in any group. These groups are **only visible to users who authenticate as a Lemmy admin** — regular users cannot see or access them.

### Registration Applications

**Group:** `<domain>.admin.registrations`

Each pending registration application appears as an article with:

- The applicant's username, registration date, and application text in the body.
- Metadata encoded in the subject line: `[REG app=<id> person=<id>]`.

**To approve:** Reply to the article with a body containing just:
```
ok
```
or `approve`.

**To deny:** Reply with:
```
deny
```

leNNyTP calls the Lemmy API to approve or deny the application, then posts a system confirmation article as a reply in the thread so you have an audit trail.

### Reports

**Group:** `<domain>.admin.reports`

Unresolved post and comment reports appear as articles. Each report includes:

- Who reported it, the reason, and the original content.
- Metadata in the subject: `[REPORT type=post|comment report=<id> target=<id> author=<id>]`.

**To act on a report,** reply with one or more commands (one per line or space-separated):

| Command | Action |
|---|---|
| `del` | Delete the reported post or comment. |
| `ban` | Ban the content author instance-wide. |
| `lock` | Lock the post (posts only, ignored for comments). |
| `del ban` | Delete the content AND ban the author. |
| `del ban lock` | Delete, ban, and lock (posts only). |

After executing the commands, leNNyTP also automatically resolves the report. A system confirmation article is posted to the thread showing the result of each action.

### Moderation Log

**Group:** `<domain>.admin.modlog`

A read-only feed of the instance's moderation log. Each entry (post removals, comment removals, bans, locks) appears as an article. This group does not accept posts — it's purely for monitoring.

Entry types tracked:

- Post removals
- Comment removals
- Community bans
- Instance-wide bans
- Post locks

### Inline Moderation in Any Group

Admins can also moderate directly in regular community groups without switching to the admin groups. To do this, **reply to any post or comment** with a body containing only moderation commands:

| Command | Action |
|---|---|
| `del` | Delete the post or comment you're replying to. |
| `ban` | Ban the author of the post or comment (instance-wide). |
| `lock` | Lock the thread (finds the parent post if replying to a comment). |
| `del ban` | Delete and ban. |

leNNyTP detects that the reply body is a moderation command (not regular text) and executes it instead of posting a comment. A system confirmation article appears in the group as a reply.

**Important:** Inline moderation only triggers if the entire body (or a line within it) consists exclusively of the keywords `del`, `ban`, and/or `lock`. If your reply contains any other words, it's treated as a normal comment and posted to Lemmy.

---

## Newsreader Configuration

### Thunderbird

1. Go to **Account Settings → Account Actions → Add Other Account → Newsgroup Account**.
2. Set the server to `localhost` (or your server address), port `1190`.
3. If using SSL, check "Use secure connection (SSL)" and set the port accordingly (e.g. `563`).
4. When prompted, enter your Lemmy username and password.
5. Subscribe to groups via **File → Subscribe** (or right-click the account → Subscribe).

### slrn

```bash
export NNTPSERVER=localhost:1190
slrn --create   # first run to create config
slrn
```

In slrn, press `L` to list groups, then subscribe with `s`.

### General Tips

- **Threading:** Most newsreaders will correctly thread Lemmy conversations thanks to the `References` header.
- **Scores:** The `X-Lemmy-Score` header is available for scoring/killing in newsreaders that support it.
- **Polling:** Articles appear after the next background sync. Use a shorter `--interval` (e.g. `60`) for near-real-time updates, at the cost of more API calls.

---

## Examples

### Minimal Local Setup

```bash
python3 lemmy_nntp.py --instance https://lemmy.ml
```

Binds to `127.0.0.1:1190`, polls every 15 minutes, requires authentication for everything.

### Public Read, Frequent Polling

```bash
python3 lemmy_nntp.py --instance https://lemmy.ml \
    --no-auth --interval 120 --port 119
```

Anyone can read without logging in. Polls every 2 minutes. Standard NNTP port.

### Federated Content with Admin Features

```bash
python3 lemmy_nntp.py --instance https://mylemmy.example.com \
    --listing-type All \
    --username admin_bot --password hunter2 \
    --interval 300
```

Fetches all federated communities, polls every 5 minutes with an admin account. Admin groups are enabled.

### Internet-Facing NNTPS

```bash
python3 lemmy_nntp.py --instance https://mylemmy.example.com \
    --host 0.0.0.0 --port 563 \
    --ssl-cert /etc/letsencrypt/live/news.example.com/fullchain.pem \
    --ssl-key /etc/letsencrypt/live/news.example.com/privkey.pem \
    --username poller --password pollpass
```

Encrypted NNTPS on the standard port, accessible from the internet.

---

## Spool Directory Structure

```
spool/
├── _communities.json              # Cached community list from Lemmy
├── lemmy.ml.linux/                # One directory per newsgroup
│   ├── _index.json                # Article number ↔ Message-ID mapping
│   ├── _meta.json                 # Community metadata (ID, title, description)
│   ├── post-12345@lemmy.ml.json   # Individual article files
│   ├── comment-67890@lemmy.ml.json
│   └── ...
├── lemmy.ml.asklemmy/
│   └── ...
├── lemmy.ml.admin.registrations/  # Admin group (if admin)
│   └── ...
├── lemmy.ml.admin.reports/
│   └── ...
└── lemmy.ml.admin.modlog/
    └── ...
```

Each article is a JSON file named after its Message-ID. The `_index.json` maps sequential article numbers (as NNTP expects) to Message-IDs. The spool can be safely deleted to force a full re-sync on the next poll.

---

## Limitations

- **One-way sync for edits:** If a post or comment is edited on Lemmy after being synced, the spool keeps the original version. Delete the article's JSON file from the spool to force a re-fetch on the next poll.
- **50 posts per community per poll:** The poller fetches the 50 newest posts per community. Older posts are not retrieved unless they were already in the spool.
- **200 comments per post:** The comment fetch limit is 200 per post. Very large threads may be truncated.
- **No real-time push:** Content appears after the next poll cycle. Set `--interval` lower for faster updates.
- **No image upload:** You cannot attach images via NNTP posting. Link to externally hosted images in your post body instead.
- **Plain text only:** Articles are served as `text/plain`. Rich HTML rendering is not supported.
- **JWT expiry:** Long-running sessions may experience JWT expiration. Reconnect to re-authenticate.

---


## Disclaimer
I'm not a programmer — just a Usenet nerd who wanted to read Lemmy from a newsreader and used AI to build the tool. I can't promise the code is well-written, handles every edge case, or is safe to expose to the open internet. Run it at your own risk, especially on untrusted networks. If any actual developers or security folks want to poke holes in it and help make it better, please do — PRs and bug reports are very welcome.
