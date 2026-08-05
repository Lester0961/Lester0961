#!/usr/bin/env python3
"""Refresh Lester0961's terminal profile SVG from public GitHub data.

The workflow supplies GH_TOKEN.  Local runs may omit it; public REST data is
still used where available, while cached values are retained on API failures.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
USERNAME = os.environ.get("GH_USERNAME", "Lester0961")
TOKEN = os.environ.get("GH_TOKEN", "")
CACHE_PATH = ROOT / "cache" / "github-stats.json"
TEMPLATES = {
    "dark": ROOT / "templates" / "profile-dark-template.svg",
    "light": ROOT / "templates" / "profile-light-template.svg",
}
OUTPUTS = {theme: ROOT / "assets" / f"profile-{theme}.svg" for theme in TEMPLATES}
SOURCE_EXTENSIONS = {
    ".c", ".cpp", ".cs", ".css", ".dart", ".html", ".java", ".js", ".jsx",
    ".php", ".py", ".sql", ".ts", ".tsx",
}
EXCLUDED_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
EXCLUDED_PREFIXES = ("dist/", "build/", "coverage/", "vendor/", "node_modules/")


class GitHubClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "User-Agent": "Lester0961-profile-readme",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def paginated(self, url: str, *, params: dict[str, Any] | None = None) -> list[Any]:
        all_items: list[Any] = []
        query = {"per_page": 100, **(params or {})}
        while url:
            response = self.session.get(url, params=query, timeout=30)
            response.raise_for_status()
            all_items.extend(response.json())
            url = response.links.get("next", {}).get("url")
            query = None
        return all_items

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not TOKEN:
            return {}
        response = self.session.post(
            "https://api.github.com/graphql",
            json={"query": query, "variables": variables}, timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise requests.HTTPError(payload["errors"])
        return payload["data"]


def load_cache() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {"version": 1, "repositories": {}}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("Warning: cache is invalid; starting a fresh cache.", file=sys.stderr)
        return {"version": 1, "repositories": {}}


def save_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def is_source_file(filename: str) -> bool:
    normalized = filename.lstrip("/")
    return (
        normalized not in EXCLUDED_NAMES
        and not normalized.startswith(EXCLUDED_PREFIXES)
        and Path(normalized).suffix.lower() in SOURCE_EXTENSIONS
    )


def is_eligible_commit(commit: dict[str, Any]) -> bool:
    author = commit.get("author") or {}
    message = (commit.get("commit") or {}).get("message", "").lower()
    login = str(author.get("login", "")).lower()
    if login != USERNAME.lower() or author.get("type") == "Bot":
        return False
    if len(commit.get("parents", [])) > 1:
        return False
    return not any(marker in message for marker in ("[skip profile stats]", "[profile update]", "dependabot"))


def repository_list(client: GitHubClient) -> tuple[list[dict[str, Any]], int, int, int]:
    user = client.get(f"https://api.github.com/users/{USERNAME}")
    owned = client.paginated(
        f"https://api.github.com/users/{USERNAME}/repos",
        params={"type": "owner", "sort": "full_name", "direction": "asc"},
    )
    repositories = [repo for repo in owned if not repo.get("archived") and not repo.get("fork")]
    contributed_count = 0
    contributions = 0
    try:
        data = client.graphql(
            """
            query($login: String!, $from: DateTime!, $to: DateTime!) {
              user(login: $login) {
                repositoriesContributedTo(first: 100, contributionTypes: [COMMIT], includeUserRepositories: false) {
                  totalCount
                  nodes { nameWithOwner isFork defaultBranchRef { name } }
                }
                contributionsCollection(from: $from, to: $to) {
                  contributionCalendar { totalContributions }
                }
              }
            }
            """,
            {
                "login": USERNAME,
                "from": f"{datetime.now(UTC).year}-01-01T00:00:00Z",
                "to": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        account = data.get("user") or {}
        contributed = account.get("repositoriesContributedTo") or {}
        contributed_count = int(contributed.get("totalCount", 0))
        contributions = int(((account.get("contributionsCollection") or {}).get("contributionCalendar") or {}).get("totalContributions", 0))
        for repo in contributed.get("nodes", []):
            if not repo.get("isFork") and repo.get("defaultBranchRef"):
                owner, name = repo["nameWithOwner"].split("/", 1)
                repositories.append({
                    "full_name": repo["nameWithOwner"], "owner": {"login": owner}, "name": name,
                    "default_branch": repo["defaultBranchRef"]["name"], "stargazers_count": 0,
                })
    except requests.RequestException as error:
        print(f"Warning: GraphQL statistics unavailable: {error}", file=sys.stderr)
    return repositories, len(repositories), contributed_count, contributions, int(user.get("followers", 0))


def commit_detail(client: GitHubClient, repository: str, sha: str) -> dict[str, Any] | None:
    try:
        return client.get(f"https://api.github.com/repos/{repository}/commits/{sha}")
    except requests.RequestException as error:
        print(f"Warning: unable to read {repository}@{sha[:7]}: {error}", file=sys.stderr)
        return None


def collect_commits(client: GitHubClient, cache: dict[str, Any], repositories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    all_commits: dict[str, dict[str, Any]] = {}
    repository_cache = cache.setdefault("repositories", {})
    for repo in repositories:
        full_name = repo["full_name"]
        if full_name.lower() == f"{USERNAME}/{USERNAME}".lower():
            continue  # exclude the profile repository and its automation commits
        branch = repo.get("default_branch")
        if not branch:
            continue
        state = repository_cache.setdefault(full_name, {"default_branch": branch, "commits": {}})
        try:
            shas = client.paginated(
                f"https://api.github.com/repos/{full_name}/commits", params={"sha": branch}
            )
        except requests.RequestException as error:
            print(f"Warning: unable to list commits for {full_name}: {error}", file=sys.stderr)
            continue
        state["default_branch"] = branch
        state["latest_commit"] = shas[0]["sha"] if shas else None
        records = state.setdefault("commits", {})
        for summary in shas:
            sha = summary["sha"]
            record = records.get(sha)
            if record is None:
                detail = commit_detail(client, full_name, sha)
                if not detail or not is_eligible_commit(detail):
                    records[sha] = {"included": False}
                    continue
                files = [file for file in detail.get("files", []) if is_source_file(file.get("filename", ""))]
                if not files:
                    records[sha] = {"included": False}
                    continue
                record = {
                    "included": True,
                    "date": detail["commit"]["author"]["date"],
                    "additions": sum(int(file.get("additions", 0)) for file in files),
                    "deletions": sum(int(file.get("deletions", 0)) for file in files),
                    "author_github_id": (detail.get("author") or {}).get("id"),
                }
                records[sha] = record
            if record.get("included"):
                all_commits.setdefault(sha, {"sha": sha, "repository": full_name, **record})
    return sorted(all_commits.values(), key=lambda item: (item.get("date", ""), item["sha"]))


def range_rows(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for number, commit in enumerate(commits, start=1):
        buckets[(number - 1) // 100].append(commit)
    rows = []
    for index in sorted(buckets):
        bucket = buckets[index]
        start = index * 100 + 1
        end = start + len(bucket) - 1
        added = sum(int(item["additions"]) for item in bucket)
        deleted = sum(int(item["deletions"]) for item in bucket)
        rows.append({"label": f"Commits {start:03d}-{end:03d}", "added": added, "deleted": deleted, "net": added - deleted})
    return rows


def format_signed(value: int) -> str:
    return f"{value:+,}" if value else "0"


def replace_svg(template: Path, output: Path, values: dict[str, str], rows: list[dict[str, Any]]) -> None:
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(template), parser)
    for element_id, value in values.items():
        elements = tree.xpath(f"//*[@id='{element_id}']")
        if not elements:
            raise ValueError(f"Missing SVG element id: {element_id}")
        elements[0].text = value
    row_group = tree.xpath("//*[@id='commit-range-rows']")
    if len(row_group) != 1:
        raise ValueError("Missing SVG commit range group")
    group = row_group[0]
    for child in list(group):
        group.remove(child)
    namespace = "{http://www.w3.org/2000/svg}"
    for index, row in enumerate(rows):
        text = etree.SubElement(group, f"{namespace}text", {"x": "555", "y": str(628 + index * 20), "class": "copy"})
        text.text = f"{row['label']}:  +{row['added']:,}  -{row['deleted']:,}  net {format_signed(row['net'])}"

    # The terminal grows only when there are more than five ranges.  Every
    # 100-commit sequence remains visible instead of discarding older rows.
    overflow = max(0, len(rows) - 5) * 20
    root = tree.getroot()
    height = 884 + overflow
    root.set("height", str(height))
    root.set("viewBox", f"0 0 1200 {height}")
    terminal = tree.xpath("//*[local-name()='rect' and @class='terminal']")
    if terminal:
        terminal[0].set("height", str(height - 2))
    divider = tree.xpath("//*[@id='side-divider']")
    if len(divider) != 1:
        raise ValueError("Missing SVG side divider")
    divider[0].set("y2", str(height - 34))
    for element_id, base_y in {
        "lines-heading": 740, "lines-values": 762, "contact-heading": 794,
        "contact-line-1": 816, "contact-line-2": 836, "last-update-line": 864,
    }.items():
        element = tree.xpath(f"//*[@id='{element_id}']")
        if len(element) != 1:
            raise ValueError(f"Missing SVG layout element id: {element_id}")
        element[0].set("y", str(base_y + overflow))
    output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(output), encoding="utf-8", xml_declaration=True, pretty_print=True)


def main() -> None:
    client = GitHubClient(TOKEN)
    cache = load_cache()
    try:
        repositories, repo_count, contributed_count, contributions, followers = repository_list(client)
    except requests.RequestException as error:
        print(f"GitHub data could not be fetched: {error}", file=sys.stderr)
        repositories, repo_count, contributed_count, contributions, followers = [], 0, 0, 0, 0
    commits = collect_commits(client, cache, repositories)
    added = sum(int(item["additions"]) for item in commits)
    deleted = sum(int(item["deletions"]) for item in commits)
    stars = sum(int(repo.get("stargazers_count", 0)) for repo in repositories if repo.get("owner", {}).get("login", "").lower() == USERNAME.lower())
    rows = range_rows(commits)
    values = {
        "repo-count": f"{repo_count:,}", "contributed-repo-count": f"{contributed_count:,}",
        "commit-count": f"{len(commits):,}", "star-count": f"{stars:,}",
        "follower-count": f"{followers:,}", "contribution-count": f"{contributions:,}",
        "lines-added": f"{added:,}", "lines-deleted": f"{deleted:,}", "lines-net": format_signed(added - deleted),
        "last-update": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
    }
    cache.update({
        "version": 1, "generated_at": datetime.now(UTC).isoformat(), "username": USERNAME,
        "summary": {"repositories": repo_count, "contributed_repositories": contributed_count, "commits": len(commits), "added": added, "deleted": deleted},
    })
    for theme, template in TEMPLATES.items():
        replace_svg(template, OUTPUTS[theme], values, rows)
    save_cache(cache)
    print(f"Updated profile SVGs from {len(commits):,} eligible commits.")


if __name__ == "__main__":
    main()
