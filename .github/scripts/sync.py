#!/usr/bin/env python3
"""
One-way issue and comment mirror: source repo -> this (dummy) repo.

Designed to run as a GitHub Action on a schedule or via manual trigger.
Reads public issues from SOURCE_REPO, creates mirrors in this repo,
and syncs comments (source -> dummy only, never the reverse).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

SOURCE_REPO = os.environ["SOURCE_REPO"]
DUMMY_REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
FORCE_FULL_SYNC = os.environ.get("FORCE_FULL_SYNC", "false").lower() == "true"
STATE_FILE = "state.json"
CUTOFF_DATE = "2023-01-01T00:00:00Z"

ISSUE_MARKER = "<!-- source-issue-id: {repo} | {number} -->"
COMMENT_MARKER = "<!-- source-comment-id: {comment_id} -->"

def _sanitize_body(text):
    """Neutralize GitHub URLs and owner/repo#N references so GitHub won't
    create back-references on the source repo.

    Wraps them in backticks with a space before '#' so GitHub renders them
    as inline code instead of clickable cross-references.
    """
    import re
    escaped = re.escape(SOURCE_REPO)
    # Full URLs first (including optional #fragment at the end)
    text = re.sub(
        r'https?://github\.com/' + escaped + r'/(?:issues|pull)/(\d+)(?:#\S*)?',
        lambda m: f'`{SOURCE_REPO} #{m.group(1)}`',
        text,
    )
    # Short references:  owner/repo#123
    text = re.sub(
        escaped + r'#(\d+)\b',
        lambda m: f'`{SOURCE_REPO} #{m.group(1)}`',
        text,
    )
    # Strip @mentions so mirrored text doesn't ping users
    text = re.sub(r'(?<!\w)@(\w+)', r'`@\1`', text)
    return text


def _sanitize_title(text):
    """Sanitize an issue title — backticks don't suppress auto-linking in
    titles, so we replace the pattern with plain-text wording instead."""
    import re
    escaped = re.escape(SOURCE_REPO)
    text = re.sub(
        r'https?://github\.com/' + escaped + r'/(?:issues|pull)/(\d+)(?:#\S*)?',
        lambda m: f'{SOURCE_REPO} issue {m.group(1)}',
        text,
    )
    text = re.sub(
        escaped + r'#(\d+)\b',
        lambda m: f'{SOURCE_REPO} issue {m.group(1)}',
        text,
    )
    return text


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def api(method, url, data=None, token=None):
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "issue-mirror-bot",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        print(f"  API {method} {url} -> {exc.code}: {detail}", file=sys.stderr)
        raise


def api_paginated(url, token=None):
    """GET all pages and return the concatenated list."""
    results = []
    page = 1
    while True:
        sep = "&" if "?" in url else "?"
        page_url = f"{url}{sep}per_page=100&page={page}"
        batch = api("GET", page_url, token=token)
        if not isinstance(batch, list):
            break
        results.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return results


# ---------------------------------------------------------------------------
# State management — stored in repo as state.json, committed after each run
# ---------------------------------------------------------------------------

def load_state():
    if FORCE_FULL_SYNC:
        return {"last_poll": CUTOFF_DATE, "issue_map": {}}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
            state.setdefault("issue_map", {})
            return state
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_poll": CUTOFF_DATE, "issue_map": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Issue sync
# ---------------------------------------------------------------------------

def get_source_issues(since):
    """Fetch issues (not PRs) from source repo updated since *since*.

    Issues created before CUTOFF_DATE are always ignored.
    """
    url = (
        f"https://api.github.com/repos/{SOURCE_REPO}/issues"
        f"?since={since}&state=all&sort=updated&direction=asc"
    )
    issues = api_paginated(url)
    return [
        i for i in issues
        if "pull_request" not in i and i["created_at"] >= CUTOFF_DATE
    ]


def ensure_label():
    """Create the 'mirrored' label if it doesn't exist yet."""
    try:
        api(
            "POST",
            f"https://api.github.com/repos/{DUMMY_REPO}/labels",
            data={"name": "mirrored", "color": "0075ca",
                  "description": "Mirrored from external repo"},
            token=TOKEN,
        )
        print("Created 'mirrored' label")
    except urllib.error.HTTPError:
        pass


def create_mirror_issue(source_issue):
    src_num = source_issue["number"]
    title = _sanitize_title(source_issue["title"])
    body = _sanitize_body(source_issue.get("body") or "")
    marker = ISSUE_MARKER.format(repo=SOURCE_REPO, number=src_num)

    mirror_body = (
        f"**Mirrored from:** `{SOURCE_REPO} #{src_num}`\n\n"
        f"{marker}\n\n---\n\n{body}"
    )
    result = api(
        "POST",
        f"https://api.github.com/repos/{DUMMY_REPO}/issues",
        data={"title": f"[Mirror] {title}", "body": mirror_body,
              "labels": ["mirrored"]},
        token=TOKEN,
    )
    print(f"  Created mirror #{result['number']}")
    return result["number"]


# ---------------------------------------------------------------------------
# State sync (open/closed)
# ---------------------------------------------------------------------------

def sync_state(source_issue, mirror_number):
    """Close or reopen the mirror to match the source issue's state."""
    source_state = source_issue["state"]
    mirror = api(
        "GET",
        f"https://api.github.com/repos/{DUMMY_REPO}/issues/{mirror_number}",
        token=TOKEN,
    )
    if mirror["state"] != source_state:
        api(
            "PATCH",
            f"https://api.github.com/repos/{DUMMY_REPO}/issues/{mirror_number}",
            data={"state": source_state},
            token=TOKEN,
        )
        print(f"  Updated mirror #{mirror_number} state -> {source_state}")


# ---------------------------------------------------------------------------
# Comment sync (one-way: source -> dummy)
# ---------------------------------------------------------------------------

def sync_comments(source_number, mirror_number, synced_comment_ids):
    """Mirror new comments from source issue to the dummy issue.

    Returns the updated set of synced comment IDs.
    """
    src_comments = api_paginated(
        f"https://api.github.com/repos/{SOURCE_REPO}/issues/{source_number}/comments"
    )

    new_ids = set(synced_comment_ids)

    for sc in src_comments:
        cid = str(sc["id"])
        if cid in new_ids:
            continue

        author = sc["user"]["login"]
        created = sc["created_at"]
        body = _sanitize_body(sc.get("body") or "")
        marker = COMMENT_MARKER.format(comment_id=cid)

        mirror_body = (
            f"**{author}** commented on {created}:\n\n"
            f"{marker}\n\n---\n\n{body}"
        )

        api(
            "POST",
            f"https://api.github.com/repos/{DUMMY_REPO}/issues/{mirror_number}/comments",
            data={"body": mirror_body},
            token=TOKEN,
        )
        new_ids.add(cid)
        print(f"    Mirrored comment {cid}")
        time.sleep(1)

    return sorted(new_ids)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    since = state["last_poll"]
    issue_map = state["issue_map"]  # { "src_number": { "mirror": N, "comments": [...] } }
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    print(f"Source repo : {SOURCE_REPO}")
    print(f"Dummy repo  : {DUMMY_REPO}")
    print(f"Since       : {since}")
    print(f"Full sync   : {FORCE_FULL_SYNC}")
    print()

    ensure_label()

    source_issues = get_source_issues(since)
    print(f"Found {len(source_issues)} issue(s) updated since {since}\n")

    for issue in source_issues:
        src_num = str(issue["number"])
        print(f"Source #{src_num}: {issue['title']}")

        entry = issue_map.get(src_num)

        if entry is None:
            mirror_num = create_mirror_issue(issue)
            entry = {"mirror": mirror_num, "comments": []}
            issue_map[src_num] = entry
            time.sleep(1)
        else:
            print(f"  Already mirrored as #{entry['mirror']}")

        sync_state(issue, entry["mirror"])

        synced = sync_comments(
            int(src_num), entry["mirror"], entry.get("comments", [])
        )
        entry["comments"] = synced

    state["last_poll"] = now
    state["issue_map"] = issue_map
    save_state(state)
    print(f"\nDone. State saved — last_poll = {now}")


if __name__ == "__main__":
    main()
