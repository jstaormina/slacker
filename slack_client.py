"""Slack API client for fetching channels, messages, and threads."""

import time
from datetime import datetime, timedelta, timezone

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Rate limit: ~1.2s between calls to stay under Tier 3 limits
RATE_LIMIT_DELAY = 1.2


class SlackClient:
    def __init__(self, token: str):
        self.client = WebClient(token=token)
        self._user_cache: dict[str, str] = {}

    def _rate_limit_pause(self):
        time.sleep(RATE_LIMIT_DELAY)

    def resolve_channel_ids(self, channel_names: list[str]) -> dict[str, str]:
        """Map channel names to channel IDs. Returns {name: id} for found channels."""
        name_to_id = {}
        cursor = None
        target_names = set(channel_names)

        while True:
            try:
                kwargs = {"types": "public_channel,private_channel", "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = self.client.conversations_list(**kwargs)
            except SlackApiError as e:
                print(f"  Error listing channels: {e.response['error']}")
                break

            for ch in resp["channels"]:
                if ch["name"] in target_names:
                    name_to_id[ch["name"]] = ch["id"]

            if len(name_to_id) == len(target_names):
                break

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            self._rate_limit_pause()

        return name_to_id

    def fetch_messages(self, channel_id: str, days: int) -> list[dict]:
        """Fetch all messages from a channel within the last N days."""
        oldest = datetime.now(timezone.utc) - timedelta(days=days)
        oldest_ts = str(oldest.timestamp())
        messages = []
        cursor = None

        while True:
            try:
                kwargs = {"channel": channel_id, "oldest": oldest_ts, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = self.client.conversations_history(**kwargs)
            except SlackApiError as e:
                print(f"  Error fetching messages: {e.response['error']}")
                break

            messages.extend(resp.get("messages", []))

            if not resp.get("has_more", False):
                break
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            self._rate_limit_pause()

        return messages

    def fetch_thread(self, channel_id: str, thread_ts: str) -> list[dict]:
        """Fetch all replies in a thread."""
        replies = []
        cursor = None

        while True:
            try:
                kwargs = {"channel": channel_id, "ts": thread_ts, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = self.client.conversations_replies(**kwargs)
            except SlackApiError as e:
                print(f"  Error fetching thread: {e.response['error']}")
                break

            replies.extend(resp.get("messages", []))

            if not resp.get("has_more", False):
                break
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            self._rate_limit_pause()

        return replies

    def fetch_surrounding_messages(
        self, channel_id: str, message_ts: str, before: int = 15, after: int = 15
    ) -> list[dict]:
        """Fetch messages surrounding a given timestamp in the channel.

        Returns up to `before` messages before and `after` messages after the target,
        sorted chronologically. This captures the channel conversation flow around
        a message, not just thread replies.
        """
        messages = []

        # Fetch messages BEFORE (latest < message_ts, going backwards)
        try:
            resp = self.client.conversations_history(
                channel=channel_id,
                latest=message_ts,
                limit=before + 1,  # +1 because latest is exclusive-ish
                inclusive=True,
            )
            messages.extend(resp.get("messages", []))
        except SlackApiError as e:
            print(f"  Error fetching surrounding (before): {e.response['error']}")

        self._rate_limit_pause()

        # Fetch messages AFTER (oldest > message_ts)
        try:
            resp = self.client.conversations_history(
                channel=channel_id,
                oldest=message_ts,
                limit=after + 1,
                inclusive=False,
            )
            messages.extend(resp.get("messages", []))
        except SlackApiError as e:
            print(f"  Error fetching surrounding (after): {e.response['error']}")

        # Deduplicate by ts and sort chronologically
        seen = set()
        unique = []
        for msg in messages:
            ts = msg.get("ts")
            if ts and ts not in seen:
                seen.add(ts)
                unique.append(msg)
        unique.sort(key=lambda m: float(m.get("ts", 0)))

        return unique

    def resolve_user(self, user_id: str) -> str:
        """Resolve a user ID to a display name. Caches results."""
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        try:
            resp = self.client.users_info(user=user_id)
            profile = resp["user"]["profile"]
            name = profile.get("display_name") or profile.get("real_name") or user_id
            self._user_cache[user_id] = name
            return name
        except SlackApiError:
            self._user_cache[user_id] = user_id
            return user_id

    def resolve_users_bulk(self, user_ids: set[str]) -> dict[str, str]:
        """Resolve multiple user IDs to display names."""
        result = {}
        for uid in user_ids:
            result[uid] = self.resolve_user(uid)
            self._rate_limit_pause()
        return result

    def get_message_permalink(self, channel_id: str, message_ts: str) -> str | None:
        """Get a permalink for a message."""
        try:
            resp = self.client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
            return resp.get("permalink")
        except SlackApiError:
            return None
