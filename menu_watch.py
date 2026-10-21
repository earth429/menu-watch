"""Bounded, at-most-once notification attempts; Python standard library only."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import sys
import tomllib
import unicodedata
import uuid
from http.client import HTTPException
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

AUTHOR_ID = "1158210930158649345"
HANDLE = "bampirePR"
TIMELINE = f"https://api.fxtwitter.com/2/profile/{HANDLE}/statuses"
ID = re.compile(r"[1-9][0-9]{0,19}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
HEADER = re.compile(r"[★☆*【】\[\]「」『』・●◆■◇]*晩杯屋新メニュー[★☆*【】\[\]「」『』・●◆■◇]*\Z")
INTRO = re.compile(r"(?:本日|今日)(?:の)?(?:オンメニュー|新メニュー)(?:は|です)?[!！:：]*\Z")
ITEM = re.compile(r"(?P<name>.+?)\s*(?P<price>[1-9][0-9]{1,4})\s*円\Z")
PRICE = re.compile(r"[1-9][0-9]{1,4}\s*円\Z")
REVIVAL = re.compile(r"復活|リターン|戻って|帰って")
UNSUPPORTED = re.compile(
    r"限定|一部店舗|対象店舗|取り扱い店舗|取扱店舗|先行|予約|予告|近日|明日|あす|来週|来月|"
    r"販売終了|提供終了|終売|販売中止|販売休止|売り切れ|売切|完売|訂正|修正|誤記|お詫び|"
    r"[0-9]{1,2}月[0-9]{1,2}日|[0-9]{1,2}/[0-9]{1,2}|から販売|から提供"
)


class WatchError(Exception):
    """Only fixed, non-secret messages belong in this exception."""


class RemoteError(WatchError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP request failed (status {status}); no retry")


@dataclass(frozen=True)
class Settings:
    frequency: str = "daily"
    hour_jst: int = 15
    minute: int = 17
    include_revivals: bool = True
    lookback_hours: int = 48
    max_pages: int = 3
    max_notifications: int = 3
    message_style: str = "summary"
    link_preview: bool = True

    @property
    def cron_utc(self) -> str:
        hour = str((self.hour_jst - 9) % 24) if self.frequency == "daily" else "*"
        return f"{self.minute} {hour} * * *"


DEFAULT_SETTINGS = Settings()
CONFIG_PATH = Path(__file__).with_name("menu-watch.toml")


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    try:
        raw = path.read_bytes()
        if len(raw) > 32768:
            raise WatchError("Settings file exceeds size limit")
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise WatchError("Cannot read settings TOML file") from None
    allowed = {
        "schedule": {"frequency", "hour_jst", "minute"},
        "watch": {"scope", "include_revivals", "lookback_hours", "max_pages", "max_notifications"},
        "notification": {"style", "link_preview"},
    }
    if set(data) - set(allowed) or any(
            not isinstance(values, dict) or set(values) - allowed[section]
            for section, values in data.items()):
        raise WatchError("Unknown settings section or key")
    schedule = data.get("schedule", {})
    watch = data.get("watch", {})
    notification = data.get("notification", {})
    settings = Settings(
        frequency=schedule.get("frequency", "daily"),
        hour_jst=schedule.get("hour_jst", 15), minute=schedule.get("minute", 17),
        include_revivals=watch.get("include_revivals", True),
        lookback_hours=watch.get("lookback_hours", 48), max_pages=watch.get("max_pages", 3),
        max_notifications=watch.get("max_notifications", 3),
        message_style=notification.get("style", "summary"),
        link_preview=notification.get("link_preview", True),
    )
    if (watch.get("scope", "common") != "common"
            or settings.frequency not in ("daily", "hourly")
            or settings.message_style not in ("summary", "original")
            or type(settings.include_revivals) is not bool or type(settings.link_preview) is not bool):
        raise WatchError("Unsupported notification scope or settings value")
    for value, lower, upper in ((settings.hour_jst, 0, 23), (settings.minute, 0, 59),
                                (settings.lookback_hours, 1, 168), (settings.max_pages, 1, 5),
                                (settings.max_notifications, 1, 3)):
        if type(value) is not int or not lower <= value <= upper:
            raise WatchError("Settings number is outside the supported range")
    return settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: object) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise WatchError("Invalid timezone-aware timestamp") from None


def decode_json(raw: bytes) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise WatchError("Invalid JSON response or file") from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTP:
    """No redirects or retries; errors never include a URL, headers or response body."""

    def __init__(self):
        self.opener = build_opener(NoRedirect())

    def json(self, method, url, payload=None, headers=None, timeout=15):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(url, data=body, method=method, headers={
            "User-Agent": "menu-watch/0.1", "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
            **(headers or {}),
        })
        try:
            with self.opener.open(request, timeout=timeout) as response:
                if response.status not in (200, 201):
                    raise RemoteError(response.status)
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise WatchError("Response exceeded size limit")
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise RemoteError(status) from None
        except (URLError, OSError, ValueError, HTTPException):
            raise WatchError("Network request failed; outcome may be unknown; no retry") from None
        return decode_json(raw)


def post_facts(post: dict) -> dict:
    """Compare notification facts, ignoring engagement/profile counters that can change per page."""
    facts = {key: post.get(key) for key in (
        "type", "id", "text", "created_timestamp", "replying_to", "reposted_by",
    )}
    author = post.get("author")
    facts["author"] = ({key: author.get(key) for key in ("id", "screen_name")}
                       if isinstance(author, dict) else author)
    facts["quoted"] = post.get("quote") is not None
    return facts


def fetch_posts(http: HTTP, settings: Settings = DEFAULT_SETTINGS) -> list[dict]:
    posts = {}
    cursor = None
    cursors = set()
    for _ in range(settings.max_pages):
        url = TIMELINE + ("?" + urlencode({"cursor": cursor}) if cursor else "")
        page = http.json("GET", url)
        if (not isinstance(page, dict) or type(page.get("code")) is not int
                or page["code"] != 200 or not isinstance(page.get("results"), list)
                or not isinstance(page.get("cursor"), dict)):
            raise WatchError("Invalid timeline page")
        for post in page["results"]:
            if not isinstance(post, dict):
                raise WatchError("Invalid timeline entry")
            post_id = post.get("id")
            if not isinstance(post_id, str) or not ID.fullmatch(post_id):
                continue
            if post_id in posts and post_facts(posts[post_id]) != post_facts(post):
                raise WatchError("Conflicting copies of a post; nothing sent")
            posts[post_id] = post
        cursor = page["cursor"].get("bottom")
        if cursor is None or cursor == "":
            break
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise WatchError("Invalid timeline cursor")
        if cursor in cursors:
            raise WatchError("Timeline cursor cycle; nothing sent")
        cursors.add(cursor)
    return list(posts.values())


def fetch_post(http: HTTP, post_id: str) -> dict:
    if not ID.fullmatch(post_id):
        raise WatchError("Invalid requested post id")
    result = http.json("GET", f"https://api.fxtwitter.com/2/status/{post_id}")
    if (not isinstance(result, dict) or type(result.get("code")) is not int
            or result["code"] != 200 or not isinstance(result.get("status"), dict)
            or result["status"].get("id") != post_id):
        raise WatchError("Invalid individual post response")
    return result["status"]


@dataclass(frozen=True)
class MenuPost:
    id: str
    timestamp: datetime
    items: tuple[tuple[str, int], ...]
    notes: tuple[str, ...] = ()
    original_text: str = ""
    image_url: str | None = None


def preview_image_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme == "https" and parsed.netloc == "pbs.twimg.com"
            and re.fullmatch(r"/media/[A-Za-z0-9_.-]+", parsed.path)
            and not parsed.fragment
            and not any(unicodedata.category(c).startswith("C") for c in value)):
        return value
    return None


def first_post_image(post: dict) -> str | None:
    media = post.get("media")
    photos = media.get("photos") if isinstance(media, dict) else None
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, dict) and photo.get("type") == "photo":
                url = preview_image_url(photo.get("url"))
                if url:
                    return url
    return None


def parse_menu(post: dict, now: datetime, start: datetime,
               settings: Settings = DEFAULT_SETTINGS) -> tuple[MenuPost | None, str]:
    """Accept a deliberately small grammar; unknown/partial layouts fail closed."""
    author = post.get("author")
    post_id = post.get("id")
    if (post.get("type") != "status" or not isinstance(author, dict)
            or str(author.get("id")) != AUTHOR_ID
            or not isinstance(author.get("screen_name"), str)
            or author["screen_name"].lower() != HANDLE.lower()
            or not isinstance(post_id, str) or not ID.fullmatch(post_id)):
        return None, "identity-or-type"
    if ("replying_to" not in post or "reposted_by" not in post
            or post["replying_to"] is not None or post["reposted_by"] is not None
            or post.get("quote") is not None):
        return None, "reply-repost-or-quote"
    ts = post.get("created_timestamp")
    if type(ts) is not int:
        return None, "timestamp"
    try:
        posted = datetime.fromtimestamp(ts, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None, "timestamp"
    if posted < max(start, now - timedelta(hours=settings.lookback_hours)) or posted > now:
        return None, "outside-window"
    text = post.get("text")
    if not isinstance(text, str) or len(text) > 10000:
        return None, "text"
    text = unicodedata.normalize("NFKC", text).strip()
    if any(unicodedata.category(c).startswith("C") and c not in "\n\r\t" for c in text):
        return None, "control-character"
    lines = text.splitlines()
    if not lines or not HEADER.fullmatch(re.sub(r"\s", "", lines[0])):
        return None, "heading"
    body = "\n".join(lines[1:]).strip()
    if UNSUPPORTED.search(body) or re.search(r"\S+店", body):
        return None, "limited-or-unsupported"
    if not settings.include_revivals and REVIVAL.search(body):
        return None, "revivals-disabled"
    blocks = re.split(r"\n\s*\n", body)
    items = []
    notes = []
    found_block = False
    for block in blocks:
        rows = [line.strip() for line in block.splitlines() if line.strip()]
        if not rows:
            continue
        has_price = any("円" in row or "¥" in row for row in rows)
        if has_price:
            if found_block:
                return None, "multiple-price-blocks"
            found_block = True
            i = 0
            while i < len(rows):
                row = rows[i]
                if row.startswith("【") and row.endswith("】"):
                    row = row[1:-1].strip()
                if not items and INTRO.fullmatch(row):
                    i += 1
                    continue
                if not items and re.fullmatch(r"(?:復活|リターン)[!！]*", row):
                    notes.append(row)
                    i += 1
                    continue
                match = ITEM.fullmatch(row)
                if not match and i + 1 < len(rows) and PRICE.fullmatch(rows[i + 1]):
                    match = ITEM.fullmatch(row + " " + rows[i + 1])
                    i += 1
                if not match:
                    return None, "incomplete-price-block"
                name = match["name"].strip()
                # Prevent prose, multiple prices or ambiguous multiple names becoming a menu item.
                if (not name or len(name) > 80 or re.search(r"[0-9円¥。、,;；/／:：!?！？#@<>【】]|https?", name)
                        or re.search(r"(?:各|より|から|につき|価格|税込|税別|値引|割引|均一)$", name)):
                    return None, "ambiguous-item"
                items.append((name, int(match["price"])))
                i += 1
        else:
            for row in rows:
                if not found_block and INTRO.fullmatch(row):
                    continue
                if re.fullmatch(r"(?:復活|リターン)[!！]*", row):
                    notes.append(row)
                    continue
                if all(word.startswith("#") for word in row.split()):
                    continue
                if found_block and (re.search(r"[。!?！？‼]", row)
                                    or re.search(r"(?:です|ます|ですね|ますね|ください|下さい)[♪~〜!！。]*$", row)):
                    if REVIVAL.search(row):
                        notes.append(row)
                    continue
                return None, "unknown-or-unpriced-line"
    if not items or len(items) > 10 or len({name for name, _ in items}) != len(items):
        return None, "missing-or-duplicate-items"
    result = MenuPost(post_id, posted, tuple(items), tuple(notes), post["text"], first_post_image(post))
    if len(format_message(result, settings).encode("utf-16-le")) // 2 > 2000:
        return None, "message-too-long"
    return result, "candidate"


def escape_discord(text: str) -> str:
    text = text.replace("@", "@\u200b")
    return re.sub(r"([\\`*_{}\[\]()<>~|>#])", r"\\\1", text)


def format_message(post: MenuPost, settings: Settings = DEFAULT_SETTINGS) -> str:
    if settings.message_style == "original":
        return (escape_discord(post.original_text) +
                f"\n\n元投稿：https://x.com/{HANDLE}/status/{post.id}")
    lines = ["晩杯屋 新メニュー"]
    lines.extend(f"・{escape_discord(name)}　{price}円" for name, price in post.items)
    lines.extend(f"原文注記：{escape_discord(note)}" for note in post.notes)
    lines.append(f"元投稿：https://x.com/{HANDLE}/status/{post.id}")
    return "\n".join(lines)


def build_preview(post: MenuPost, settings: Settings = DEFAULT_SETTINGS) -> dict | None:
    if not settings.link_preview:
        return None
    embed = {"title": "晩杯屋【公式】の投稿",
             "url": f"https://x.com/{HANDLE}/status/{post.id}", "timestamp": stamp(post.timestamp)}
    if post.original_text and settings.message_style == "summary":
        text = escape_discord(post.original_text)
        encoded = text.encode("utf-16-le")
        if len(encoded) > 6000:
            text = encoded[:5998].decode("utf-16-le", errors="ignore") + "…"
        embed["description"] = text
    image = preview_image_url(post.image_url)
    if image:
        embed["image"] = {"url": image}
    return embed


def webhook_identity(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise WatchError("Invalid Discord webhook URL") from None
    if (parsed.scheme != "https" or parsed.netloc != "discord.com"
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/api(?:/v10)?/webhooks/[1-9][0-9]{0,19}/[A-Za-z0-9_-]{20,200}", parsed.path)):
        raise WatchError("Invalid Discord webhook URL; use a standard discord.com webhook")
    return parsed.path.split("/")[-2]


def discord_payload(content: str, embed=None, link_preview=True) -> dict:
    payload = {"content": content, "allowed_mentions": {"parse": [], "users": [], "roles": []},
               "flags": 0 if link_preview else 4}
    if embed is not None and link_preview:
        payload["embeds"] = [embed]
    return payload


def send_discord(http: HTTP, url: str, content: str, embed=None, link_preview=True) -> str:
    webhook_identity(url)
    result = http.json("POST", url + "?wait=true", discord_payload(content, embed, link_preview))
    message_id = result.get("id") if isinstance(result, dict) else None
    if not isinstance(message_id, str) or not ID.fullmatch(message_id):
        raise WatchError("Discord response unconfirmed; no retry")
    return message_id


def verify_webhook(http: HTTP, url: str) -> str:
    """Confirm an incoming webhook without posting a message or logging its metadata."""
    expected_id = webhook_identity(url)
    result = http.json("GET", url)
    if (not isinstance(result, dict) or result.get("id") != expected_id
            or type(result.get("type")) is not int or result["type"] != 1
            or not isinstance(result.get("channel_id"), str)
            or not ID.fullmatch(result["channel_id"])):
        raise WatchError("Webhook could not be verified; state was not initialized")
    return expected_id


def new_state(destination: str, webhook_id: str, now: datetime) -> dict:
    return {"version": 1, "author_id": AUTHOR_ID, "destination": destination,
            "webhook_id": webhook_id, "started_at": stamp(now), "posts": {}}


def validate_state(state: object, destination: str, now: datetime) -> dict:
    if (not isinstance(state, dict) or type(state.get("version")) is not int
            or state["version"] != 1 or state.get("author_id") != AUTHOR_ID
            or state.get("destination") != destination
            or not isinstance(state.get("webhook_id"), str)
            or not ID.fullmatch(state["webhook_id"])
            or not isinstance(state.get("posts"), dict)):
        raise WatchError("Invalid state or destination mismatch; nothing sent")
    started = parse_time(state.get("started_at"))
    if started > now:
        raise WatchError("State start time is in the future")
    for post_id, entry in state["posts"].items():
        if (not ID.fullmatch(post_id) or not isinstance(entry, dict)
                or entry.get("status") not in ("reserved", "sent", "unknown")
                or not isinstance(entry.get("attempt_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", entry["attempt_id"])):
            raise WatchError("Invalid state entry")
        reserved = parse_time(entry.get("reserved_at"))
        if not started <= reserved <= now:
            raise WatchError("Invalid reservation time")
        if entry["status"] in ("sent", "unknown"):
            finished = parse_time(entry.get("finished_at"))
            if not reserved <= finished <= now:
                raise WatchError("Invalid completion time")
        if entry["status"] == "sent" and (
                not isinstance(entry.get("message_id"), str)
                or not ID.fullmatch(entry["message_id"])):
            raise WatchError("Invalid Discord message id in state")
    return state


class GitHubState:
    """A branch containing only state.json. Non-fast-forward updates are rejected."""

    def __init__(self, http: HTTP, repo: str, token: str, destination: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not token:
            raise WatchError("GITHUB_REPOSITORY and GH_TOKEN are required")
        self.http = http
        self.base = f"https://api.github.com/repos/{repo}"
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10"}
        self.branch = "menu-watch-state-" + destination
        self.head = None

    def api(self, method, path, payload=None):
        result = self.http.json(method, self.base + path, payload, self.headers, timeout=10)
        if not isinstance(result, dict):
            raise WatchError("Invalid GitHub response")
        return result

    @staticmethod
    def sha(result):
        value = result.get("sha")
        if not isinstance(value, str) or not SHA.fullmatch(value):
            raise WatchError("Invalid GitHub object id")
        return value

    def ref(self):
        result = self.api("GET", "/git/ref/heads/" + self.branch)
        if result.get("ref") != "refs/heads/" + self.branch:
            raise WatchError("Unexpected state reference")
        obj = result.get("object")
        if not isinstance(obj, dict) or obj.get("type") != "commit":
            raise WatchError("Invalid state commit reference")
        return self.sha(obj)

    def load(self):
        self.head = self.ref()  # 404 is fatal, never treated as empty history.
        result = self.api("GET", "/contents/state.json?" + urlencode({"ref": self.head}))
        if result.get("type") != "file" or result.get("encoding") != "base64":
            raise WatchError("Invalid state file response")
        try:
            raw = base64.b64decode("".join(result["content"].split()), validate=True)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise WatchError("Invalid state file encoding") from None
        if len(raw) > 512 * 1024:
            raise WatchError("State exceeds size limit; review history before continuing")
        return decode_json(raw)

    def initialize(self, state):
        try:
            self.ref()
        except RemoteError as exc:
            if exc.status != 404:
                raise
        else:
            raise WatchError("State branch already exists; initialization refused")
        self._commit(state, initialize=True)

    def save(self, state):
        if self.head is None:
            raise WatchError("Load state before saving")
        self._commit(state, initialize=False)

    def _commit(self, state, initialize):
        content = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if len(content.encode("utf-8")) > 512 * 1024:
            raise WatchError("State exceeds size limit")
        tree = self.api("POST", "/git/trees", {"tree": [
            {"path": "state.json", "mode": "100644", "type": "blob", "content": content}
        ]})
        commit = self.api("POST", "/git/commits", {
            "message": "通知履歴を初期化" if initialize else "通知履歴を更新",
            "tree": self.sha(tree), "parents": [] if initialize else [self.head],
        })
        head = self.sha(commit)
        if initialize:
            self.api("POST", "/git/refs", {"ref": "refs/heads/" + self.branch, "sha": head})
        else:
            # Sibling commits cannot fast-forward. Missing branches cannot be recreated by PATCH.
            self.api("PATCH", "/git/refs/heads/" + self.branch, {"sha": head, "force": False})
        self.head = head


def candidates(posts, state, now, settings: Settings = DEFAULT_SETTINGS, only_post_id=None):
    window_start = now - timedelta(hours=settings.lookback_hours)
    start = parse_time(state["started_at"]) if state and only_post_id is None else window_start
    results = {}
    report = []
    for post in posts:
        if only_post_id is not None and post.get("id") != only_post_id:
            continue
        candidate, reason = parse_menu(post, now, start, settings)
        if candidate and state and candidate.id in state["posts"]:
            reason = "already-reserved"
            candidate = None
        if candidate:
            results[candidate.id] = candidate
        post_id = post.get("id")
        report.append({"id": post_id if isinstance(post_id, str) and ID.fullmatch(post_id) else None,
                       "reason": reason})
    return sorted(results.values(), key=lambda item: (item.timestamp, int(item.id)))[:settings.max_notifications], report


def deliver(posts, state, store, http, webhook, now, settings: Settings = DEFAULT_SETTINGS,
            only_post_id=None):
    validate_state(state, state["destination"], now)
    if webhook_identity(webhook) != state["webhook_id"]:
        raise WatchError("Webhook does not match initialized destination")
    if only_post_id is not None and only_post_id in state["posts"]:
        print(json.dumps({"id": only_post_id, "status": "already-reserved"}))
        return
    pending, _ = candidates(posts, state, now, settings, only_post_id)
    if only_post_id is not None and not pending:
        raise WatchError("Requested post is not an eligible common announcement; nothing sent")
    for post in pending:
        # A random nonce also keeps concurrent writers' commits distinct in the same second.
        state["posts"][post.id] = {"status": "reserved", "reserved_at": stamp(utcnow()),
                                    "attempt_id": uuid.uuid4().hex}
        if only_post_id is not None:
            state["posts"][post.id]["manual"] = True
        store.save(copy.deepcopy(state))  # Must finish remotely before attempting Discord.
        try:
            message_id = send_discord(http, webhook, format_message(post, settings),
                                       build_preview(post, settings), settings.link_preview)
        except WatchError:
            state["posts"][post.id].update(status="unknown", finished_at=stamp(utcnow()))
            store.save(copy.deepcopy(state))  # If this fails, remote reservation still prevents resend.
            raise WatchError("Discord delivery failed or unconfirmed; reserved id will not retry") from None
        state["posts"][post.id].update(status="sent", message_id=message_id,
                                       finished_at=stamp(utcnow()))
        store.save(copy.deepcopy(state))
        print(json.dumps({"id": post.id, "status": "sent"}))


def refresh_preview(post, state, http, webhook, now, settings: Settings = DEFAULT_SETTINGS):
    validate_state(state, state["destination"], now)
    if webhook_identity(webhook) != state["webhook_id"]:
        raise WatchError("Webhook does not match initialized destination")
    parsed, _ = parse_menu(post, now, now-timedelta(hours=settings.lookback_hours), settings)
    entry = state["posts"].get(post.get("id"))
    if parsed is None or not isinstance(entry, dict) or entry.get("status") != "sent":
        raise WatchError("A verified recent post with a sent message is required for preview refresh")
    message_id = entry["message_id"]
    url = webhook + "/messages/" + message_id
    current = http.json("GET", url)
    source = f"https://x.com/{HANDLE}/status/{parsed.id}"
    if (not isinstance(current, dict) or current.get("id") != message_id
            or current.get("webhook_id") != state["webhook_id"]
            or not isinstance(current.get("content"), str)
            or not re.search(re.escape(source) + r"(?![0-9])", current["content"])):
        raise WatchError("Stored message does not match the selected post; nothing edited")
    embed = build_preview(parsed, settings)
    payload = discord_payload(current["content"], embed, settings.link_preview)
    payload["embeds"] = [embed] if embed else []
    result = http.json("PATCH", url, payload)
    flags = result.get("flags", 0) if isinstance(result, dict) else None
    embeds = result.get("embeds") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or result.get("id") != message_id or type(flags) is not int
            or not isinstance(embeds, list)
            or bool(flags & 4) == settings.link_preview
            or (settings.link_preview and not any(
                isinstance(item, dict) and item.get("url") == source
                for item in embeds))):
        raise WatchError("Preview edit could not be confirmed; no new message sent")
    return {"id": parsed.id, "message_id": message_id, "status": "preview-updated",
            "embed_count": len(embeds),
            "has_image": any(isinstance(item, dict) and isinstance(item.get("image"), dict)
                             for item in embeds)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dry-run", "init", "notify", "test-webhook", "refresh-preview"),
                        nargs="?", default="dry-run")
    parser.add_argument("--destination", choices=("bampire-prod", "bampire-test"), default="bampire-test")
    parser.add_argument("--fixture", type=Path, help="dry-run only: offline timeline fixture")
    parser.add_argument("--state", choices=("none", "github"), default="none", help="dry-run history source")
    parser.add_argument("--now", help="dry-run only: ISO timestamp for reproducible fixture playback")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="settings TOML file")
    parser.add_argument("--post-id", help="notify/dry-run/refresh-preview: select one recent post")
    args = parser.parse_args(argv)
    args.post_id = args.post_id or None
    if args.post_id is not None and (
            args.mode not in ("notify", "dry-run", "refresh-preview") or not ID.fullmatch(args.post_id)):
        parser.error("--post-id requires a numeric post id and notify, dry-run or refresh-preview mode")
    if args.mode == "refresh-preview" and args.post_id is None:
        parser.error("refresh-preview requires --post-id")
    if args.mode != "dry-run" and (args.fixture or args.now or args.state != "none"):
        parser.error("--fixture, --now and --state are dry-run options")
    try:
        settings = load_settings(args.config)
        if args.mode == "test-webhook" and args.destination != "bampire-test":
            raise WatchError("Connection tests are restricted to bampire-test")
        now = parse_time(args.now) if args.now else utcnow()
        http = HTTP()
        if args.mode == "test-webhook":
            message_id = send_discord(http, os.environ.get("DISCORD_WEBHOOK_URL", ""),
                                      "menu-watch 接続テスト\n"
                                      "このチャンネルに新メニュー告知を通知します。\n"
                                      "これは手動の接続確認メッセージです。")
            print(json.dumps({"connection_test": True, "message_id": message_id}))
            return 0
        state = None
        if args.mode != "dry-run" or args.state == "github":
            store = GitHubState(http, os.environ.get("GITHUB_REPOSITORY", ""),
                                os.environ.get("GH_TOKEN", ""), args.destination)
            if args.mode == "init":
                webhook_id = verify_webhook(http, os.environ.get("DISCORD_WEBHOOK_URL", ""))
                store.initialize(new_state(args.destination, webhook_id, now))
                print("State initialized at current time; no Discord message sent")
                return 0
            state = validate_state(store.load(), args.destination, now)
        if args.fixture:
            raw = args.fixture.read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise WatchError("Fixture exceeds size limit")
            page = decode_json(raw)
            if (not isinstance(page, dict) or type(page.get("code")) is not int
                    or page["code"] != 200 or not isinstance(page.get("results"), list)
                    or not all(isinstance(post, dict) for post in page["results"])):
                raise WatchError("Invalid fixture")
            posts = page["results"]
        elif args.post_id:
            posts = [fetch_post(http, args.post_id)]
        else:
            posts = fetch_posts(http, settings)
        if args.mode == "refresh-preview":
            result = refresh_preview(posts[0], state, http,
                                     os.environ.get("DISCORD_WEBHOOK_URL", ""), now, settings)
            print(json.dumps(result))
            return 0
        if args.mode == "dry-run":
            pending, report = candidates(posts, state, now, settings, args.post_id)
            print(json.dumps({"dry_run": True, "history_loaded": state is not None,
                              "notices": [{"id": p.id, "content": format_message(p, settings),
                                           "preview": build_preview(p, settings)} for p in pending],
                              "examined": report}, ensure_ascii=False, indent=2))
        else:
            deliver(posts, state, store, http, os.environ.get("DISCORD_WEBHOOK_URL", ""), now,
                    settings, args.post_id)
        return 0
    except WatchError as exc:
        # WatchError messages are fixed, sanitized strings; never print arbitrary network exceptions.
        print(f"menu-watch: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("menu-watch: local file operation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
