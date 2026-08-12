"""Mirror your Bluesky following timeline into a Telegram channel.

Polls app.bsky.feed.getTimeline, posts anything not seen before to Telegram,
and records what it has posted in state.json so nothing is duplicated.

Configuration (environment variables):
  BSKY_HANDLE          your Bluesky handle, e.g. "someone.bsky.social"
  BSKY_APP_PASSWORD    an app password from bsky.app Settings > App Passwords
  TELEGRAM_BOT_TOKEN   bot token from @BotFather
  TELEGRAM_CHAT_ID     "@yourchannel" for public channels, or "-100..." id
  INCLUDE_REPOSTS      "true" (default) or "false"
  INCLUDE_REPLIES      "false" (default) or "true"
  MAX_POSTS_PER_RUN    default 20 (safety cap so a backlog never floods the channel)
"""

import html
import json
import os
import sys
import time
from pathlib import Path

import requests

BSKY_API = "https://bsky.social/xrpc"
STATE_FILE = Path(__file__).parent / "state.json"
TIMELINE_LIMIT = 50
SECONDS_BETWEEN_POSTS = 3  # Telegram allows ~20 messages/min to one chat


def env(name, default=None):
    # In GitHub Actions a missing/misnamed secret arrives as an empty string,
    # so treat empty the same as absent.
    value = os.environ.get(name, default)
    if not (value and value.strip()):
        sys.exit(f"Missing required environment variable / repository secret: {name}. "
                 "Check Settings > Secrets and variables > Actions.")
    return value.strip()


def env_bool(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen": []}


def save_state(state):
    # Keep the file bounded; the timeline only shows recent posts anyway.
    state["seen"] = state["seen"][-1000:]
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def bsky_login(handle, app_password):
    resp = requests.post(
        f"{BSKY_API}/com.atproto.server.createSession",
        json={"identifier": handle, "password": app_password},
        timeout=30,
    )
    if resp.status_code in (400, 401):
        sys.exit(f"Bluesky login failed ({resp.status_code}): {resp.text}\n"
                 "Check the BSKY_HANDLE and BSKY_APP_PASSWORD secrets. The handle "
                 "should look like 'you.bsky.social' (no @), and the password must "
                 "be an app password from Settings > Privacy and Security > App Passwords.")
    resp.raise_for_status()
    return resp.json()["accessJwt"]


def bsky_timeline(jwt):
    resp = requests.get(
        f"{BSKY_API}/app.bsky.feed.getTimeline",
        params={"limit": TIMELINE_LIMIT},
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["feed"]


def extract_images(post):
    """Return fullsize image URLs from a post's embed, if any."""
    embed = post.get("embed") or {}
    embed_type = embed.get("$type", "")
    if embed_type.startswith("app.bsky.embed.images"):
        return [img["fullsize"] for img in embed.get("images", [])]
    if embed_type.startswith("app.bsky.embed.recordWithMedia"):
        media = embed.get("media") or {}
        if media.get("$type", "").startswith("app.bsky.embed.images"):
            return [img["fullsize"] for img in media.get("images", [])]
    return []


def post_url(post):
    rkey = post["uri"].rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{post['author']['handle']}/post/{rkey}"


def build_message(item):
    post = item["post"]
    author = post["author"]
    name = author.get("displayName") or author["handle"]
    header = f"<b>{html.escape(name)}</b> (@{html.escape(author['handle'])})"

    reason = item.get("reason") or {}
    if reason.get("$type", "").endswith("reasonRepost"):
        by = reason.get("by", {})
        reposter = by.get("displayName") or by.get("handle", "someone")
        header = f"\U0001F501 <i>Reposted by {html.escape(reposter)}</i>\n{header}"

    text = post.get("record", {}).get("text", "").strip()
    parts = [header]
    if text:
        parts.append(html.escape(text))
    parts.append(post_url(post))
    return "\n\n".join(parts)


class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def _call(self, method, payload):
        resp = requests.post(f"{self.base}/{method}", json=payload, timeout=60)
        if resp.status_code == 429:
            retry = resp.json().get("parameters", {}).get("retry_after", 30)
            time.sleep(retry + 1)
            resp = requests.post(f"{self.base}/{method}", json=payload, timeout=60)
        if resp.status_code in (400, 401, 403, 404) and method == "sendMessage":
            sys.exit(f"Telegram {method} failed ({resp.status_code}): {resp.text}\n"
                     "Common causes: TELEGRAM_CHAT_ID is wrong (public channels use "
                     "'@channelname'; private ones a '-100...' id), the bot was not "
                     "added as a channel admin, or TELEGRAM_BOT_TOKEN is wrong.")
        resp.raise_for_status()
        return resp.json()

    def send_text(self, text):
        self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
        })

    def send_post(self, message, image_urls):
        # Telegram captions max out at 1024 chars; fall back to a plain
        # message (with link preview) if the caption would be truncated badly.
        if not image_urls or len(message) > 1024:
            self.send_text(message)
            if image_urls and len(message) > 1024:
                return  # link preview will carry the media
            return
        try:
            if len(image_urls) == 1:
                self._call("sendPhoto", {
                    "chat_id": self.chat_id,
                    "photo": image_urls[0],
                    "caption": message,
                    "parse_mode": "HTML",
                })
            else:
                media = [{"type": "photo", "media": url} for url in image_urls[:10]]
                media[0]["caption"] = message
                media[0]["parse_mode"] = "HTML"
                self._call("sendMediaGroup", {
                    "chat_id": self.chat_id,
                    "media": media,
                })
        except requests.HTTPError:
            # Telegram sometimes refuses to fetch a remote image; the post
            # still goes out as text with a link back to the original.
            self.send_text(message)


def main():
    include_reposts = env_bool("INCLUDE_REPOSTS", "true")
    include_replies = env_bool("INCLUDE_REPLIES", "false")
    max_posts = int(os.environ.get("MAX_POSTS_PER_RUN", "20"))

    jwt = bsky_login(env("BSKY_HANDLE"), env("BSKY_APP_PASSWORD"))
    feed = bsky_timeline(jwt)

    state = load_state()
    seen = set(state["seen"])
    first_run = not state["seen"]

    def mark_seen(uri):
        seen.add(uri)
        state["seen"].append(uri)

    fresh = []
    for item in feed:
        post = item["post"]
        if post["uri"] in seen:
            continue
        is_repost = (item.get("reason") or {}).get("$type", "").endswith("reasonRepost")
        if is_repost and not include_reposts:
            mark_seen(post["uri"])
            continue
        if item.get("reply") and not is_repost and not include_replies:
            mark_seen(post["uri"])
            continue
        fresh.append(item)

    fresh.reverse()  # oldest first, so the channel reads chronologically

    if first_run:
        # Don't flood the channel with 50 old posts on the very first run;
        # just remember the current timeline and start mirroring from here.
        for item in fresh:
            mark_seen(item["post"]["uri"])
        save_state(state)
        print(f"First run: marked {len(fresh)} existing posts as seen. "
              "New posts will be mirrored from now on.")
        return

    telegram = Telegram(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"))
    posted = 0
    for item in fresh[:max_posts]:
        post = item["post"]
        message = build_message(item)
        telegram.send_post(message, extract_images(post))
        mark_seen(post["uri"])
        posted += 1
        # Persist after every send so a crash mid-run never causes duplicates.
        save_state(state)
        time.sleep(SECONDS_BETWEEN_POSTS)

    save_state(state)  # also persists URIs of filtered-out reposts/replies
    skipped = len(fresh) - posted
    print(f"Posted {posted} new item(s)." + (f" ({skipped} deferred to next run.)" if skipped > 0 else ""))


if __name__ == "__main__":
    main()
