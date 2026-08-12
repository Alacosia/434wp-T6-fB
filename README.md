# Bluesky → Telegram mirror

Mirrors your Bluesky **following timeline** into a Telegram channel. Runs for
free on GitHub Actions every 15 minutes — no server of your own needed.

Each new post from people you follow is posted to the channel with the author's
name, the post text, attached images, and a link back to the original on Bluesky.

## One-time setup

### 1. Telegram bot and channel

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts.
   Copy the **bot token** it gives you (looks like `123456:ABC-DEF...`).
2. Create your channel (or use an existing one).
3. Open the channel's info → **Administrators** → add your new bot as an admin
   (it only needs the "Post messages" permission).
4. Find your **chat id**:
   - Public channel: it's just `@yourchannelname`.
   - Private channel: open the channel in [Telegram Web](https://web.telegram.org);
     the URL ends in a number like `#-1234567890123`. Your chat id is that number
     with `-100` in front of the bare digits — e.g. URL number `-1234567890123`
     means chat id `-1001234567890123`... in practice just copy the number from
     the URL and try it; if Telegram says "chat not found", prefix `-100`.

### 2. Bluesky app password

1. On [bsky.app](https://bsky.app): **Settings → Privacy and Security → App Passwords**.
2. Create one (name it anything, e.g. `telegram-mirror`) and copy it.
   This is *not* your main password — it can be revoked anytime.

### 3. GitHub repository

1. Create a new GitHub repository and push this folder to it.
   A **public** repo gets unlimited free Actions minutes; the code contains no
   secrets, so public is safe and recommended. (If you prefer a private repo,
   change the cron in `.github/workflows/mirror.yml` to `*/30 * * * *` to stay
   inside the 2,000 free minutes/month.)
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
   Add these four secrets:

   | Name | Value |
   |---|---|
   | `BSKY_HANDLE` | your handle, e.g. `you.bsky.social` |
   | `BSKY_APP_PASSWORD` | the app password from step 2 |
   | `TELEGRAM_BOT_TOKEN` | the token from BotFather |
   | `TELEGRAM_CHAT_ID` | `@yourchannel` or the `-100...` id |
   | `TELEGRAM_USER_ID` | your numeric Telegram id (see "Like / Repost buttons" below) |

3. Go to the **Actions** tab, enable workflows if prompted, open
   **Mirror Bluesky to Telegram**, and click **Run workflow** to test it.

The **first run posts nothing** — it just records the current timeline so your
channel isn't flooded with old posts. Everything new after that gets mirrored.

## Like / Repost buttons

Every mirrored post carries ❤️ Like and 🔁 Repost buttons that act **from your
Bluesky account** — no need to open Bluesky. (Multi-image posts get the buttons
on a small companion message, since Telegram albums can't carry buttons.)

Because the bot only runs every 15 minutes, a tap is not applied instantly:
the button's loading spinner will time out (this is normal), and on the next
run the like/repost happens and the button changes to "❤️ Liked ✓". To undo a
like/repost you'll need to do it on Bluesky itself.

**Strongly recommended:** add a `TELEGRAM_USER_ID` secret with your numeric
Telegram id (message **@userinfobot** and it replies with it). Only that
user's button presses are honored; without it, *anyone* who can see the
channel can like/repost from your account.

## Options

Set these as extra repository secrets (or edit the defaults in `bot.py`):

- `INCLUDE_REPOSTS` — `true` (default) / `false`
- `INCLUDE_REPLIES` — `false` (default) / `true`
- `MAX_POSTS_PER_RUN` — default `100`; anything beyond this waits for the next run

## Notes and limits

- The schedule is every 15 minutes, but GitHub Actions cron is best-effort —
  expect posts to arrive within ~15–30 minutes of the original.
- Videos aren't re-uploaded; those posts go out as text with a link (the link
  preview usually shows the video).
- `state.json` is the bot's memory of what it has already posted. The workflow
  commits it back to the repo after each run. Don't delete it unless you want
  the bot to re-baseline (it will skip, not repost, the current timeline).

## Running locally instead

```bash
pip install -r requirements.txt
```

Set the four environment variables and run `python bot.py`. Each run does one
check-and-post pass, so schedule it with Task Scheduler / cron if you want it
continuous.
