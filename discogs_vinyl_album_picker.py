#!/usr/bin/env python3
"""
discogs_vinyl_album_picker.py

Picks N random albums from your Discogs collection for a rotating wall
display. Tracks which albums have been displayed recently so the same
sleeves don't keep getting cooked by the sun.

Usage:
    export DISCOGS_USERNAME=your_username
    export DISCOGS_TOKEN=your_personal_access_token
    python discogs_vinyl_album_picker.py

    # Or pass on the command line:
    python discogs_vinyl_album_picker.py --username you --token abc123 --count 24

    # Ignore history (true random):
    python discogs_vinyl_album_picker.py --no-history

    # Clear history:
    python discogs_vinyl_album_picker.py --reset-history

Get a token at: https://www.discogs.com/settings/developers
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from datetime import date, timedelta
from pathlib import Path

import requests

DISCOGS_API = "https://api.discogs.com"
HISTORY_FILE = Path(__file__).resolve().parent / "discogs_wall_history.json"
CACHE_FILE = Path(__file__).resolve().parent / "discogs_collection_cache.json"
USER_AGENT = "VinylWallPicker/1.0"
DEFAULT_COUNT = 24
DEFAULT_FORMATS = ["Vinyl"]              # case-insensitive; [] = no filter (any format)
DEFAULT_EXCLUDE_FORMATS = ["Box Set"]    # always-removed formats; won't fit on shallow shelves
DEFAULT_FIELD_NAME = "Wall Display"      # Discogs custom field controlling inclusion
DEFAULT_FIELD_VALUE = "Yes"              # the value required to be included
DEFAULT_COOLDOWN_DAYS = 90               # days an album stays out of rotation after display


def fetch_collection(username: str, token: str) -> list[dict]:
    """Fetch the full collection (folder 0 = All) with pagination."""
    releases = []
    page = 1
    per_page = 100
    headers = {"User-Agent": USER_AGENT}

    while True:
        url = f"{DISCOGS_API}/users/{username}/collection/folders/0/releases"
        params = {"token": token, "page": page, "per_page": per_page}
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for r in data.get("releases", []):
            info = r.get("basic_information", {})
            artists_list = [a["name"] for a in info.get("artists", [])]
            formats_raw = info.get("formats", [])
            releases.append({
                "id": r["id"],
                "artist": ", ".join(artists_list),
                "artists": artists_list,
                "title": info.get("title", ""),
                "year": info.get("year", 0),
                "formats": [f.get("name", "") for f in formats_raw],
                "format_descriptions": [d for f in formats_raw for d in f.get("descriptions", [])],
                "notes": {str(n["field_id"]): n.get("value", "") for n in r.get("notes", [])},
                "url": f"https://www.discogs.com/release/{r['id']}",
            })

        pagination = data.get("pagination", {})
        if page >= pagination.get("pages", 1):
            break
        page += 1
        time.sleep(1)  # be polite to the API

    return releases


def fetch_collection_fields(username: str, token: str) -> dict[str, int]:
    """Fetch the user's custom collection fields. Returns {field_name: field_id}."""
    url = f"{DISCOGS_API}/users/{username}/collection/fields"
    headers = {"User-Agent": USER_AGENT}
    params = {"token": token}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return {f["name"]: f["id"] for f in resp.json().get("fields", [])}


def filter_by_field(releases: list[dict], field_id: int, required_value: str) -> list[dict]:
    """Keep releases whose custom field equals required_value (case-insensitive)."""
    key = str(field_id)
    target = required_value.strip().lower()
    return [r for r in releases if r["notes"].get(key, "").strip().lower() == target]


def filter_by_format(releases: list[dict], formats: list[str]) -> list[dict]:
    """Keep releases whose format list contains any of the given format names."""
    if not formats:
        return releases
    wanted = {f.lower() for f in formats}
    return [r for r in releases if wanted & {f.lower() for f in r["formats"]}]


def exclude_by_format(releases: list[dict], formats: list[str]) -> list[dict]:
    """Drop releases whose format names or descriptions contain any of the given names."""
    if not formats:
        return releases
    unwanted = {f.lower() for f in formats}

    def matches(r: dict) -> bool:
        tags = {t.lower() for t in r["formats"]} | {t.lower() for t in r.get("format_descriptions", [])}
        return bool(unwanted & tags)

    return [r for r in releases if not matches(r)]


def normalize_artist(name: str) -> str:
    """Strip a leading 'The ' so sorting/grouping treats 'The Beatles' as 'Beatles'."""
    n = name.strip()
    if n.lower().startswith("the "):
        n = n[4:]
    return n


def artist_key(release: dict) -> str:
    """Identity for the primary artist. Used to prevent same-artist duplicates on the wall.

    Also subsumes album-level dedup: two pressings of the same album necessarily
    share an artist, so they can't both be picked.
    """
    primary = release["artists"][0] if release.get("artists") else release.get("artist", "")
    return normalize_artist(primary).lower()


def load_history() -> dict:
    """History schema: {'displayed': {release_id_str: 'YYYY-MM-DD'}}.

    Migrates the older {'picks': [[id, ...], ...]} schema by marking every
    id in it as displayed today (the safest assumption — keeps recently-shown
    records on cooldown for the full window).
    """
    if not HISTORY_FILE.exists():
        return {"displayed": {}}
    try:
        data = json.loads(HISTORY_FILE.read_text())
    except json.JSONDecodeError:
        return {"displayed": {}}

    if "displayed" in data:
        return {"displayed": data["displayed"]}

    if "picks" in data:
        today = date.today().isoformat()
        displayed = {}
        for pick in data["picks"]:
            for release_id in pick:
                displayed[str(release_id)] = today
        if displayed:
            print(f"Migrated {len(displayed)} releases from old history format "
                  f"(all marked as displayed today).")
        return {"displayed": displayed}

    return {"displayed": {}}


def save_history(history: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def recently_displayed(history: dict, cooldown_days: int) -> set[str]:
    """Set of release IDs (as strings) currently within the cooldown window."""
    if cooldown_days <= 0:
        return set()
    cutoff = date.today() - timedelta(days=cooldown_days)
    recent = set()
    for release_id, iso in history["displayed"].items():
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if d >= cutoff:
            recent.add(release_id)
    return recent


def load_cache() -> list[dict] | None:
    """Return today's cached collection, or None if missing/stale/corrupt."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        if data.get("date") == date.today().isoformat():
            return data.get("releases")
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def save_cache(releases: list[dict]) -> None:
    CACHE_FILE.write_text(json.dumps({
        "date": date.today().isoformat(),
        "releases": releases,
    }, indent=2))


def pick_albums(releases: list[dict], count: int, history: dict, cooldown_days: int) -> list[dict]:
    """Pick `count` releases, preferring ones outside the cooldown window.

    Constraints:
    - No two releases share a primary artist (so no two records by the same artist
      end up on the wall at once).
    - This also rules out duplicate albums (e.g. multiple pressings of one title),
      since two pressings of the same album necessarily share an artist.
    """
    recent = recently_displayed(history, cooldown_days)
    fresh = [r for r in releases if str(r["id"]) not in recent]

    def sample_unique_artists(pool: list[dict], n: int) -> list[dict]:
        """Sample n releases such that no two share the same primary artist."""
        by_artist: dict[str, list[dict]] = {}
        for r in pool:
            by_artist.setdefault(artist_key(r), []).append(r)
        keys = list(by_artist.keys())
        chosen = random.sample(keys, min(n, len(keys)))
        return [random.choice(by_artist[k]) for k in chosen]

    picks = sample_unique_artists(fresh, count)

    if len(picks) < count:
        # Not enough unique artists in fresh pool — fill from cooldown,
        # avoiding artists already represented in picks.
        already = {artist_key(r) for r in picks}
        leftover = [r for r in releases if artist_key(r) not in already]
        picks.extend(sample_unique_artists(leftover, count - len(picks)))
        random.shuffle(picks)

    return picks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pick random albums from your Discogs collection for a wall display."
    )
    parser.add_argument("--username", default=os.environ.get("DISCOGS_USERNAME"))
    parser.add_argument("--token", default=os.environ.get("DISCOGS_TOKEN"))
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument(
        "--formats",
        default=",".join(DEFAULT_FORMATS),
        help='Comma-separated format names to include (case-insensitive). '
             'Default: "Vinyl". Use "" or "any" for no filter. '
             'Examples: "Vinyl", "Vinyl,Cassette", "CD".',
    )
    parser.add_argument(
        "--exclude-formats",
        default=",".join(DEFAULT_EXCLUDE_FORMATS),
        help='Comma-separated format names to exclude (case-insensitive). '
             'Default: "Box Set". Use "" to exclude nothing.',
    )
    parser.add_argument(
        "--ignore-wall-display",
        action="store_true",
        help=f'Skip the "{DEFAULT_FIELD_NAME}" custom field filter. '
             f'By default, only releases tagged "{DEFAULT_FIELD_NAME}" = "{DEFAULT_FIELD_VALUE}" are eligible.',
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force a fresh fetch from Discogs and overwrite today's cache.",
    )
    parser.add_argument(
        "--cooldown-days",
        type=int,
        default=DEFAULT_COOLDOWN_DAYS,
        help=f"Days a displayed album stays out of rotation (default: {DEFAULT_COOLDOWN_DAYS}). "
             "Use 0 to disable cooldown entirely.",
    )
    parser.add_argument("--no-history", action="store_true",
                        help="Don't filter by display history (pure random).")
    parser.add_argument("--reset-history", action="store_true",
                        help="Clear display history and exit.")
    parser.add_argument("--output", help="Optional: write picks to a JSON file as well.")
    args = parser.parse_args()

    if args.reset_history:
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
            print(f"Cleared history at {HISTORY_FILE}")
        else:
            print("No history file to clear.")
        return

    if not args.username or not args.token:
        parser.error(
            "Need --username and --token (or DISCOGS_USERNAME / DISCOGS_TOKEN env vars)."
        )

    cached = None if args.refresh else load_cache()
    using_cache = cached is not None
    if using_cache:
        releases = cached
        print(f"Using cached collection from today ({len(releases)} releases). "
              f"Use --refresh to re-fetch.")
    else:
        print(f"Fetching collection for {args.username}...")
        releases = fetch_collection(args.username, args.token)
        save_cache(releases)
        print(f"Found {len(releases)} releases in collection. (Cached for today.)")

    if args.formats.strip().lower() in ("", "any"):
        formats = []
    else:
        formats = [f.strip() for f in args.formats.split(",") if f.strip()]

    if formats:
        releases = filter_by_format(releases, formats)
        print(f"{len(releases)} match format filter: {', '.join(formats)}")

    exclude_formats = [f.strip() for f in args.exclude_formats.split(",") if f.strip()]
    if exclude_formats:
        before = len(releases)
        releases = exclude_by_format(releases, exclude_formats)
        dropped = before - len(releases)
        if dropped:
            print(f"Excluded {dropped} ({', '.join(exclude_formats)}); {len(releases)} remaining.")

    if not args.ignore_wall_display:
        fields = fetch_collection_fields(args.username, args.token)
        field_id = fields.get(DEFAULT_FIELD_NAME)
        if field_id is None:
            print(f'Warning: no custom field named "{DEFAULT_FIELD_NAME}" found. '
                  f'Skipping field filter — pass --ignore-wall-display to silence this.')
        else:
            before = len(releases)
            releases = filter_by_field(releases, field_id, DEFAULT_FIELD_VALUE)
            print(f'{len(releases)} have "{DEFAULT_FIELD_NAME}" = "{DEFAULT_FIELD_VALUE}" '
                  f'(filtered out {before - len(releases)}).')
            if len(releases) == 0 and using_cache:
                print('Hint: if you recently tagged releases on Discogs, '
                      'run with --refresh to update the cache.')
    print()

    if len(releases) < args.count:
        print(f"Only {len(releases)} releases in collection — picking all of them.")
        args.count = len(releases)

    history = load_history() if not args.no_history else {"displayed": {}}
    picks = pick_albums(releases, args.count, history, args.cooldown_days)
    picks.sort(key=lambda r: normalize_artist(r["artist"]).lower())

    print(f"=== {len(picks)} albums for the wall ===\n")
    for i, r in enumerate(picks, 1):
        year = f" ({r['year']})" if r["year"] else ""
        fmt = f" [{', '.join(r['formats'])}]" if r["formats"] else ""
        print(f"{i:2}. {r['artist']} — {r['title']}{year}{fmt}")
        print(f"    {r['url']}")

    if not args.no_history:
        today = date.today().isoformat()
        for r in picks:
            history["displayed"][str(r["id"])] = today
        save_history(history)
        cooldown_count = len(recently_displayed(history, args.cooldown_days))
        print(f"\nHistory saved to {HISTORY_FILE}")
        if args.cooldown_days > 0:
            print(f"{cooldown_count} albums on cooldown (displayed within last {args.cooldown_days} days).")
        else:
            print("Cooldown disabled.")

    if args.output:
        Path(args.output).write_text(json.dumps(picks, indent=2))
        print(f"Picks also written to {args.output}")


if __name__ == "__main__":
    main()
