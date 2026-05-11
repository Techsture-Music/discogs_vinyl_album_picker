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

import argparse
import json
import os
import random
import time
from pathlib import Path

import requests

DISCOGS_API = "https://api.discogs.com"
HISTORY_FILE = Path(__file__).resolve().parent / "discogs_wall_history.json"
USER_AGENT = "VinylWallPicker/1.0"
DEFAULT_COUNT = 24
DEFAULT_FORMATS = ["Vinyl"]              # case-insensitive; [] = no filter (any format)
DEFAULT_EXCLUDE_FORMATS = ["Box Set"]    # always-removed formats; won't fit on shallow shelves
DEFAULT_FIELD_NAME = "Wall Display"      # Discogs custom field controlling inclusion
DEFAULT_FIELD_VALUE = "Yes"              # the value required to be included
COOLDOWN_PICKS = 3                       # how many recent picks are "on cooldown"
HISTORY_KEEP = 10                        # keep this many past picks in the file


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
            artists = ", ".join(a["name"] for a in info.get("artists", []))
            formats_raw = info.get("formats", [])
            releases.append({
                "id": r["id"],
                "artist": artists,
                "title": info.get("title", ""),
                "year": info.get("year", 0),
                "formats": [f.get("name", "") for f in formats_raw],
                "format_descriptions": [d for f in formats_raw for d in f.get("descriptions", [])],
                "notes": {n["field_id"]: n.get("value", "") for n in r.get("notes", [])},
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
    target = required_value.strip().lower()
    return [r for r in releases if r["notes"].get(field_id, "").strip().lower() == target]


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


def album_key(release: dict) -> tuple[str, str]:
    """Identity for an album regardless of pressing/color variant."""
    return (normalize_artist(release["artist"]).lower(), release["title"].strip().lower())


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"picks": []}


def save_history(history: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def recently_displayed(history: dict, cooldown: int = COOLDOWN_PICKS) -> set[int]:
    recent = set()
    for picks in history["picks"][-cooldown:]:
        recent.update(picks)
    return recent


def pick_albums(releases: list[dict], count: int, history: dict) -> list[dict]:
    """Pick `count` releases, preferring ones not in recent rotation.

    Two releases with the same (artist, title) — e.g. black vs. colored pressings
    of the same album — count as one candidate. One pressing is picked at random
    for that album.
    """
    recent = recently_displayed(history)
    fresh = [r for r in releases if r["id"] not in recent]

    def sample_unique_albums(pool: list[dict], n: int) -> list[dict]:
        """Sample n releases such that no two share the same album_key."""
        by_album: dict[tuple[str, str], list[dict]] = {}
        for r in pool:
            by_album.setdefault(album_key(r), []).append(r)
        keys = list(by_album.keys())
        chosen = random.sample(keys, min(n, len(keys)))
        return [random.choice(by_album[k]) for k in chosen]

    picks = sample_unique_albums(fresh, count)

    if len(picks) < count:
        # Not enough unique fresh albums — fill from the cooldown pool,
        # avoiding albums already in `picks`.
        already = {album_key(r) for r in picks}
        leftover = [r for r in releases if album_key(r) not in already]
        picks.extend(sample_unique_albums(leftover, count - len(picks)))
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

    print(f"Fetching collection for {args.username}...")
    releases = fetch_collection(args.username, args.token)
    print(f"Found {len(releases)} releases in collection.")

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
    print()

    if len(releases) < args.count:
        print(f"Only {len(releases)} releases in collection — picking all of them.")
        args.count = len(releases)

    history = load_history() if not args.no_history else {"picks": []}
    picks = pick_albums(releases, args.count, history)
    picks.sort(key=lambda r: normalize_artist(r["artist"]).lower())

    print(f"=== {len(picks)} albums for the wall ===\n")
    for i, r in enumerate(picks, 1):
        year = f" ({r['year']})" if r["year"] else ""
        fmt = f" [{', '.join(r['formats'])}]" if r["formats"] else ""
        print(f"{i:2}. {r['artist']} — {r['title']}{year}{fmt}")
        print(f"    {r['url']}")

    if not args.no_history:
        history["picks"].append([r["id"] for r in picks])
        history["picks"] = history["picks"][-HISTORY_KEEP:]
        save_history(history)
        cooldown_count = len(recently_displayed(history))
        print(f"\nHistory saved to {HISTORY_FILE}")
        print(f"{cooldown_count} albums now on cooldown for the next pick.")

    if args.output:
        Path(args.output).write_text(json.dumps(picks, indent=2))
        print(f"Picks also written to {args.output}")


if __name__ == "__main__":
    main()
