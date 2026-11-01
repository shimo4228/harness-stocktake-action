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
REPORT_ROOT = Path(".notes/stocktake")

GROUPS = ["Retire", "Merge / demote", "Improve / update", "Other", "Keep"]
_GROUP_PREFIXES = [
    ("Retire", ("retire", "dissolve")),
    ("Merge / demote", ("merge", "demote")),
    ("Improve / update", ("improve", "update", "append", "revise", "create")),
    ("Keep", ("keep", "out of scope", "already covered", "too specific")),
]

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
        page = 1
        while True:
            batch = self.request("GET", f"/repos/{self.repo}/issues?state=open&per_page=100&page={page}")
            for issue in batch:
                if "pull_request" not in issue and issue.get("title") == ISSUE_TITLE:
                    return issue
            if len(batch) < 100:
                return None
            page += 1

    def get_issue(self, number: int) -> dict[str, Any]:
        return self.request("GET", f"/repos/{self.repo}/issues/{number}")

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


# --- report data ------------------------------------------------------------------------------


def verdict_group(verdict: str) -> str:
    v = verdict.strip().lower()
    for group, prefixes in _GROUP_PREFIXES:
        if v.startswith(prefixes):
            return group
    return "Other"


def find_run_dir(workspace: Path) -> Path | None:
    """The run directory of the newest report: latest.json first, else the newest dated dir."""
    root = workspace / REPORT_ROOT
    latest = root / "latest.json"
    if latest.is_file():
        try:
            report = Path(json.loads(latest.read_text())["report"])
        except (ValueError, KeyError, TypeError):
            report = None
        if report is not None:
            run = report.parent if report.is_absolute() else (workspace / report).parent
            if (run / "items-all.json").is_file():
                return run
    if not root.is_dir():
        return None
    runs = sorted(p for p in root.iterdir() if p.is_dir() and (p / "items-all.json").is_file())
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


def _inline(text: object, limit: int = 240) -> str:
    """Repo-derived text on one line, unable to tick a box, ping a user or close a comment."""
    s = " ".join(str(text or "").split())
    s = s.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    s = s.replace("[", "\\[").replace("`", "'")
    s = re.sub(r"@(?=\w)", "@\u200b", s)
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
        for group in GROUPS[:-1]:
            rows = [i for i in acting if verdict_group(str(i["verdict"])) == group]
            if not rows:
                continue
            lines += [f"### {group}", ""]
            for i in rows:
                mark = "x" if ticked.get(str(i["id"])) else " "
                n = len(i.get("targets") or [])
                extra = f" (+{n - 1} more)" if n > 1 else ""
                lines.append(f"- [{mark}] {_label(i)}{extra} — {_inline(_first_sentence(i.get('reason')))}")
            lines.append("")
        lines += ["---", "", f"- [ ] **{APPLY_LABEL}** {APPLY_MARKER}", ""]
    if keep:
        lines += ["<details><summary>Kept items</summary>", ""]
        lines += [f"- `{_inline(i.get('target'), 120)}` — {_inline(i.get('verdict'), 60)}" for i in keep]
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
    raise ValueError("report too large for the tracked issue")


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
    if previous and previous.get("pr_url"):
        data["pr_url"] = previous["pr_url"]
    body = render_body(data, ticked)
    issue = gh.update_issue(issue["number"], body) if issue else gh.create_issue(body)
    print(f"Tracked issue: {issue.get('html_url', issue['number'])}")
    write_output(issue_number=issue["number"])
    return 0


def record_failure(gh: GitHub, run_url: str, message: str) -> None:
    issue = gh.find_issue()
    data = (decode_data(issue["body"] or "") if issue else None) or {}
    marks = read_marks(issue["body"] or "")[0] if issue else {}
    data["last_run"] = stamp(run_url, message)
    body = render_body(data, marks)
    if issue:
        gh.update_issue(issue["number"], body)
    else:
        gh.create_issue(body)


def cmd_mark_failed(args: argparse.Namespace, gh: GitHub) -> int:
    record_failure(gh, args.run_url, args.message)
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
    if issue.get("title") != ISSUE_TITLE:
        print("Not the tracked issue.")
        write_output(apply="false")
        return 0
    body = issue.get("body") or ""
    marks, apply = read_marks(body)
    if not apply:
        print("The apply box is not ticked.")
        write_output(apply="false")
        return 0
    data = decode_data(body)
    if not data or not data.get("report_date"):
        print("::error::The tracked issue holds no report data; run the workflow once first.")
        write_output(apply="false")
        return 1
    approved = [i for i in acting_items(data["items"]) if marks.get(str(i["id"]))]

    # Untick the apply box at once, so a second edit does not start a second apply.
    data.pop("note", None)
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


def cmd_apply_publish(args: argparse.Namespace, gh: GitHub, git: Git = run_git) -> int:
    issue = gh.find_issue()
    if issue is None:
        print("::error::The tracked issue is gone.")
        return 1
    data = decode_data(issue["body"] or "") or {}
    marks, _ = read_marks(issue["body"] or "")
    local = f"stocktake/{args.run_date}"
    base = args.default_branch or gh.default_branch()

    try:
        git("rev-parse", "--verify", f"refs/heads/{local}")
        log = git("log", "--oneline", f"origin/{base}..{local}")
    except subprocess.CalledProcessError:
        log = ""
    if not log:
        data["last_run"] = stamp(args.run_url, "apply made no commit")
        data["note"] = "The apply run made no commit; see the run log for the pending items."
        gh.update_issue(issue["number"], render_body(data, marks))
        print("::warning::The apply run made no commit.")
        return 0

    git("push", "--force", "origin", f"refs/heads/{local}:refs/heads/{args.branch}")

    approved = [i for i in acting_items(data.get("items", [])) if marks.get(str(i["id"]))]
    lines = [
        (
            f"Harness stocktake of {data.get('report_date')}: {len(approved)} approved item(s), "
            f"applied on `{args.branch}`."
        ),
        "",
        "**Merging this pull request is the approval.** Nothing reached the default branch before it.",
        "",
        f"Tracked issue: #{issue['number']} · [run]({args.run_url})",
        "",
        "### Approved items",
        "",
    ]
    lines += [f"- {_label(i)}" for i in approved]
    lines += ["", "### Commits", "", "```", log, "```", ""]
    report = final_message(args.execution_file)
    if report:
        lines += [
            "### Completion report",
            "",
            "<details><summary>From the apply run</summary>",
            "",
            report[:40_000],
            "",
            "</details>",
            "",
        ]
    body = "\n".join(lines)
    title = f"Harness stocktake {data.get('report_date')}"

    pr = gh.find_pr(args.branch)
    pr = gh.update_pr(pr["number"], title, body) if pr else gh.create_pr(args.branch, base, title, body)
    data["pr_url"] = pr["html_url"]
    data["last_run"] = stamp(args.run_url, f"applied {len(approved)} item(s)")
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
    c.add_argument("--branch", default="claude/stocktake")
    c.add_argument("--default-branch", default="")
    c.add_argument("--execution-file", default="")

    d = sub.add_parser("mark-failed")
    d.add_argument("--message", required=True)

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
