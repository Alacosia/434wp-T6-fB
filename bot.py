"""Mirror your Bluesky following timeline into a Telegram channel.

Each run does two things:
  1. Collects Like/Repost button presses from the channel (via getUpdates)
     and applies them to Bluesky from your account.
  2. Polls app.bsky.feed.getTimeline and posts anything new to Telegram,
     with inline Like/Repost buttons.

state.json remembers what has been posted, which buttons were pressed, and
the Telegram update offset, so nothing is duplicated across runs.

Configuration (environment variables):
  BSKY_HANDLE          your Bluesky handle, e.g. "someone.bsky.social"
  BSKY_APP_PASSWORD    an app password from bsky.app Settings > App Passwords
  TELEGRAM_BOT_TOKEN   bot token from @BotFather
  TELEGRAM_CHAT_ID     "@yourchannel" for public channels, or "-100..." id
  TELEGRAM_USER_ID     your numeric Telegram id; only this user's button
                       presses are honored (recommended — message
                       @userinfobot to get it). If unset, anyone in the
                       channel can trigger likes/reposts from YOUR account.
  INCLUDE_REPOSTS      "true" (default) or "false"
  INCLUDE_REPLIES      "false" (default) or "true"
  MAX_POSTS_PER_RUN    default 20 (safety cap so a backlog never floods the channel)
"""

import html
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BSKY_API = "https://bsky.social/xrpc"
STATE_FILE = Path(__file__).parent / "state.json"
TIMELINE_LIMIT = 100  # Bluesky's maximum per request
SECONDS_BETWEEN_POSTS = 3  # Telegram allows ~20 messages/min to one chat
MAX_TRACKED_POSTS = 300    # how many mirrored posts keep working buttons


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
    state = {"seen": [], "tg_offset": 0, "next_id": 1, "posts": {}}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return state


def save_state(state):
    # Keep the file bounded; the timeline only shows recent posts anyway.
    state["seen"] = state["seen"][-1000:]
    while len(state["posts"]) > MAX_TRACKED_POSTS:
        state["posts"].pop(next(iter(state["posts"])))
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- Bluesky ---

class Bluesky:
    def __init__(self, handle, app_password):
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
        data = resp.json()
        self.did = data["did"]
        self.headers = {"Authorization": f"Bearer {data['accessJwt']}"}

    def timeline(self):
        resp = requests.get(
            f"{BSKY_API}/app.bsky.feed.getTimeline",
            params={"limit": TIMELINE_LIMIT},
            headers=self.headers,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["feed"]

    def _create_record(self, collection, subject_uri, subject_cid):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        resp = requests.post(
            f"{BSKY_API}/com.atproto.repo.createRecord",
            headers=self.headers,
            json={
                "repo": self.did,
                "collection": collection,
                "record": {
                    "$type": collection,
                    "subject": {"uri": subject_uri, "cid": subject_cid},
                    "createdAt": now,
                },
            },
            timeout=30,
        )
        resp.raise_for_status()

    def like(self, uri, cid):
        self._create_record("app.bsky.feed.like", uri, cid)

    def repost(self, uri, cid):
        self._create_record("app.bsky.feed.repost", uri, cid)


# --------------------------------------------------------------- Telegram ---

def keyboard(post_id, liked, reposted):
    return {"inline_keyboard": [[
        {"text": "❤️ Liked ✓" if liked else "❤️ Like",
         "callback_data": "noop" if liked else f"like:{post_id}"},
        {"text": "\U0001F501 Reposted ✓" if reposted else "\U0001F501 Repost",
         "callback_data": "noop" if reposted else f"repost:{post_id}"},
    ]]}


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
        return resp.json()["result"]

    def _call_quiet(self, method, payload):
        """For best-effort calls (stale callbacks, unchanged keyboards)."""
        try:
            return self._call(method, payload)
        except requests.HTTPError:
            return None

    def get_updates(self, offset):
        return self._call("getUpdates", {
            "offset": offset,
            "timeout": 0,
            "allowed_updates": ["callback_query"],
        })

    def answer_callback(self, callback_id, text=None):
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self._call_quiet("answerCallbackQuery", payload)

    def update_keyboard(self, chat_id, message_id, markup):
        self._call_quiet("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": markup,
        })

    def send_text(self, text, markup=None):
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
        }
        if markup:
            payload["reply_markup"] = markup
        return self._call("sendMessage", payload)

    def send_post(self, message, image_urls, markup):
        """Send one mirrored post. Returns the message carrying the buttons."""
        if not image_urls or len(message) > 1024:
            # No media, or the caption would be truncated: plain message
            # (the link preview carries the media in the long-caption case).
            return self.send_text(message, markup)
        try:
            if len(image_urls) == 1:
                return self._call("sendPhoto", {
                    "chat_id": self.chat_id,
                    "photo": image_urls[0],
                    "caption": message,
                    "parse_mode": "HTML",
                    "reply_markup": markup,
                })
            # Telegram albums cannot carry inline keyboards, so the buttons
            # ride on a small companion reply message.
            media = [{"type": "photo", "media": url} for url in image_urls[:10]]
            media[0]["caption"] = message
            media[0]["parse_mode"] = "HTML"
            album = self._call("sendMediaGroup", {
                "chat_id": self.chat_id,
                "media": media,
            })
            return self._call("sendMessage", {
                "chat_id": self.chat_id,
                "text": "⬆️",
                "reply_to_message_id": album[0]["message_id"],
                "reply_markup": markup,
            })
        except requests.HTTPError:
            # Telegram sometimes refuses to fetch a remote image; the post
            # still goes out as text with a link back to the original.
            return self.send_text(message, markup)


# ------------------------------------------------------------- formatting ---

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


# ---------------------------------------------------------------- actions ---

def process_button_presses(state, telegram, bsky, allowed_user):
    """Apply Like/Repost button presses collected since the last run."""
    applied = 0
    for update in telegram.get_updates(state["tg_offset"]):
        state["tg_offset"] = update["update_id"] + 1
        cq = update.get("callback_query")
        if not cq:
            continue
        if allowed_user and str(cq["from"]["id"]) != allowed_user:
            telegram.answer_callback(cq["id"], "Only the channel owner can use these buttons.")
            continue
        action, _, post_id = (cq.get("data") or "").partition(":")
        tracked = state["posts"].get(post_id)
        if action not in ("like", "repost") or not tracked:
            telegram.answer_callback(cq["id"])
            continue
        flag = "liked" if action == "like" else "reposted"
        if not tracked[flag]:
            getattr(bsky, action)(tracked["uri"], tracked["cid"])
            tracked[flag] = True
            applied += 1
            telegram.update_keyboard(
                tracked["chat_id"], tracked["message_id"],
                keyboard(post_id, tracked["liked"], tracked["reposted"]),
            )
            save_state(state)
        telegram.answer_callback(cq["id"])
    if applied:
        print(f"Applied {applied} like/repost button press(es) to Bluesky.")


def main():
    include_reposts = env_bool("INCLUDE_REPOSTS", "true")
    include_replies = env_bool("INCLUDE_REPLIES", "false")
    max_posts = int(os.environ.get("MAX_POSTS_PER_RUN", "100"))
    allowed_user = os.environ.get("TELEGRAM_USER_ID", "").strip()

    bsky = Bluesky(env("BSKY_HANDLE"), env("BSKY_APP_PASSWORD"))
    telegram = Telegram(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"))

    state = load_state()
    seen = set(state["seen"])
    first_run = not state["seen"]

    process_button_presses(state, telegram, bsky, allowed_user)
    save_state(state)  # persist the update offset even if mirroring fails

    def mark_seen(uri):
        seen.add(uri)
        state["seen"].append(uri)

    fresh = []
    for item in bsky.timeline():
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

    posted = 0
    for item in fresh[:max_posts]:
        post = item["post"]
        post_id = str(state["next_id"])
        markup = keyboard(post_id, liked=False, reposted=False)
        sent = telegram.send_post(build_message(item), extract_images(post), markup)
        state["next_id"] += 1
        state["posts"][post_id] = {
            "uri": post["uri"],
            "cid": post["cid"],
            "chat_id": sent["chat"]["id"],
            "message_id": sent["message_id"],
            "liked": False,
            "reposted": False,
        }
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
