"""Slack Web API client for fetching channel messages without Playwright."""

import re

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def create_client(token: str) -> WebClient:
    """Create an authenticated Slack WebClient."""
    return WebClient(token=token)


def test_auth(client: WebClient) -> dict:
    """Validate the token and return workspace info.

    Returns dict with keys: ok, team, user, user_id, team_id.
    Raises SlackApiError on failure.
    """
    resp = client.auth_test()
    return {
        "ok": resp["ok"],
        "team": resp.get("team", ""),
        "user": resp.get("user", ""),
        "user_id": resp.get("user_id", ""),
        "team_id": resp.get("team_id", ""),
    }


def list_channels(client: WebClient) -> list[dict]:
    """List all accessible channels (public + private).

    Returns list of {id, name} dicts.
    """
    channels = []
    cursor = None

    while True:
        kwargs = {
            "types": "public_channel,private_channel",
            "exclude_archived": True,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor

        resp = client.conversations_list(**kwargs)
        for ch in resp.get("channels", []):
            channels.append({"id": ch["id"], "name": ch["name"]})

        cursor = resp.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    return channels


def resolve_channel_names(client: WebClient, names: list[str]) -> list[dict]:
    """Resolve channel names to IDs.

    Returns list of {id, name} dicts for matched channels.
    Raises ValueError if any name is not found.
    """
    all_channels = list_channels(client)
    by_name = {ch["name"].lower(): ch for ch in all_channels}

    resolved = []
    missing = []
    for name in names:
        key = name.strip().lower().lstrip("#")
        if key in by_name:
            resolved.append(by_name[key])
        else:
            missing.append(name)

    if missing:
        raise ValueError(f"Channel(s) not found: {', '.join(missing)}")

    return resolved


def fetch_channel_messages(
    client: WebClient,
    channel_id: str,
    channel_name: str,
    user_cache: dict | None = None,
    log=print,
) -> list[dict]:
    """Fetch all messages from a channel via the Slack API.

    Returns messages in the same format as convert_scraped_messages():
        {ts, text, user}

    Args:
        client: Authenticated WebClient.
        channel_id: Slack channel ID.
        channel_name: Human-readable channel name (for logging).
        user_cache: Optional shared dict of user_id -> display_name.
        log: Logging callable.
    """
    if user_cache is None:
        user_cache = {}

    messages = []
    cursor = None

    raw_count = 0
    skipped_subtype = 0
    skipped_empty = 0

    while True:
        kwargs = {"channel": channel_id, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor

        try:
            resp = client.conversations_history(**kwargs)
        except SlackApiError as e:
            if e.response.get("error") == "not_in_channel":
                log(f"  {channel_name}: not in channel, attempting to join...")
                try:
                    client.conversations_join(channel=channel_id)
                    resp = client.conversations_history(**kwargs)
                except SlackApiError as join_err:
                    log(f"  {channel_name}: could not join ({join_err.response.get('error', join_err)}), skipping")
                    return []
            else:
                log(f"  {channel_name}: API error: {e.response.get('error', e)}")
                return []

        batch = resp.get("messages", [])
        raw_count += len(batch)

        for msg in batch:
            subtype = msg.get("subtype")
            if subtype in ("channel_join", "channel_leave", "channel_topic",
                           "channel_purpose", "channel_name"):
                skipped_subtype += 1
                continue

            text = msg.get("text", "")
            if not text:
                skipped_empty += 1
                continue

            user_id = msg.get("user", "")
            user_name = _resolve_user(client, user_id, user_cache) if user_id else "unknown"
            text = _clean_slack_markup(text, user_cache)

            messages.append({
                "ts": msg.get("ts", "0"),
                "text": text,
                "user": user_name,
            })

        cursor = resp.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    if raw_count > 0 and not messages:
        log(f"  {channel_name}: API returned {raw_count} messages but all filtered "
            f"(subtype={skipped_subtype}, empty={skipped_empty})")
    elif raw_count == 0:
        log(f"  {channel_name}: API returned 0 messages")

    # API returns newest first; reverse to oldest first
    messages.reverse()
    return messages


def _clean_slack_markup(text: str, user_cache: dict) -> str:
    """Convert Slack mrkdwn to plain text.

    Handles: <@U123> mentions, <#C123|name> channels, <url|label> links,
    <!subteam^ID|@name> groups, and :emoji: codes.
    """
    # User mentions: <@U12345> -> @display_name
    def replace_user(m):
        uid = m.group(1)
        return f"@{user_cache.get(uid, uid)}"
    text = re.sub(r"<@(U[A-Z0-9]+)>", replace_user, text)

    # Channel references: <#C12345|channel-name> -> #channel-name
    text = re.sub(r"<#C[A-Z0-9]+\|([^>]+)>", r"#\1", text)

    # Links: <http://url|label> -> label, <http://url> -> url
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)

    # Subteam/special mentions: <!subteam^ID|@name> -> @name
    text = re.sub(r"<!subteam\^[A-Z0-9]+\|(@[^>]+)>", r"\1", text)
    text = re.sub(r"<!([a-z]+)>", r"@\1", text)  # <!here>, <!channel>, <!everyone>

    return text


def _resolve_user(client: WebClient, user_id: str, cache: dict) -> str:
    """Resolve a user ID to a display name, with caching."""
    if user_id in cache:
        return cache[user_id]

    try:
        resp = client.users_info(user=user_id)
        user = resp.get("user", {})
        name = (
            user.get("profile", {}).get("display_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
        cache[user_id] = name
        return name
    except SlackApiError:
        cache[user_id] = user_id
        return user_id
