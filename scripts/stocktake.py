#!/usr/bin/env python3
"""Issue and pull-request plumbing for the harness-stocktake Action.

Three commands, one per step of action.yml:

  audit-sync     after an audit run: put the report into the tracked issue
  apply-prepare  on an issue edit: read the ticked boxes, rebuild the run directory
  apply-publish  after an apply run: push the branch and open or update the one PR

`mark-failed` records a failed run in the tracked issue.

The tracked issue is the only state the Action keeps. Its body holds the visible checklist and,
in an HTML comment, the report data (gzip + base64) the apply run needs. Standard library only,
so the Action installs nothing.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import gzip
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ISSUE_TITLE = "Harness stocktake"
BODY_MARKER = "<!-- harness-stocktake:v1 -->"
DATA_OPEN = "<!-- harness-stocktake:data"
DATA_CLOSE = "-->"
APPLY_LABEL = "Apply the ticked items"
APPLY_MARKER = "<!-- apply -->"
MAX_BODY = 65000  # GitHub caps an issue body at 65536 characters
MAX_VISIBLE = 40000  # the checklist's share; the rest is left for the data blob
PR_BRANCH = "claude/stocktake"
REPORT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WRITE_PERMISSIONS = {"admin", "maintain", "write"}
REPORT_ROOT = Path(".notes/stocktake")
OUTCOMES_FILE = "outcomes.json"  # the apply run writes it next to report.md

GROUPS = ["Retire", "Merge / demote", "Improve / update", "Other", "Keep"]
_GROUP_PREFIXES = [
    ("Retire", ("retire", "dissolve")),
    ("Merge / demote", ("merge", "demote")),
    ("Improve / update", ("improve", "update", "append", "revise", "create")),
    ("Keep", ("keep", "out of scope", "already covered", "too specific")),
]

README_URL = "https://github.com/shimo4228/harness-stocktake-action#what-it-cant-change"

# Claude Code's protected paths (https://code.claude.com/docs/en/permission-modes#protected-paths).
# In a `-p` run its Edit and Write tools are denied there whatever --allowedTools allows, so an
# item that edits one is applied by hand.
PROTECTED_DIRS = {".git", ".vscode", ".idea", ".husky", ".cargo", ".devcontainer", ".yarn", ".mvn", ".claude"}
PROTECTED_EXEMPT = (".claude/worktrees/", ".claude/agent-memory/")
PROTECTED_FILES = {
    ".gitconfig",
    ".gitmodules",
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".bash_aliases",
    ".bash_logout",
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".zlogin",
    ".zlogout",
    ".profile",
    ".envrc",
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    ".pnp.cjs",
    ".pnp.loader.mjs",
    ".pnpmfile.cjs",
    "bunfig.toml",
    ".bunfig.toml",
    ".bazelrc",
    ".bazelversion",
    ".bazeliskrc",
    ".pre-commit-config.yaml",
    "lefthook.yml",
    "lefthook.yaml",
    ".lefthook.yml",
    ".lefthook.yaml",
    "gradle-wrapper.properties",
    "maven-wrapper.properties",
    ".devcontainer.json",
    ".ripgreprc",
    "pyrightconfig.json",
    ".mcp.json",
    ".claude.json",
}
# Config files whose items edit entries in them (a hook, a permission, an MCP server), so even a
# Retire is an edit. Matched at any depth: a monorepo package can carry its own .claude/.
CONFIG_SUFFIXES = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json", ".claude.json")

CHECKBOX = re.compile(r"^- \[(?P<mark>[ xX])\] `(?P<id>[^`]+)`")
APPLY_LINE = re.compile(r"^- \[(?P<mark>[ xX])\] \*\*" + re.escape(APPLY_LABEL) + r"\*\*")

Item = dict[str, Any]


# --- GitHub API -------------------------------------------------------------------------------


class GitHubError(RuntimeError):
    pass


class GitHub:
    """The few REST calls the Action makes. Tests replace it with a fake of the same shape."""

    def __init__(self, token: str, repo: str, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.repo = repo
        self.api_url = api_url.rstrip("/")

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "harness-stocktake-action",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:500]
            raise GitHubError(f"{method} {path}: HTTP {err.code} {detail}") from err
        return json.loads(raw) if raw else None

    def find_issue(self) -> dict[str, Any] | None:
        """The oldest open issue the Action itself opened; issues people open are never touched."""
        page = 1
        while True:
            batch = self.request(
                "GET",
                f"/repos/{self.repo}/issues?state=open&sort=created&direction=asc&per_page=100&page={page}",
            )
            for issue in batch:
                if "pull_request" not in issue and is_tracked_issue(issue):
                    return issue
            if len(batch) < 100:
                return None
            page += 1

    def get_issue(self, number: int) -> dict[str, Any]:
        return self.request("GET", f"/repos/{self.repo}/issues/{number}")

    def permission(self, login: str) -> str:
        return self.request("GET", f"/repos/{self.repo}/collaborators/{login}/permission")["permission"]

    def create_issue(self, body: str) -> dict[str, Any]:
        return self.request("POST", f"/repos/{self.repo}/issues", {"title": ISSUE_TITLE, "body": body})

    def update_issue(self, number: int, body: str) -> dict[str, Any]:
        return self.request("PATCH", f"/repos/{self.repo}/issues/{number}", {"body": body})

    def default_branch(self) -> str:
        return self.request("GET", f"/repos/{self.repo}")["default_branch"]

    def find_pr(self, branch: str) -> dict[str, Any] | None:
        owner = self.repo.split("/")[0]
        prs = self.request("GET", f"/repos/{self.repo}/pulls?state=open&head={owner}:{branch}")
        return prs[0] if prs else None

    def create_pr(self, branch: str, base: str, title: str, body: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/repos/{self.repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )

    def update_pr(self, number: int, title: str, body: str) -> dict[str, Any]:
        return self.request("PATCH", f"/repos/{self.repo}/pulls/{number}", {"title": title, "body": body})


def is_tracked_issue(issue: dict[str, Any]) -> bool:
    """The tracked issue: our title, opened by a bot (the workflow token), carrying our marker."""
    return (
        issue.get("title") == ISSUE_TITLE
        and (issue.get("user") or {}).get("type") == "Bot"
        and BODY_MARKER in (issue.get("body") or "")
    )


# --- report data ------------------------------------------------------------------------------


def verdict_group(verdict: str) -> str:
    v = verdict.strip().lower()
    for group, prefixes in _GROUP_PREFIXES:
        if v.startswith(prefixes):
            return group
    return "Other"


def _tracked_by_git(path: Path) -> bool:
    """True when git tracks the file: a report committed to the repo, not one this run wrote."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "ls-files", "--", path.name],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    return bool(out.strip())


def _fresh_run(run: Path) -> bool:
    items = run / "items-all.json"
    return items.is_file() and not _tracked_by_git(items)


def find_run_dir(workspace: Path) -> Path | None:
    """The run directory this job's audit wrote: latest.json first, else the newest dated dir.

    A report committed to the repo is never taken, so a failed audit cannot pass an old report
    off as this run's.
    """
    root = workspace / REPORT_ROOT
    latest = root / "latest.json"
    if latest.is_file():
        try:
            report = Path(json.loads(latest.read_text())["report"])
        except (ValueError, KeyError, TypeError):
            report = None
        if report is not None:
            run = report.parent if report.is_absolute() else (workspace / report).parent
            if _fresh_run(run):
                return run
    if not root.is_dir():
        return None
    runs = sorted(p for p in root.iterdir() if p.is_dir() and _fresh_run(p))
    return runs[-1] if runs else None


def load_run(run: Path) -> dict[str, Any]:
    items = json.loads((run / "items-all.json").read_text())
    if not isinstance(items, list):
        raise TypeError(f"{run}/items-all.json is not a list")
    crosscheck = None
    if (run / "crosscheck.json").is_file():
        crosscheck = json.loads((run / "crosscheck.json").read_text())
    base = ""
    report = run / "report.md"
    if report.is_file():
        m = re.search(r"\*\*base:\*\* `([0-9a-f]+)`", report.read_text())
        base = m.group(1) if m else ""
    items.sort(key=lambda i: (i.get("order") is None, i.get("order") or 0, str(i.get("id"))))
    return {"report_date": run.name, "base": base, "items": items, "crosscheck": crosscheck}


def acting_items(items: list[Item]) -> list[Item]:
    """Items that change a file and have a row of their own."""
    return [
        i for i in items if verdict_group(str(i.get("verdict", ""))) != "Keep" and not i.get("folded_into")
    ]


def item_paths(item: Item) -> list[str]:
    """The repo-relative paths an item acts on: `targets` for a grouped item, else `target`."""
    raw = item.get("targets") or [item.get("target")]
    paths = []
    for p in raw if isinstance(raw, list) else [raw]:
        s = str(p or "").strip()
        while s.startswith("./"):
            s = s[2:]
        if s:
            paths.append(s)
    return paths


def is_protected(path: str) -> bool:
    """True for a path Claude Code's Edit and Write tools may not touch in a `-p` run."""
    if path.startswith(PROTECTED_EXEMPT):
        return False
    parts = path.split("/")
    if any(part in PROTECTED_DIRS for part in parts[:-1]) or path.startswith(".config/git/"):
        return True
    return parts[-1] in PROTECTED_FILES or parts[-1] in PROTECTED_DIRS


def _is_config(path: str) -> bool:
    return any(path == c or path.endswith("/" + c) for c in CONFIG_SUFFIXES)


def needs_hand(item: Item) -> bool:
    """An item the Action cannot apply: it edits a protected path.

    A Retire of a protected file is a `git rm`, which Claude Code allows, unless the file is a
    config whose entries the item edits (`.claude/settings.json`, `.mcp.json`).
    """
    paths = item_paths(item)
    if any(_is_config(p) for p in paths):
        return True
    return verdict_group(str(item.get("verdict", ""))) != "Retire" and any(map(is_protected, paths))


def _matches(path: str, changed: set[str]) -> set[str]:
    """The changed files under one item path: the path itself, files below it, or glob matches."""
    if any(c in path for c in "*?["):
        return {c for c in changed if fnmatch.fnmatchcase(c, path)}
    prefix = path.rstrip("/") + "/"
    return {c for c in changed if c == path or c.startswith(prefix)}


def touches(item: Item, changed: set[str]) -> set[str]:
    """The changed files that belong to an item."""
    return set().union(*(_matches(t, changed) for t in item_paths(item)))


def item_status(approved: list[Item], changed: set[str], outcomes: dict[str, Item]) -> dict[str, Item]:
    """applied / pending per approved item, from what the branch changed.

    The branch decides. An item is applied only when the branch changes every one of its paths;
    an item that edits a path Claude Code cannot edit here also needs the apply run to record it
    as applied, since its protected part can be missing while the rest landed. outcomes.json, the
    apply run's own record, can move an item to pending and add its note and diff, but it cannot
    make an unchanged file applied.
    """
    hits = {str(i["id"]): touches(i, changed) for i in approved}
    owners: dict[str, list[str]] = {}
    for id_, files in hits.items():
        for f in files:
            owners.setdefault(f, []).append(id_)
    status: dict[str, Item] = {}
    for i in approved:
        id_ = str(i["id"])
        record = outcomes.get(id_) or {}
        claimed = str(record.get("outcome", "")).strip().lower()
        missing = [t for t in item_paths(i) if not _matches(t, changed)]
        applied = (
            bool(hits[id_])
            and not missing
            and claimed not in ("pending", "skipped")
            and (claimed in ("applied", "reevaluated") or not needs_hand(i))
        )
        entry: Item = {"status": "applied" if applied else "pending"}
        for key in ("note", "diff"):  # whole: pr_body decides what fits, and says when it doesn't
            if record.get(key):
                entry[key] = str(record[key])
        if not applied and hits[id_]:
            entry["partial"] = sorted(hits[id_])
            if "note" not in entry:
                left = ", ".join(missing) if missing else "a path Claude Code cannot edit in the Action"
                entry["note"] = (
                    f"Only part of it is on this branch ({', '.join(sorted(hits[id_]))}); still to do: "
                    f"{left}. Finish it by hand."
                )
        if applied and claimed != "applied":
            # The run did not record this item as applied, yet its file also belongs to another one:
            # the change on the branch may be the other item's alone.
            shared = sorted({o for f in hits[id_] for o in owners[f]} - {id_})
            if shared:
                entry["shared"] = shared
        status[id_] = entry
    return status


def load_outcomes(path: Path) -> dict[str, Item]:
    """outcomes.json from the apply run: {id: {outcome, note, diff}}. Anything else reads as empty."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if isinstance(raw, list):
        raw = {str(r.get("id")): r for r in raw if isinstance(r, dict) and r.get("id")}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Item] = {}
    for k, v in raw.items():
        if isinstance(v, str):
            v = {"outcome": v}
        if isinstance(v, dict):
            out[str(k)] = v
    return out


def item_key(item: Item) -> str:
    """Carries a decision from one report to the next: ids are stable only within a run."""
    return f"{item.get('source')}|{item.get('target')}|{item.get('verdict')}"


def encode_data(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    text = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
    return "\n".join(text[i : i + 76] for i in range(0, len(text), 76))


def decode_data(body: str) -> dict[str, Any] | None:
    start = body.find(DATA_OPEN)
    if start < 0:
        return None
    end = body.find(DATA_CLOSE, start + len(DATA_OPEN))
    if end < 0:
        return None
    blob = "".join(body[start + len(DATA_OPEN) : end].split())
    try:
        return json.loads(gzip.decompress(base64.b64decode(blob)))
    except (ValueError, OSError):
        return None


def read_marks(body: str) -> tuple[dict[str, bool], bool]:
    """Ticked state per item id, and whether the apply box is ticked."""
    marks: dict[str, bool] = {}
    apply = False
    for line in body.splitlines():
        m = APPLY_LINE.match(line)
        if m and APPLY_MARKER in line:
            apply = m.group("mark") != " "
            continue
        m = CHECKBOX.match(line)
        if m:
            marks[m.group("id")] = m.group("mark") != " "
    return marks, apply


# --- issue body -------------------------------------------------------------------------------


def _neutralise(text: object) -> str:
    """Repo-derived text that cannot ping a user or team, or open or close an HTML comment."""
    s = str(text or "").replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    return re.sub(r"@(?=\w)", "@\u200b", s)


def _inline(text: object, limit: int = 240) -> str:
    """Repo-derived text on one line, unable to tick a box, ping a user or close a comment."""
    s = _neutralise(" ".join(str(text or "").split()))
    s = s.replace("[", "\\[").replace("`", "'")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _label(item: Item) -> str:
    """`id` **verdict** · `target`, as the checklist and the PR body show an item."""
    return (
        f"`{_inline(item['id'], 60)}` **{_inline(item['verdict'], 60)}** · "
        f"`{_inline(item.get('target'), 120)}`"
    )


def _first_sentence(text: object) -> str:
    s = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.。])(\s|$)", s)
    return m.group(1) if m else s


TAG_APPLIED = "✅ *applied*"
TAG_PENDING = "✋ **pending, apply by hand**"
TAG_HAND = "✋ *by hand*"


def _status_tag(item: Item, outcomes: dict[str, Item], ticked: bool) -> str:
    """Where an item stands: after an apply its result, before one whether it needs a hand edit.

    The result shows only while the box stays ticked: unticking an item drops it from the next apply.
    """
    status = (outcomes.get(str(item["id"])) or {}).get("status") if ticked else None
    if status == "applied":
        return f" · {TAG_APPLIED}"
    if status == "pending":
        return f" · {TAG_PENDING}"
    return f" · {TAG_HAND}" if needs_hand(item) else ""


def _kept_lines(keep: list[Item]) -> list[str]:
    """One line per kept path. Several sources may keep the same file; each is named once."""
    by_target: dict[str, dict[str, list[str]]] = {}
    for i in keep:
        verdicts = by_target.setdefault(_inline(i.get("target"), 120), {})
        sources = verdicts.setdefault(_inline(i.get("verdict"), 60), [])
        source = _inline(i.get("source"), 40)
        if source and source not in sources:
            sources.append(source)
    lines = []
    for target, verdicts in by_target.items():
        said = " · ".join(f"{v} ({', '.join(s)})" if s else v for v, s in verdicts.items())
        lines.append(f"- `{target}` — {said}")
    return lines


def render_body(data: dict[str, Any], ticked: dict[str, bool]) -> str:
    items = data.get("items", [])
    acting = acting_items(items)
    keep = [i for i in items if verdict_group(str(i.get("verdict", ""))) == "Keep"]
    run = data.get("last_run") or {}

    lines = [BODY_MARKER, ""]
    when = run.get("at", "—")
    result = run.get("result", "")
    link = f" · [run]({run['url']})" if run.get("url") else ""
    lines.append(f"**Last run:** {when} — {result}{link}")
    if data.get("report_date"):
        base = f" · base `{data['base'][:12]}`" if data.get("base") else ""
        lines.append(f"**Report:** {data['report_date']}{base} · plugin `{data.get('plugin_ref', '?')}`")
    if data.get("pr_url"):
        lines.append(f"**Pull request:** {data['pr_url']}")
    if data.get("note"):
        lines.append(f"**Note:** {_inline(data['note'], 400)}")
    lines += [
        "",
        (
            "> Scheduled runs stop after 60 days without activity in the repository. If the last run "
            "is more than a month old, start one by hand: Actions → *Harness stocktake* → *Run workflow*."
        ),
        "",
    ]

    if not data.get("report_date"):
        lines.append("No report yet.")
    elif not acting:
        lines.append(f"Nothing to change. {len(keep)} item(s) kept.")
    else:
        counts = " · ".join(
            f"{g} {n}"
            for g in GROUPS[:-1]
            if (n := sum(1 for i in acting if verdict_group(str(i["verdict"])) == g))
        )
        lines += [
            f"**{len(acting)} item(s) to change** ({counts}) · {len(keep)} kept",
            "",
            (
                "Tick the items you approve, then tick the last box. The run applies the ticked items "
                "on the `claude/stocktake` branch and opens one pull request (or updates the open one). "
                "Nothing reaches the default branch until you merge it."
            ),
            "",
        ]
        outcomes = data.get("outcomes") or {}
        shown = {k for k in outcomes if ticked.get(k)}
        if shown:
            lines += [
                (
                    f"After the last apply: {TAG_APPLIED} means the item's files changed on "
                    f"`claude/stocktake`; {TAG_PENDING} means they did not, and the pull request "
                    "says what to apply by hand."
                ),
                "",
            ]
        if any(needs_hand(i) and str(i["id"]) not in shown for i in acting):
            lines += [
                (
                    f"{TAG_HAND}: Claude Code cannot edit this path inside the Action "
                    f"([why]({README_URL})). Tick it and the pull request lists it as pending, with "
                    "the diff when the apply run recorded one, for you to apply."
                ),
                "",
            ]
        budget = MAX_VISIBLE - sum(len(ln) + 1 for ln in lines)
        hidden = 0
        for group in GROUPS[:-1]:
            rows = [i for i in acting if verdict_group(str(i["verdict"])) == group]
            if not rows:
                continue
            block = [f"### {group}", ""]
            for i in rows:
                mark = "x" if ticked.get(str(i["id"])) else " "
                n = len(i.get("targets") or [])
                extra = f" (+{n - 1} more)" if n > 1 else ""
                tag = _status_tag(i, outcomes, bool(ticked.get(str(i["id"]))))
                line = f"- [{mark}] {_label(i)}{extra}{tag} — {_inline(_first_sentence(i.get('reason')))}"
                if budget - len(line) - 1 < 0:
                    hidden += 1
                    continue
                budget -= len(line) + 1
                block.append(line)
            if len(block) > 2:
                lines += [*block, ""]
        if hidden:
            lines += [
                (
                    f"**{hidden} more item(s) do not fit in this issue.** They are in the run's summary; "
                    "apply the ones above first and the rest return in the next report."
                ),
                "",
            ]
        lines += ["---", "", f"- [ ] **{APPLY_LABEL}** {APPLY_MARKER}", ""]
    if keep:
        lines += ["<details><summary>Kept items</summary>", ""]
        lines += _kept_lines(keep)
        lines += ["", "</details>", ""]
    lines.append("The full report, with each item's evidence and change, is in the run's summary.")
    return _with_data(lines, data)


def _with_data(lines: list[str], data: dict[str, Any]) -> str:
    """Append the data blob, dropping detail until the body fits GitHub's limit."""
    visible = "\n".join(lines)
    candidates = [data]
    trimmed = dict(data, items=acting_items(data.get("items", [])))
    candidates.append(trimmed)
    candidates.append(
        dict(trimmed, items=[{k: v for k, v in i.items() if k != "evidence"} for i in trimmed["items"]])
    )
    for candidate in candidates:
        body = f"{visible}\n\n{DATA_OPEN}\n{encode_data(candidate)}\n{DATA_CLOSE}\n"
        if len(body) <= MAX_BODY:
            return body
    # Even the trimmed data does not fit: keep the issue updated, without apply data.
    note = "**Note:** the report is too large to store in this issue, so it cannot be applied from here."
    bare = {k: v for k, v in data.items() if k not in ("items", "crosscheck", "report_date")}
    return f"{visible}\n\n{note}\n\n{DATA_OPEN}\n{encode_data(bare)}\n{DATA_CLOSE}\n"


def stamp(run_url: str, result: str, now: dt.datetime | None = None) -> dict[str, str]:
    now = now or dt.datetime.now(dt.timezone.utc)
    return {"at": now.strftime("%Y-%m-%d %H:%M UTC"), "url": run_url, "result": result}


# --- commands ---------------------------------------------------------------------------------


def write_output(**values: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={v}" for k, v in values.items()]
    if path:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


def append_summary(path: str | None, text: str, limit: int = 900_000) -> None:
    if not path:
        return
    if len(text) > limit:
        text = text[:limit] + "\n\n… (truncated)\n"
    with open(path, "a") as fh:
        fh.write(text + "\n")


def cmd_audit_sync(args: argparse.Namespace, gh: GitHub) -> int:
    workspace = Path(args.workspace)
    run = find_run_dir(workspace)
    if run is None:
        print("::error::The audit wrote no report (.notes/stocktake/<date>/items-all.json).")
        if args.update_issue:
            record_failure(gh, args.run_url, "audit failed: no report was written")
        return 1
    data = load_run(run)
    data["plugin_ref"] = args.plugin_ref
    if (run / "report.md").is_file():
        append_summary(args.summary, (run / "report.md").read_text())

    acting = acting_items(data["items"])
    write_output(items=len(acting), run_dir=run)
    if not args.update_issue:
        print(f"Report: {len(acting)} item(s) to change. The tracked issue is left as it is.")
        return 0

    issue = gh.find_issue()
    previous = decode_data(issue["body"] or "") if issue else None
    carried: dict[str, bool] = {}
    if issue and previous:
        marks, _ = read_marks(issue["body"] or "")
        by_id = {str(i.get("id")): i for i in previous.get("items", [])}
        carried = {item_key(by_id[k]): v for k, v in marks.items() if k in by_id}
    ticked = {str(i["id"]): carried.get(item_key(i), False) for i in acting}

    data["last_run"] = stamp(args.run_url, f"report {data['report_date']}, {len(acting)} item(s) to change")
    pr = gh.find_pr(PR_BRANCH)
    if pr:
        data["pr_url"] = pr["html_url"]
    body = render_body(data, ticked)
    issue = gh.update_issue(issue["number"], body) if issue else gh.create_issue(body)
    print(f"Tracked issue: {issue.get('html_url', issue['number'])}")
    write_output(issue_number=issue["number"])
    return 0


def tracked_issue(gh: GitHub, number: int | None) -> dict[str, Any] | None:
    """The tracked issue: by number when the run already knows it, else by search."""
    return gh.get_issue(number) if number else gh.find_issue()


def record_failure(gh: GitHub, run_url: str, message: str, number: int | None = None) -> None:
    issue = tracked_issue(gh, number)
    data = (decode_data(issue["body"] or "") if issue else None) or {}
    marks = read_marks(issue["body"] or "")[0] if issue else {}
    data["last_run"] = stamp(run_url, message)
    body = render_body(data, marks)
    if issue:
        gh.update_issue(issue["number"], body)
    else:
        gh.create_issue(body)


def cmd_mark_failed(args: argparse.Namespace, gh: GitHub) -> int:
    record_failure(gh, args.run_url, args.message, args.issue_number)
    return 0


def report_markdown(data: dict[str, Any], ticked: dict[str, bool]) -> str:
    """report.md in the plugin's Markdown-report shape, with every decision filled in."""

    def cell(value: object) -> str:
        if isinstance(value, list):
            value = "; ".join(str(v) for v in value)
        return ("" if value is None else str(value)).replace("|", "\\|").replace("\n", " ").strip()

    lines = [
        f"# Harness stocktake {data['report_date']}",
        "",
        f"- **date:** {data['report_date']}",
        f"- **base:** `{data.get('base', '')}`",
        "- **approved:** through the tracked issue; every row's `decision` is filled in",
        "",
        "| order | id | verdict | target | reason | change | conflicts | decision |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i in acting_items(data["items"]):
        decision = "accept" if ticked.get(str(i["id"])) else "skip"
        lines.append(
            f"| {i.get('order', '')} | {i['id']} | {cell(i['verdict'])} | {cell(i.get('target'))} | "
            f"{cell(i.get('reason'))} | {cell(i.get('change'))} | {cell(i.get('conflicts')) or '—'} | "
            f"{decision} |"
        )
    return "\n".join(lines) + "\n"


def cmd_apply_prepare(args: argparse.Namespace, gh: GitHub) -> int:
    event = json.loads(Path(args.event_path).read_text())
    issue = event.get("issue") or {}
    if not is_tracked_issue(issue):
        print("Not the tracked issue (it must be the one this Action opened).")
        write_output(apply="false")
        return 0
    sender = (event.get("sender") or {}).get("login", "")
    if not sender or gh.permission(sender) not in WRITE_PERMISSIONS:
        print(f"::warning::{sender or 'An unknown user'} has no write access; edit ignored.")
        write_output(apply="false")
        return 0
    body = issue.get("body") or ""
    marks, apply = read_marks(body)
    if not apply:
        print("The apply box is not ticked.")
        write_output(apply="false")
        return 0
    data = decode_data(body)
    if not data or not REPORT_DATE.match(str(data.get("report_date", ""))):
        print("::error::The tracked issue holds no report data; run the workflow once first.")
        write_output(apply="false")
        return 1
    approved = [i for i in acting_items(data["items"]) if marks.get(str(i["id"]))]

    # Untick the apply box at once, so a second edit does not start a second apply.
    data.pop("note", None)
    data.pop("outcomes", None)
    if approved:
        data["last_run"] = stamp(args.run_url, f"applying {len(approved)} ticked item(s)")
    else:
        data["last_run"] = stamp(args.run_url, "nothing applied")
        data["note"] = "The apply box was ticked with no item ticked."
    gh.update_issue(issue["number"], render_body(data, marks))
    if not approved:
        print("No item is ticked; nothing to apply.")
        write_output(apply="false")
        return 0

    run = Path(args.workspace) / REPORT_ROOT / data["report_date"]
    run.mkdir(parents=True, exist_ok=True)
    (run / "items-all.json").write_text(json.dumps(data["items"], ensure_ascii=False, indent=2))
    if data.get("crosscheck") is not None:
        (run / "crosscheck.json").write_text(json.dumps(data["crosscheck"], ensure_ascii=False, indent=2))
    (run / "report.md").write_text(report_markdown(data, marks))
    (run / OUTCOMES_FILE).unlink(missing_ok=True)
    latest = {
        "report": str(REPORT_ROOT / data["report_date"] / "report.md"),
        "date": data["report_date"],
        "base": data.get("base", ""),
    }
    (run.parent / "latest.json").write_text(json.dumps(latest, indent=2))
    write_output(
        apply="true", run_date=data["report_date"], approved=len(approved), issue_number=issue["number"]
    )
    return 0


Git = Callable[..., str]


def run_git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def final_message(execution_file: str | None) -> str:
    """The last result text Claude wrote (claude-code-action's execution file)."""
    if not execution_file or not Path(execution_file).is_file():
        return ""
    try:
        messages = json.loads(Path(execution_file).read_text())
    except ValueError:
        return ""
    for message in reversed(messages if isinstance(messages, list) else []):
        if isinstance(message, dict) and message.get("type") == "result":
            return str(message.get("result") or "")
    return ""


def apply_branch(git: Git, base: str, run_date: str) -> tuple[str, str]:
    """The branch the apply run committed to, and its commits over the default branch.

    The plugin names it stocktake/<date>, and that date may be the day of the apply rather than
    the report's, so every local stocktake/* branch is a candidate: the report's date first, then
    the newest commit.
    """
    try:
        refs = git(
            "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads/stocktake/"
        ).split()
    except subprocess.CalledProcessError:
        refs = []
    preferred = f"stocktake/{run_date}"
    refs.sort(key=lambda r: r != preferred)  # stable: keeps newest-first among the rest
    for ref in refs:
        try:
            log = git("log", "--oneline", f"origin/{base}..{ref}")
        except subprocess.CalledProcessError:
            continue
        if log:
            return ref, log
    return "", ""


def changed_files(git: Git, base: str, ref: str) -> set[str]:
    """Paths that merging the branch adds, changes or deletes (renames as both old and new path).

    The net diff from where the branch left the default branch, so a file a commit changed and a
    later commit put back is not in it. Names come NUL-separated and unquoted, so a non-ASCII path
    reads as itself. The run's own notes under .notes/ are not part of any item.
    """
    try:
        out = git(
            "-c",
            "core.quotePath=false",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"origin/{base}...{ref}",
        )
    except subprocess.CalledProcessError as err:
        print(f"::warning::Could not list the branch's files ({err}); every item reads as pending.")
        return set()
    root = f"{REPORT_ROOT.parts[0]}/"
    return {n for n in out.split("\0") if n.strip() and not n.startswith(root)}


def _fence(text: str, lang: str = "") -> list[str]:
    """A code block whose fence is longer than any backtick run inside it."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{lang}", text.rstrip("\n"), fence]


_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _close_blocks(text: str) -> str:
    """Text cut at an arbitrary point, with any code fence and <details> it left open closed again."""
    fence = ""
    details = 0
    for line in text.splitlines():
        m = _FENCE_LINE.match(line)
        if fence:
            closes = m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
            if closes and not line[m.end() :].strip():
                fence = ""
            continue
        if m:
            fence = m.group(1)
            continue
        details += line.count("<details>") - line.count("</details>")
    tail = ""
    if fence:
        tail += f"\n{fence}"
    tail += "\n</details>" * max(details, 0)
    return f"{text}{tail}\n" if tail else text


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


CHANGE_FLOOR = 500  # a diff or an approved change this short is always shown whole


def _pending_line(item: Item) -> str:
    hand = " · Claude Code cannot edit this path in the Action" if needs_hand(item) else ""
    return f"- {_label(item)}{hand} — see [Pending items](#pending-items)"


def _drop_dead_links(body: str, pending: list[Item]) -> str:
    """After a cut, a pending item whose section is gone no longer links to it."""
    for i in pending:
        if f"#### {_label(i)}" not in body:
            line = _pending_line(i)
            cut = line.replace("see [Pending items](#pending-items)", "details: see the run's summary")
            body = body.replace(line, cut)
    return body


def pending_details(pending: list[Item], status: dict[str, Item]) -> str:
    """Each pending item's note, diff and approved change in full, for the run's summary.

    The PR body shortens or drops these to fit; the summary is where it sends the reader."""
    lines = ["## Pending items, in full", ""]
    for i in pending:
        entry = status[str(i["id"])]
        lines += [f"### {_label(i)}", ""]
        if entry.get("note"):
            lines += [_neutralise(str(entry["note"])), ""]
        if entry.get("diff"):
            lines += [*_fence(entry["diff"], "diff"), ""]
        lines += ["Approved change:", "", *_fence(str(i.get("change") or "(none)")), ""]
    return "\n".join(lines)


def pr_body(
    data: dict[str, Any],
    approved: list[Item],
    status: dict[str, Item],
    issue_number: int,
    run_url: str,
    branch: str,
    log: str,
    report: str,
    diff_limit: int = 20_000,
    report_limit: int = 40_000,
    note_limit: int = 20_000,
) -> str:
    """The PR body. The limits shorten it to fit; nothing in it points at a section they removed."""
    has_report = bool(report) and report_limit > 0
    elsewhere = (
        "see the completion report below (the full one is in the run's summary)"
        if has_report
        else "the run's summary holds the full completion report"
    )
    applied = [i for i in approved if status[str(i["id"])]["status"] == "applied"]
    pending = [i for i in approved if status[str(i["id"])]["status"] == "pending"]
    lines = [
        (
            f"Harness stocktake of {data.get('report_date')}: {len(approved)} approved item(s), "
            f"{len(applied)} applied on `{branch}`, {len(pending)} left for you to apply by hand."
        ),
        "",
        "**Merging this pull request is the approval.** Nothing reached the default branch before it.",
        "",
        f"Tracked issue: #{issue_number} · [run]({run_url})",
        "",
        "### Approved items",
        "",
        f"**Applied on this branch ({len(applied)})**: the item's files change in this pull request.",
        "",
    ]
    for i in applied:
        shared = status[str(i["id"])].get("shared")
        also = (
            f" — shares its file with {', '.join(f'`{_inline(s, 60)}`' for s in shared)}; check the diff"
            if shared
            else ""
        )
        lines.append(f"- {_label(i)}{also}")
    if not applied:
        lines.append("- none")
    lines += [
        "",
        (
            f"**Pending, apply by hand ({len(pending)})**: this pull request does not change the "
            "item's files. Merging it leaves them as they are."
        ),
        "",
    ]
    for i in pending:
        lines.append(_pending_line(i))
    if not pending:
        lines.append("- none")
    if pending:
        lines += ["", "### Pending items", ""]
        for i in pending:
            entry = status[str(i["id"])]
            lines += [f"#### {_label(i)}", ""]
            if entry.get("note"):
                lines += [_neutralise(_clip(entry["note"], note_limit)), ""]
            if entry.get("partial"):
                lines += [
                    f"Partly on this branch: {', '.join(f'`{_inline(f, 120)}`' for f in entry['partial'])}.",
                    "",
                ]
            # A code block shows the diff as written, so it is not neutralised: GitHub neither
            # mentions anyone nor opens a comment from inside one, and the author applies it by hand.
            change = str(i.get("change") or "(none)")
            if entry.get("diff") and len(entry["diff"]) <= max(diff_limit, CHANGE_FLOOR):
                lines += [*_fence(entry["diff"], "diff"), ""]
            elif entry.get("diff"):
                lines += [
                    (
                        f"The diff ({len(entry['diff'])} characters) is too long for this pull request; "
                        f"{elsewhere}."
                    ),
                    "",
                ]
            elif len(change) <= max(diff_limit, CHANGE_FLOOR):
                more = "; the completion report below may say more" if has_report else ""
                lines += [
                    f"The apply run recorded no diff for it. The approved change is below{more}.",
                    "",
                    *_fence(change),
                    "",
                ]
            else:
                lines += [
                    (
                        f"The apply run recorded no diff for it, and the approved change ({len(change)} "
                        f"characters) is too long for this pull request; {elsewhere}."
                    ),
                    "",
                ]
    if log:
        lines += ["", "### Commits", "", *_fence(log), ""]
    if has_report:
        cut = report[:report_limit]
        if len(report) > report_limit:
            cut = _close_blocks(cut) + "\n\n… (cut; the full report is in the run's summary)"
        lines += [
            "### Completion report",
            "",
            "<details><summary>From the apply run</summary>",
            "",
            _neutralise(cut),
            "",
            "</details>",
            "",
        ]
    return "\n".join(lines)


def cmd_apply_publish(args: argparse.Namespace, gh: GitHub, git: Git = run_git) -> int:
    issue = tracked_issue(gh, args.issue_number)
    if issue is None:
        print("::error::The tracked issue is gone.")
        return 1
    data = decode_data(issue["body"] or "") or {}
    marks, _ = read_marks(issue["body"] or "")
    base = args.default_branch or gh.default_branch()
    approved = [i for i in acting_items(data.get("items", [])) if marks.get(str(i["id"]))]
    outcomes = load_outcomes(Path(args.workspace) / REPORT_ROOT / args.run_date / OUTCOMES_FILE)
    local, log = apply_branch(git, base, args.run_date)
    changed = changed_files(git, base, local) if log else set()
    status = item_status(approved, changed, outcomes)
    data["outcomes"] = {k: {"status": v["status"]} for k, v in status.items()}
    pending_now = [i for i in approved if status[str(i["id"])]["status"] == "pending"]
    if pending_now:
        append_summary(args.summary, pending_details(pending_now, status))
    if not log:
        data["last_run"] = stamp(args.run_url, "apply made no commit")
        data["note"] = "The apply run made no commit, so every ticked item is pending; see the run log."
        gh.update_issue(issue["number"], render_body(data, marks))
        print("::warning::The apply run made no commit.")
        return 0

    git("push", "--force", "origin", f"refs/heads/{local}:refs/heads/{args.branch}")

    report = final_message(args.execution_file)
    # GitHub caps a PR body like an issue body: shorten the diffs, notes and report until it fits.
    tiers = ((20_000, 40_000, 20_000), (8_000, 15_000, 4_000), (3_000, 5_000, 1_000), (0, 0, 300))
    for diff_limit, report_limit, note_limit in tiers:
        body = pr_body(
            data,
            approved,
            status,
            issue["number"],
            args.run_url,
            args.branch,
            log,
            report,
            diff_limit,
            report_limit,
            note_limit,
        )
        if len(body) <= MAX_BODY:
            break
    else:
        # Still too long (a very long commit list): cut, then close what the cut left open.
        tail = "\n\n… (cut to fit GitHub's limit)\n"
        size = MAX_BODY - len(tail)
        while len(closed := _drop_dead_links(_close_blocks(body[:size]), pending_now) + tail) > MAX_BODY:
            size -= len(closed) - MAX_BODY
        body = closed
    title = f"Harness stocktake {data.get('report_date')}"

    pr = gh.find_pr(args.branch)
    pr = gh.update_pr(pr["number"], title, body) if pr else gh.create_pr(args.branch, base, title, body)
    n_applied = sum(1 for v in status.values() if v["status"] == "applied")
    data["pr_url"] = pr["html_url"]
    data["last_run"] = stamp(
        args.run_url, f"applied {n_applied} of {len(approved)} item(s), {len(approved) - n_applied} pending"
    )
    data.pop("note", None)
    gh.update_issue(issue["number"], render_body(data, marks))
    print(f"Pull request: {pr['html_url']}")
    write_output(pr_url=pr["html_url"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("audit-sync")
    a.add_argument("--workspace", default=".")
    a.add_argument("--plugin-ref", default="")
    a.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    a.add_argument(
        "--no-issue",
        dest="update_issue",
        action="store_false",
        help="report to the step summary only (pull_request runs)",
    )

    b = sub.add_parser("apply-prepare")
    b.add_argument("--workspace", default=".")
    b.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH"))

    c = sub.add_parser("apply-publish")
    c.add_argument("--run-date", required=True)
    c.add_argument("--branch", default=PR_BRANCH)
    c.add_argument("--default-branch", default="")
    c.add_argument("--execution-file", default="")
    c.add_argument("--workspace", default=".")
    c.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))

    d = sub.add_parser("mark-failed")
    d.add_argument("--message", required=True)

    for s in (c, d):
        s.add_argument("--issue-number", type=int, default=None)
    for s in (a, b, c, d):
        s.add_argument("--run-url", default=default_run_url())
    return p


def default_run_url() -> str:
    env = os.environ
    if env.get("GITHUB_RUN_ID"):
        return f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env.get('GITHUB_REPOSITORY')}/actions/runs/{env['GITHUB_RUN_ID']}"
    return ""


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    gh = GitHub(
        os.environ["GITHUB_TOKEN"],
        os.environ["GITHUB_REPOSITORY"],
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    commands = {
        "audit-sync": cmd_audit_sync,
        "apply-prepare": cmd_apply_prepare,
        "apply-publish": cmd_apply_publish,
        "mark-failed": cmd_mark_failed,
    }
    return commands[args.command](args, gh)


if __name__ == "__main__":
    sys.exit(main())
