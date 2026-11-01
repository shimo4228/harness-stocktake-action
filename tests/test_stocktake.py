"""Tests for scripts/stocktake.py. The GitHub API and git are fakes; nothing leaves the process."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import stocktake as st  # noqa: E402

RUN_URL = "https://github.com/o/r/actions/runs/1"


def item(id_, verdict, target, source="skills", **extra):
    return {
        "id": id_,
        "verdict": verdict,
        "target": target,
        "source": source,
        "reason": f"Reason for {id_}. More detail.",
        "change": f"change {id_}",
        **extra,
    }


def _names(changed, args):
    """A fake `git diff --name-only`: NUL-separated under -z, like git."""
    names = [n for n in changed.split("\n") if n]
    return "\0".join(names) + ("\0" if names else "") if "-z" in args else "\n".join(names)


ITEMS = [
    item("skills-01", "Retire", ".claude/skills/old/SKILL.md", order=1),
    item("docs-01", "Update", "README.md", source="docs", order=2),
    item("hooks-01", "Merge into fmt", ".claude/hooks/a.sh", source="hooks", order=3),
    item("rules-01", "Keep", ".claude/rules/style.md", source="rules", order=4),
    item("skills-02", "Retire", ".claude/skills/x/SKILL.md", order=5, folded_into="skills-01"),
]


def open_blocks(body):
    """(fence still open, <details> opened minus closed) for a Markdown body, read like GitHub."""
    fence = ""
    details = 0
    for line in body.splitlines():
        stripped = line.lstrip(" ")
        if fence:
            marks = stripped.rstrip()
            if marks and marks == fence[0] * len(marks) and len(marks) >= len(fence):
                fence = ""
            continue
        run = len(stripped) - len(stripped.lstrip(stripped[:1])) if stripped[:1] in ("`", "~") else 0
        if run >= 3 and len(line) - len(stripped) <= 3:
            fence = stripped[0] * run
            continue
        details += line.count("<details>") - line.count("</details>")
    return bool(fence), details


class FakeGitHub:
    def __init__(self, issues=None, prs=None, permissions=None):
        self.issues = {i["number"]: dict(i) for i in (issues or [])}
        self.prs = list(prs or [])
        self.permissions = permissions or {"owner": "admin"}
        self.calls = []

    def find_issue(self):
        self.calls.append("find_issue")
        open_ = [i for i in sorted(self.issues.values(), key=lambda i: i["number"]) if st.is_tracked_issue(i)]
        return dict(open_[0]) if open_ else None

    def get_issue(self, number):
        self.calls.append(("get_issue", number))
        return dict(self.issues[number])

    def permission(self, login):
        self.calls.append(("permission", login))
        return self.permissions.get(login, "read")

    def create_issue(self, body):
        number = max(self.issues, default=0) + 1
        self.issues[number] = {
            "number": number,
            "title": st.ISSUE_TITLE,
            "body": body,
            "user": {"type": "Bot", "login": "github-actions[bot]"},
            "html_url": f"https://github.com/o/r/issues/{number}",
        }
        self.calls.append(("create_issue", number))
        return self.issues[number]

    def update_issue(self, number, body):
        self.issues[number]["body"] = body
        self.calls.append(("update_issue", number))
        return self.issues[number]

    def default_branch(self):
        return "main"

    def find_pr(self, branch):
        return next((p for p in self.prs if p["head"] == branch), None)

    def create_pr(self, branch, base, title, body):
        pr = {
            "number": 50,
            "head": branch,
            "base": base,
            "title": title,
            "body": body,
            "html_url": "https://github.com/o/r/pull/50",
        }
        self.prs.append(pr)
        self.calls.append(("create_pr", branch, base))
        return pr

    def update_pr(self, number, title, body):
        pr = next(p for p in self.prs if p["number"] == number)
        pr.update(title=title, body=body)
        self.calls.append(("update_pr", number))
        return pr


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output = self.root / "output.txt"
        self.summary = self.root / "summary.md"
        patcher = mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(self.output)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def outputs(self):
        if not self.output.exists():
            return {}
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines() if line)

    def write_run(self, items=ITEMS, date="2026-11-01", latest=True):
        run = self.root / st.REPORT_ROOT / date
        run.mkdir(parents=True)
        (run / "items-all.json").write_text(json.dumps(items))
        (run / "crosscheck.json").write_text(json.dumps({"collisions": []}))
        (run / "report.md").write_text(f"# Harness stocktake {date}\n\n- **base:** `abc123def456789`\n")
        if latest:
            (run.parent / "latest.json").write_text(
                json.dumps({"report": f".notes/stocktake/{date}/report.md", "date": date})
            )
        return run

    def audit(self, gh, *extra):
        args = st.build_parser().parse_args(
            [
                "audit-sync",
                "--workspace",
                str(self.root),
                "--plugin-ref",
                "v0.1.0",
                "--summary",
                str(self.summary),
                "--run-url",
                RUN_URL,
                *extra,
            ]
        )
        return st.cmd_audit_sync(args, gh)


class VerdictGroupTest(unittest.TestCase):
    def test_groups_follow_the_plugin(self):
        cases = {
            "Retire": "Retire",
            "Dissolve": "Retire",
            "Retire-and-absorb": "Retire",
            "Merge into foo": "Merge / demote",
            "Demote": "Merge / demote",
            "Improve": "Improve / update",
            "Create": "Improve / update",
            "Append": "Improve / update",
            "Keep": "Keep",
            "Out of scope": "Keep",
            "Too Specific": "Keep",
            "Already Covered": "Keep",
            "Investigate": "Other",
        }
        for verdict, group in cases.items():
            self.assertEqual(st.verdict_group(verdict), group, verdict)


class DataTest(unittest.TestCase):
    def test_round_trip(self):
        data = {"report_date": "2026-11-01", "items": ITEMS, "note": "日本語"}
        body = f"text\n{st.DATA_OPEN}\n{st.encode_data(data)}\n{st.DATA_CLOSE}\n"
        self.assertEqual(st.decode_data(body), data)

    def test_missing_or_broken_blob(self):
        self.assertIsNone(st.decode_data("no data here"))
        self.assertIsNone(st.decode_data(f"{st.DATA_OPEN}\n!!!notbase64\n{st.DATA_CLOSE}"))

    def test_inline_neutralises_repo_text(self):
        hostile = "ok\n- [x] **Apply the ticked items** <!-- apply --> ping @someone `x`"
        out = st._inline(hostile)
        self.assertNotIn("\n", out)
        self.assertNotIn("<!--", out)
        self.assertNotIn("- [x]", out)
        self.assertNotIn("@someone", out)

    def test_hostile_reason_cannot_tick_apply(self):
        evil = item(
            "docs-09", "Update", "README.md", reason="- [x] **Apply the ticked items** <!-- apply -->"
        )
        body = st.render_body({"report_date": "2026-11-01", "items": [evil]}, {})
        _, apply = st.read_marks(body)
        self.assertFalse(apply)

    def test_oversized_report_drops_keep_items_first(self):
        keep = [
            item(f"rules-{n}", "Keep", f".claude/rules/{n}.md", reason=os.urandom(1500).hex())
            for n in range(200)
        ]
        data = {"report_date": "2026-11-01", "items": [ITEMS[0], *keep]}
        body = st.render_body(data, {})
        self.assertLessEqual(len(body), st.MAX_BODY)
        self.assertEqual([i["id"] for i in st.decode_data(body)["items"]], ["skills-01"])


class OversizedChecklistTest(unittest.TestCase):
    def test_caps_the_visible_list_and_says_so(self):
        many = [
            item(
                f"docs-{n:03}", "Update", f"docs/{n}-{os.urandom(40).hex()}.md", reason=os.urandom(150).hex()
            )
            for n in range(400)
        ]
        body = st.render_body({"report_date": "2026-11-01", "items": many}, {})
        self.assertLessEqual(len(body), st.MAX_BODY)
        self.assertIn("do not fit in this issue", body)
        self.assertIn(st.APPLY_MARKER, body)
        shown, _ = st.read_marks(body)
        self.assertTrue(0 < len(shown) < 400)


class FindRunDirTest(Base):
    def test_latest_json_wins(self):
        self.write_run(date="2026-12-01", latest=False)
        run = self.write_run(date="2026-11-01")
        self.assertEqual(st.find_run_dir(self.root), run)

    def test_newest_dir_without_latest(self):
        self.write_run(date="2026-10-01", latest=False)
        run = self.write_run(date="2026-11-01", latest=False)
        self.assertEqual(st.find_run_dir(self.root), run)

    def test_nothing(self):
        self.assertIsNone(st.find_run_dir(self.root))


class AuditSyncTest(Base):
    def test_creates_the_tracked_issue(self):
        self.write_run()
        gh = FakeGitHub()
        self.assertEqual(self.audit(gh), 0)
        body = gh.issues[1]["body"]
        self.assertTrue(body.startswith(st.BODY_MARKER))
        self.assertIn("**Last run:**", body)
        self.assertIn(RUN_URL, body)
        self.assertIn("60 days", body)
        marks, apply = st.read_marks(body)
        # Folded and Keep items get no box; new items start unticked.
        self.assertEqual(marks, {"skills-01": False, "docs-01": False, "hooks-01": False})
        self.assertFalse(apply)
        self.assertIn(st.APPLY_MARKER, body)
        data = st.decode_data(body)
        self.assertEqual(data["base"], "abc123def456789")
        self.assertEqual(data["plugin_ref"], "v0.1.0")
        self.assertIn("Harness stocktake 2026-11-01", self.summary.read_text())
        self.assertEqual(self.outputs()["items"], "3")
        self.assertEqual(self.outputs()["issue_number"], "1")

    def test_updates_and_carries_ticks_by_target(self):
        self.write_run()
        gh = FakeGitHub()
        self.audit(gh)
        gh.issues[1]["body"] = gh.issues[1]["body"].replace("- [ ] `docs-01`", "- [x] `docs-01`")
        gh.prs.append({"number": 50, "head": st.PR_BRANCH, "html_url": "https://github.com/o/r/pull/50"})

        # Next month: ids are renumbered, the docs item is the same finding.
        renumbered = [dict(i, id=i["id"].replace("01", "07")) for i in ITEMS]
        for i in renumbered:
            if i.get("folded_into"):
                i["folded_into"] = "skills-07"
        self.write_run(items=renumbered, date="2026-12-01")
        self.assertEqual(self.audit(gh), 0)
        self.assertEqual(len(gh.issues), 1)
        body = gh.issues[1]["body"]
        marks, _ = st.read_marks(body)
        self.assertEqual(marks, {"skills-07": False, "docs-07": True, "hooks-07": False})
        self.assertIn("pull/50", body)
        self.assertIn("2026-12-01", body)

    def test_drops_the_link_once_the_pr_is_closed(self):
        self.write_run()
        data = {"report_date": "2026-10-01", "items": ITEMS, "pr_url": "https://github.com/o/r/pull/12"}
        gh = FakeGitHub(
            issues=[
                {
                    "number": 1,
                    "title": st.ISSUE_TITLE,
                    "user": {"type": "Bot", "login": "github-actions[bot]"},
                    "body": st.render_body(data, {}),
                }
            ]
        )
        self.audit(gh)
        self.assertNotIn("pull/12", gh.issues[1]["body"])
        self.assertNotIn("**Pull request:**", gh.issues[1]["body"])

    def test_leaves_issues_people_opened_alone(self):
        self.write_run()
        human = {"number": 1, "title": st.ISSUE_TITLE, "user": {"type": "User"}, "body": "Bug: it broke"}
        gh = FakeGitHub(issues=[human])
        self.audit(gh)
        self.assertEqual(gh.issues[1]["body"], "Bug: it broke")
        self.assertIn(("create_issue", 2), gh.calls)

    def test_ignores_a_committed_report(self):
        run = self.write_run(date="2026-10-01")
        git = ["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "old report"], check=True)
        self.assertIsNone(st.find_run_dir(self.root))
        fresh = self.write_run(date="2026-11-01", latest=False)
        self.assertEqual(st.find_run_dir(self.root), fresh)
        self.assertNotEqual(fresh, run)

    def test_no_report_marks_the_issue(self):
        gh = FakeGitHub()
        self.assertEqual(self.audit(gh), 1)
        self.assertIn("audit failed", gh.issues[1]["body"])

    def test_no_report_prints_claudes_last_message(self):
        execution = self.root / "exec.json"
        message = "I stopped: no PyPI.\n::error::forged"
        execution.write_text(json.dumps([{"type": "result", "result": message}]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.audit(FakeGitHub(), "--execution-file", str(execution)), 1)
        lines = out.getvalue().splitlines()
        stop = next(i for i, line in enumerate(lines) if line.startswith("::stop-commands::"))
        token = lines[stop].removeprefix("::stop-commands::")
        self.assertEqual(lines[stop + 1 : stop + 3], message.splitlines())
        self.assertEqual(lines[stop + 3], f"::{token}::")

    def test_pull_request_run_leaves_the_issue(self):
        self.write_run()
        gh = FakeGitHub()
        self.assertEqual(self.audit(gh, "--no-issue"), 0)
        self.assertEqual(gh.calls, [])
        self.assertTrue(self.summary.read_text())

    def test_nothing_to_change(self):
        self.write_run(items=[ITEMS[3]])
        gh = FakeGitHub()
        self.audit(gh)
        body = gh.issues[1]["body"]
        self.assertIn("Nothing to change", body)
        self.assertNotIn(st.APPLY_MARKER, body)


class ApplyPrepareTest(Base):
    def issue_body(self, tick=(), apply=True):
        body = st.render_body(
            {"report_date": "2026-11-01", "base": "abc", "items": ITEMS, "crosscheck": {"collisions": []}},
            {i: True for i in tick},
        )
        if apply:
            body = body.replace(f"- [ ] **{st.APPLY_LABEL}**", f"- [x] **{st.APPLY_LABEL}**")
        return body

    def prepare(self, gh, body, title=st.ISSUE_TITLE, user=None, sender="owner"):
        issue = {
            "number": 7,
            "title": title,
            "body": body,
            "user": user or {"type": "Bot", "login": "github-actions[bot]"},
        }
        event = self.root / "event.json"
        event.write_text(json.dumps({"issue": issue, "sender": {"login": sender}}))
        gh.issues[7] = dict(issue)
        args = st.build_parser().parse_args(
            ["apply-prepare", "--workspace", str(self.root), "--event-path", str(event), "--run-url", RUN_URL]
        )
        return st.cmd_apply_prepare(args, gh)

    def test_other_issue_is_ignored(self):
        gh = FakeGitHub()
        self.assertEqual(self.prepare(gh, "hello", title="Bug"), 0)
        self.assertEqual(self.outputs()["apply"], "false")
        self.assertEqual(gh.calls, [])

    def test_apply_box_not_ticked(self):
        gh = FakeGitHub()
        self.prepare(gh, self.issue_body(tick=["docs-01"], apply=False))
        self.assertEqual(self.outputs()["apply"], "false")
        self.assertEqual(gh.calls, [("permission", "owner")])

    def test_issue_opened_by_a_person_is_ignored(self):
        gh = FakeGitHub()
        self.prepare(gh, self.issue_body(tick=["docs-01"]), user={"type": "User", "login": "mallory"})
        self.assertEqual(self.outputs()["apply"], "false")
        self.assertEqual(gh.calls, [])
        self.assertFalse((self.root / ".notes").exists())

    def test_edit_without_write_access_is_ignored(self):
        gh = FakeGitHub(permissions={"mallory": "read"})
        self.prepare(gh, self.issue_body(tick=["docs-01"]), sender="mallory")
        self.assertEqual(self.outputs()["apply"], "false")
        self.assertEqual(gh.calls, [("permission", "mallory")])
        self.assertFalse((self.root / ".notes").exists())

    def test_report_date_cannot_escape_the_notes_dir(self):
        data = {"report_date": "../../evil", "items": ITEMS}
        body = st.render_body(data, {"docs-01": True}).replace(
            f"- [ ] **{st.APPLY_LABEL}**", f"- [x] **{st.APPLY_LABEL}**"
        )
        gh = FakeGitHub()
        self.assertEqual(self.prepare(gh, body), 1)
        self.assertEqual(self.outputs()["apply"], "false")
        self.assertFalse((self.root / "evil").exists())

    def test_restores_the_run_with_decisions(self):
        gh = FakeGitHub()
        self.assertEqual(self.prepare(gh, self.issue_body(tick=["docs-01", "hooks-01"])), 0)
        out = self.outputs()
        self.assertEqual((out["apply"], out["run_date"], out["approved"]), ("true", "2026-11-01", "2"))

        run = self.root / st.REPORT_ROOT / "2026-11-01"
        report = (run / "report.md").read_text()
        rows = {
            line.split("|")[2].strip(): line.split("|")[-2].strip()
            for line in report.splitlines()
            if line.startswith("| ") and "---" not in line and "order" not in line
        }
        self.assertEqual(rows, {"skills-01": "skip", "docs-01": "accept", "hooks-01": "accept"})
        self.assertEqual(len(json.loads((run / "items-all.json").read_text())), len(ITEMS))
        self.assertTrue((run / "crosscheck.json").is_file())
        latest = json.loads((run.parent / "latest.json").read_text())
        self.assertEqual(latest["report"], ".notes/stocktake/2026-11-01/report.md")

        # The apply box is unticked at once; the item ticks stay.
        marks, apply = st.read_marks(gh.issues[7]["body"])
        self.assertFalse(apply)
        self.assertTrue(marks["docs-01"])
        self.assertIn("applying 2", gh.issues[7]["body"])

    def test_apply_with_nothing_ticked(self):
        gh = FakeGitHub()
        self.prepare(gh, self.issue_body())
        self.assertEqual(self.outputs()["apply"], "false")
        self.assertIn("no item ticked", gh.issues[7]["body"])

    def test_issue_without_data(self):
        gh = FakeGitHub()
        body = f"{st.BODY_MARKER}\n- [x] **{st.APPLY_LABEL}** {st.APPLY_MARKER}"
        self.assertEqual(self.prepare(gh, body), 1)
        self.assertEqual(self.outputs()["apply"], "false")


class ApplyPublishTest(Base):
    def setUp(self):
        super().setUp()
        data = {"report_date": "2026-11-01", "items": ITEMS}
        self.gh = FakeGitHub(
            issues=[
                {
                    "number": 7,
                    "title": st.ISSUE_TITLE,
                    "user": {"type": "Bot", "login": "github-actions[bot]"},
                    "body": st.render_body(data, {"docs-01": True}),
                }
            ]
        )
        self.git_calls = []
        self.execution = self.root / "exec.json"
        self.execution.write_text(
            json.dumps(
                [{"type": "assistant"}, {"type": "result", "result": "Applied docs-01. Pending: none."}]
            )
        )

    def git(self, log="abc1234 docs: update README", branches="stocktake/2026-11-01", changed="README.md"):
        def run(*args):
            self.git_calls.append(args)
            if args[0] == "for-each-ref":
                return branches
            if "--name-only" in args:
                return _names(changed, args)
            if args[0] == "log":
                return log
            return ""

        return run

    def publish(self, git):
        args = st.build_parser().parse_args(
            [
                "apply-publish",
                "--workspace",
                str(self.root),
                "--run-date",
                "2026-11-01",
                "--default-branch",
                "main",
                "--execution-file",
                str(self.execution),
                "--run-url",
                RUN_URL,
                "--issue-number",
                "7",
            ]
        )
        return st.cmd_apply_publish(args, self.gh, git)

    def test_opens_one_pr(self):
        self.assertEqual(self.publish(self.git()), 0)
        self.assertIn(
            ("push", "--force", "origin", "refs/heads/stocktake/2026-11-01:refs/heads/claude/stocktake"),
            self.git_calls,
        )
        self.assertIn(("create_pr", "claude/stocktake", "main"), self.gh.calls)
        pr = self.gh.prs[0]
        self.assertIn("Merging this pull request is the approval", pr["body"])
        self.assertIn("docs-01", pr["body"])
        self.assertNotIn("skills-01", pr["body"])
        self.assertIn("Applied docs-01", pr["body"])
        self.assertIn("1 applied", pr["body"])
        self.assertIn("pull/50", self.gh.issues[7]["body"])
        self.assertEqual(self.outputs()["pr_url"], "https://github.com/o/r/pull/50")

    def test_updates_the_open_pr(self):
        self.gh.prs.append(
            {
                "number": 50,
                "head": "claude/stocktake",
                "title": "old",
                "body": "old",
                "html_url": "https://github.com/o/r/pull/50",
            }
        )
        self.publish(self.git())
        self.assertIn(("update_pr", 50), self.gh.calls)
        self.assertEqual(len(self.gh.prs), 1)
        self.assertEqual(self.gh.prs[0]["title"], "Harness stocktake 2026-11-01")

    def test_no_commit_no_push(self):
        self.assertEqual(self.publish(self.git(log="")), 0)
        self.assertFalse(any(c[0] == "push" for c in self.git_calls))
        self.assertEqual(self.gh.prs, [])
        self.assertIn("made no commit", self.gh.issues[7]["body"])

    def test_missing_branch(self):
        self.assertEqual(self.publish(self.git(branches="")), 0)
        self.assertEqual(self.gh.prs, [])

    def test_finds_a_branch_named_for_the_apply_day(self):
        self.assertEqual(self.publish(self.git(branches="stocktake/2026-11-05")), 0)
        self.assertIn(
            ("push", "--force", "origin", "refs/heads/stocktake/2026-11-05:refs/heads/claude/stocktake"),
            self.git_calls,
        )
        self.assertEqual(len(self.gh.prs), 1)

    def test_looks_up_the_issue_by_number(self):
        self.publish(self.git())
        self.assertIn(("get_issue", 7), self.gh.calls)
        self.assertNotIn("find_issue", self.gh.calls)

    def test_completion_report_cannot_ping_or_open_comments(self):
        self.execution.write_text(json.dumps([{"type": "result", "result": "Done. cc @org/team <!-- x"}]))
        self.publish(self.git())
        body = self.gh.prs[0]["body"]
        self.assertNotIn("@org", body)
        self.assertNotIn("<!-- x", body)


SMOKE_ITEMS = [
    item("hooks-02", "Retire", ".claude/hooks/notify-done.sh", source="hooks", order=1),
    item("hooks-03", "Retire", ".claude/settings.json", source="hooks", order=2),
    item("docs-01", "Update", "CLAUDE.md", source="docs", order=3),
    item("docs-03", "Update", "CLAUDE.md", source="docs", order=4),
    item("skills-02", "Improve", ".claude/skills/deploy/SKILL.md", order=5),
    item("rules-01", "Merge", ".claude/rules/style.md", source="rules", order=6),
    item("docs-05", "Keep", "docs/adr/0001-in-memory-store.md", source="docs", order=7),
    item("assets-04", "Keep", "docs/adr/0001-in-memory-store.md", source="assets", order=8),
    item("distill-03", "Too Specific", ".claude/skills/code-review/SKILL.md", source="distill", order=9),
    item("skills-04", "Keep", ".claude/skills/code-review/SKILL.md", order=10),
]


class ProtectedPathTest(unittest.TestCase):
    def test_protected_paths_follow_claude_code(self):
        for path in [
            ".claude/settings.json",
            ".claude/skills/x/SKILL.md",
            ".claude/agents/a.md",
            "sub/.claude/rules/r.md",
            ".mcp.json",
            ".git/hooks/pre-commit",
            ".config/git/ignore",
        ]:
            self.assertTrue(st.is_protected(path), path)
        for path in ["CLAUDE.md", "README.md", "docs/adr/1.md", ".claude/worktrees/w/x.md", ".claudeignore"]:
            self.assertFalse(st.is_protected(path), path)

    def test_needs_hand(self):
        hand = {i["id"]: st.needs_hand(i) for i in SMOKE_ITEMS if st.verdict_group(i["verdict"]) != "Keep"}
        self.assertEqual(
            hand,
            {
                "hooks-02": False,  # a Retire is a git rm, which Claude Code allows
                "hooks-03": True,  # retiring a hook entry edits settings.json
                "docs-01": False,
                "docs-03": False,
                "skills-02": True,
                "rules-01": True,
            },
        )
        grouped = item("x-01", "Update", "group", targets=["README.md", ".claude/rules/a.md"])
        self.assertTrue(st.needs_hand(grouped))
        self.assertTrue(st.needs_hand(item("x-02", "Retire", ".mcp.json")))
        self.assertTrue(st.needs_hand(item("x-03", "Retire", "packages/app/.claude/settings.json")))
        self.assertFalse(st.needs_hand(item("x-04", "Retire", ".pre-commit-config.yaml")))
        self.assertTrue(st.needs_hand(item("x-05", "Update", ".pre-commit-config.yaml")))


class IssueTagsTest(unittest.TestCase):
    def test_hand_items_are_flagged_from_the_start(self):
        body = st.render_body({"report_date": "2026-11-01", "items": SMOKE_ITEMS}, {})
        rows = {ln.split("`")[1]: ln for ln in body.splitlines() if st.CHECKBOX.match(ln)}
        self.assertIn(st.TAG_HAND, rows["hooks-03"])
        self.assertIn(st.TAG_HAND, rows["skills-02"])
        self.assertNotIn(st.TAG_HAND, rows["hooks-02"])
        self.assertNotIn(st.TAG_HAND, rows["docs-01"])
        self.assertIn(st.README_URL, body)
        # The tag does not get in the way of reading the boxes.
        self.assertEqual(set(st.read_marks(body)[0]), set(rows))

    def test_no_legend_without_hand_items(self):
        body = st.render_body({"report_date": "2026-11-01", "items": [ITEMS[1]]}, {})
        self.assertNotIn(st.TAG_HAND, body)

    def test_kept_paths_are_listed_once(self):
        body = st.render_body({"report_date": "2026-11-01", "items": SMOKE_ITEMS}, {})
        kept = body.split("<summary>Kept items</summary>")[1].split("</details>")[0]
        self.assertEqual(kept.count("docs/adr/0001-in-memory-store.md"), 1)
        self.assertEqual(kept.count(".claude/skills/code-review/SKILL.md"), 1)
        self.assertIn("Keep (docs, assets)", kept)
        self.assertIn("Too Specific (distill) · Keep (skills)", kept)


class ItemStatusTest(unittest.TestCase):
    def test_the_branch_decides(self):
        approved = [SMOKE_ITEMS[0], SMOKE_ITEMS[1], SMOKE_ITEMS[2]]
        changed = {".claude/hooks/notify-done.sh", "CLAUDE.md"}
        status = st.item_status(approved, changed, {})
        self.assertEqual(
            {k: v["status"] for k, v in status.items()},
            {"hooks-02": "applied", "hooks-03": "pending", "docs-01": "applied"},
        )

    def test_a_claim_cannot_apply_an_unchanged_file(self):
        outcomes = {"hooks-03": {"outcome": "applied"}}
        status = st.item_status([SMOKE_ITEMS[1]], {"CLAUDE.md"}, outcomes)
        self.assertEqual(status["hooks-03"]["status"], "pending")

    def test_a_pending_record_wins_over_a_shared_file(self):
        approved = [SMOKE_ITEMS[2], SMOKE_ITEMS[3]]
        outcomes = {"docs-03": {"outcome": "pending", "note": "needs a choice", "diff": "-a\n+b"}}
        status = st.item_status(approved, {"CLAUDE.md"}, outcomes)
        self.assertEqual(status["docs-01"]["status"], "applied")
        self.assertEqual(status["docs-01"]["shared"], ["docs-03"])
        self.assertEqual(
            status["docs-03"],
            {"status": "pending", "note": "needs a choice", "diff": "-a\n+b", "partial": ["CLAUDE.md"]},
        )

    def test_a_note_without_an_outcome_still_warns_about_a_shared_file(self):
        approved = [SMOKE_ITEMS[2], SMOKE_ITEMS[3]]
        outcomes = {"docs-03": {"note": "Rewrote the build section."}}
        status = st.item_status(approved, {"CLAUDE.md"}, outcomes)
        self.assertEqual(status["docs-03"]["status"], "applied")
        self.assertEqual(status["docs-03"]["shared"], ["docs-01"])
        self.assertEqual(status["docs-01"]["shared"], ["docs-03"])
        claimed = st.item_status(approved, {"CLAUDE.md"}, {"docs-03": {"outcome": "applied"}})
        self.assertNotIn("shared", claimed["docs-03"])

    def test_directories_and_globs(self):
        retire_dir = item("skills-09", "Retire", ".claude/skills/old")
        glob = item("skills-08", "Retire", ".claude/skills/pack-*/SKILL.md")
        changed = {".claude/skills/old/SKILL.md", ".claude/skills/pack-a/SKILL.md"}
        status = st.item_status([retire_dir, glob], changed, {})
        self.assertEqual({v["status"] for v in status.values()}, {"applied"})

    def test_every_path_must_change(self):
        merge = item(
            "rules-01", "Merge", ".claude/rules/style.md", targets=[".claude/rules/style.md", "CLAUDE.md"]
        )
        status = st.item_status([merge], {"CLAUDE.md"}, {})["rules-01"]
        self.assertEqual(status["status"], "pending")
        self.assertEqual(status["partial"], ["CLAUDE.md"])
        self.assertIn(".claude/rules/style.md", status["note"])
        both = st.item_status([merge], {"CLAUDE.md", ".claude/rules/style.md"}, {})["rules-01"]
        self.assertEqual(both["status"], "pending")  # it edits .claude/: needs the run's own record
        claimed = st.item_status(
            [merge], {"CLAUDE.md", ".claude/rules/style.md"}, {"rules-01": {"outcome": "applied"}}
        )
        self.assertEqual(claimed["rules-01"]["status"], "applied")

    def test_hand_item_without_a_record_is_not_applied(self):
        improve = item("skills-02", "Improve", ".claude/skills/deploy/SKILL.md")
        status = st.item_status([improve], {".claude/skills/deploy/SKILL.md"}, {})["skills-02"]
        self.assertEqual(status["status"], "pending")
        self.assertIn("cannot edit", status["note"])

    def test_load_outcomes_tolerates_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.json"
            self.assertEqual(st.load_outcomes(path), {})
            path.write_text("not json")
            self.assertEqual(st.load_outcomes(path), {})
            path.write_text(json.dumps([{"id": "a-01", "outcome": "pending"}, "junk"]))
            self.assertEqual(st.load_outcomes(path), {"a-01": {"id": "a-01", "outcome": "pending"}})
            path.write_text(json.dumps({"a-01": "applied", "a-02": 3}))
            self.assertEqual(st.load_outcomes(path), {"a-01": {"outcome": "applied"}})


class SmokePublishTest(Base):
    """The smoke test's apply: hooks-02 and docs-01 committed, hooks-03 (settings.json) denied."""

    def setUp(self):
        super().setUp()
        data = {"report_date": "2026-11-01", "items": SMOKE_ITEMS}
        ticked = {"hooks-02": True, "hooks-03": True, "docs-01": True}
        self.gh = FakeGitHub(
            issues=[
                {
                    "number": 1,
                    "title": st.ISSUE_TITLE,
                    "user": {"type": "Bot", "login": "github-actions[bot]"},
                    "body": st.render_body(data, ticked),
                }
            ]
        )

    def git(self, log, changed):
        def run(*args):
            if "--name-only" in args:
                return _names(changed, args)
            if args[0] == "log":
                return log
            return {"for-each-ref": "stocktake/2026-11-01"}.get(args[0], "")

        return run

    def publish(self, log="1016069 docs\na793a4b chore", changed=".claude/hooks/notify-done.sh\nCLAUDE.md"):
        args = st.build_parser().parse_args(
            [
                "apply-publish",
                "--workspace",
                str(self.root),
                "--run-date",
                "2026-11-01",
                "--default-branch",
                "main",
                "--run-url",
                RUN_URL,
                "--issue-number",
                "1",
            ]
        )
        return st.cmd_apply_publish(args, self.gh, self.git(log, changed))

    def test_pr_separates_applied_from_pending(self):
        self.assertEqual(self.publish(), 0)
        body = self.gh.prs[0]["body"]
        applied, pending = body.split("**Pending, apply by hand")
        pending = pending.split("### Commits")[0]
        self.assertIn("`hooks-02`", applied)
        self.assertIn("`docs-01`", applied)
        self.assertNotIn("`hooks-03`", applied)
        self.assertIn("`hooks-03`", pending)
        self.assertIn("### Pending items", pending)
        self.assertIn("2 applied", body)
        self.assertIn("1 left for you to apply by hand", body)
        # No diff was recorded: the approved change stands in for it.
        self.assertIn("change hooks-03", pending)

        issue = self.gh.issues[1]["body"]
        rows = {ln.split("`")[1]: ln for ln in issue.splitlines() if st.CHECKBOX.match(ln)}
        self.assertIn(st.TAG_APPLIED, rows["hooks-02"])
        self.assertIn(st.TAG_APPLIED, rows["docs-01"])
        self.assertIn(st.TAG_PENDING, rows["hooks-03"])
        self.assertIn(st.TAG_HAND, rows["skills-02"])  # not ticked: still the up-front flag
        self.assertIn("applied 2 of 3 item(s), 1 pending", issue)

    def test_pending_diff_comes_from_outcomes_json(self):
        run = self.root / st.REPORT_ROOT / "2026-11-01"
        run.mkdir(parents=True)
        diff = '-      {\n-        "matcher": "Bash",\n```nested```'
        (run / st.OUTCOMES_FILE).write_text(
            json.dumps(
                {"hooks-03": {"outcome": "pending", "note": "Edit denied on settings.json", "diff": diff}}
            )
        )
        self.publish()
        body = self.gh.prs[0]["body"]
        self.assertIn("Edit denied on settings.json", body)
        self.assertIn('"matcher": "Bash"', body)
        self.assertIn("````diff", body)  # fence longer than the backticks inside

    def test_changed_files_use_the_commit_range_and_skip_notes(self):
        calls = []

        def git(*args):
            calls.append(args)
            if "--name-only" in args:
                return "CLAUDE.md\0.notes/stocktake/2026-11-01/outcomes.json\0"
            return ""

        self.assertEqual(st.changed_files(git, "main", "stocktake/x"), {"CLAUDE.md"})
        # The branch's net change since it left main: three dots, not every commit's files.
        self.assertIn("origin/main...stocktake/x", calls[0])

    def real_git(self):
        repo = self.root / "repo"
        repo.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        def git(*args):
            return st.run_git("-C", str(repo), *args)

        git("init", "-q", "-b", "main")
        (repo / "README.md").write_text("readme\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        git("checkout", "-q", "-b", "stocktake/2026-11-01")
        return repo, git

    def test_changed_files_keep_non_ascii_paths(self):
        repo, git = self.real_git()
        (repo / "docs").mkdir()
        (repo / "docs" / "設計.md").write_text("design\n")
        git("add", "-A")
        git("commit", "-q", "-m", "docs: design note")
        changed = st.changed_files(git, "main", "stocktake/2026-11-01")
        self.assertEqual(changed, {"docs/設計.md"})
        design = item("docs-07", "Update", "docs/設計.md", source="docs")
        self.assertEqual(st.item_status([design], changed, {})["docs-07"]["status"], "applied")

    def test_changed_files_are_the_net_diff(self):
        repo, git = self.real_git()
        (repo / "README.md").write_text("changed\n")
        (repo / "CLAUDE.md").write_text("rules\n")
        git("add", "-A")
        git("commit", "-q", "-m", "edit")
        (repo / "README.md").write_text("readme\n")
        git("add", "-A")
        git("commit", "-q", "-m", "revert README")
        self.assertEqual(st.changed_files(git, "main", "stocktake/2026-11-01"), {"CLAUDE.md"})

    def test_pending_diff_is_shown_verbatim(self):
        run = self.root / st.REPORT_ROOT / "2026-11-01"
        run.mkdir(parents=True)
        diff = "-  <!-- old -->\n+  <!-- new --> cc @alice\n+```\n"
        (run / st.OUTCOMES_FILE).write_text(json.dumps({"hooks-03": {"outcome": "pending", "diff": diff}}))
        self.publish()
        body = self.gh.prs[0]["body"]
        self.assertIn("````diff\n" + diff.rstrip("\n") + "\n````", body)
        self.assertEqual(open_blocks(body), (False, 0))

    def test_fallback_change_is_shown_verbatim(self):
        smoke = [dict(i) for i in SMOKE_ITEMS]
        smoke[1]["change"] = 'Drop the hook: `"command": "x --> y"` (ask @bob)'
        data = {"report_date": "2026-11-01", "items": smoke}
        ticked = {"hooks-02": True, "hooks-03": True, "docs-01": True}
        self.gh.issues[1]["body"] = st.render_body(data, ticked)
        self.publish()
        body = self.gh.prs[0]["body"]
        self.assertIn("```\n" + smoke[1]["change"] + "\n```", body)

    def publish_big(self, notes="", diff="", report="", log="abc x"):
        run = self.root / st.REPORT_ROOT / "2026-11-01"
        run.mkdir(parents=True, exist_ok=True)
        ids = ("hooks-02", "hooks-03", "docs-01")
        outcomes = {i: {"outcome": "pending", "note": notes, "diff": diff} for i in ids}
        (run / st.OUTCOMES_FILE).write_text(json.dumps(outcomes))
        execution = self.root / "exec.json"
        execution.write_text(json.dumps([{"type": "result", "result": report}]))
        args = st.build_parser().parse_args(
            [
                "apply-publish",
                "--workspace",
                str(self.root),
                "--run-date",
                "2026-11-01",
                "--default-branch",
                "main",
                "--run-url",
                RUN_URL,
                "--issue-number",
                "1",
                "--execution-file",
                str(execution),
            ]
        )
        self.assertEqual(st.cmd_apply_publish(args, self.gh, self.git(log, "CLAUDE.md")), 0)
        body = self.gh.prs[0]["body"]
        self.assertLessEqual(len(body), st.MAX_BODY)
        return body

    def test_long_notes_are_shortened_before_the_report_goes(self):
        big = "\n".join(f"-line {n} " + "x" * 80 for n in range(400))  # ~36k
        body = self.publish_big(notes="n" * 20_000, diff=big, report="r" * 60_000)
        self.assertNotIn("n" * 20_000, body)
        self.assertIn("### Completion report", body)
        self.assertEqual(open_blocks(body), (False, 0))

    def test_no_pointer_to_a_section_the_body_dropped(self):
        big = "\n".join(f"-line {n} " + "x" * 80 for n in range(400))
        log = "\n".join(f"{n:07x} commit" for n in range(6_000))  # ~90k: only the final cut fits
        body = self.publish_big(notes="n" * 20_000, diff=big, report="r" * 60_000, log=log)
        self.assertNotIn("### Completion report", body)
        self.assertNotIn("see the completion report", body)
        self.assertNotIn("completion report may say more", body)
        self.assertIn("cut to fit GitHub's limit", body)
        self.assertEqual(open_blocks(body), (False, 0))

    def test_a_cut_report_keeps_its_details_closed(self):
        report = "Summary\n\n```diff\n" + "+x\n" * 30_000
        body = self.publish_big(report=report)
        self.assertIn("### Completion report", body)
        self.assertEqual(open_blocks(body), (False, 0))

    def test_final_cut_drops_links_to_cut_pending_items(self):
        many = [item(f"docs-{n:03d}", "Update", f"docs/f{n}.md", source="docs", order=n) for n in range(150)]
        for i in many:
            i["change"] = "c" * 5_000
        data = {"report_date": "2026-11-01", "items": many}
        self.gh.issues[1]["body"] = st.render_body(data, {i["id"]: True for i in many})
        run = self.root / st.REPORT_ROOT / "2026-11-01"
        run.mkdir(parents=True)
        (run / st.OUTCOMES_FILE).write_text(json.dumps({i["id"]: {"note": "n" * 20_000} for i in many}))
        self.publish(changed="")
        body = self.gh.prs[0]["body"]
        self.assertLessEqual(len(body), st.MAX_BODY)
        self.assertIn("cut to fit GitHub's limit", body)
        lines = {ln.split("`")[1]: ln for ln in body.splitlines() if ln.startswith("- `docs-")}
        self.assertEqual(len(lines), 150)
        cut = [i for i in many if f"#### {st._label(i)}" not in body]
        self.assertTrue(cut)
        for i in many:
            linked = "#pending-items" in lines[i["id"]]
            self.assertEqual(linked, i not in cut, i["id"])
        self.assertEqual(open_blocks(body), (False, 0))

    def test_short_change_is_shown_at_the_last_tier(self):
        pending = [SMOKE_ITEMS[1], dict(SMOKE_ITEMS[2], change="")]
        status = {i["id"]: {"status": "pending"} for i in pending}
        body = st.pr_body({"report_date": "2026-11-01"}, pending, status, 1, RUN_URL, "b", "", "", 0, 0, 300)
        self.assertIn("```\nchange hooks-03\n```", body)
        self.assertIn("```\n(none)\n```", body)
        self.assertNotIn("too long", body)
        long = [dict(SMOKE_ITEMS[1], change="x" * 2_000)]
        body = st.pr_body({"report_date": "2026-11-01"}, long, status, 1, RUN_URL, "b", "", "", 0, 0, 300)
        self.assertIn("approved change (2000 characters) is too long", body)

    def test_a_diff_too_long_to_show_is_never_shown_cut(self):
        run = self.root / st.REPORT_ROOT / "2026-11-01"
        run.mkdir(parents=True)
        diff = "\n".join(f"-line {n} " + "x" * 90 for n in range(300))  # ~30k
        self.assertGreater(len(diff), 20_000)
        (run / st.OUTCOMES_FILE).write_text(json.dumps({"hooks-03": {"outcome": "pending", "diff": diff}}))
        self.publish()
        body = self.gh.prs[0]["body"]
        self.assertIn(f"The diff ({len(diff)} characters) is too long", body)
        self.assertNotIn("-line 0 ", body)
        status = st.item_status([SMOKE_ITEMS[1]], set(), {"hooks-03": {"outcome": "pending", "diff": diff}})
        self.assertEqual(status["hooks-03"]["diff"], diff)

    def test_short_diff_is_shown_at_the_last_tier(self):
        pending = [SMOKE_ITEMS[1]]
        status = {"hooks-03": {"status": "pending", "diff": "-a\n+b"}}
        body = st.pr_body({"report_date": "2026-11-01"}, pending, status, 1, RUN_URL, "b", "", "", 0, 0, 300)
        self.assertIn("```diff\n-a\n+b\n```", body)
        self.assertNotIn("too long", body)

    def test_pending_details_go_to_the_run_summary(self):
        run = self.root / st.REPORT_ROOT / "2026-11-01"
        run.mkdir(parents=True)
        diff = "\n".join(f"-line {n} " + "x" * 90 for n in range(300))  # ~30k: too long for the body
        (run / st.OUTCOMES_FILE).write_text(
            json.dumps({"hooks-03": {"outcome": "pending", "note": "apply by hand", "diff": diff}})
        )
        summary = self.root / "apply-summary.md"
        args = st.build_parser().parse_args(
            [
                "apply-publish",
                "--workspace",
                str(self.root),
                "--run-date",
                "2026-11-01",
                "--default-branch",
                "main",
                "--run-url",
                RUN_URL,
                "--issue-number",
                "1",
                "--summary",
                str(summary),
            ]
        )
        log, changed = "1016069 docs\na793a4b chore", ".claude/hooks/notify-done.sh\nCLAUDE.md"
        self.assertEqual(st.cmd_apply_publish(args, self.gh, self.git(log, changed)), 0)
        text = summary.read_text()
        self.assertIn(st._label(SMOKE_ITEMS[1]), text)
        self.assertIn("apply by hand", text)
        self.assertIn(diff, text)
        self.assertNotIn("-line 0 ", self.gh.prs[0]["body"])

    def test_close_blocks_after_a_cut(self):
        text = "<details><summary>s</summary>\n\n````diff\n+a\n```\n+b"
        closed = st._close_blocks(text)
        self.assertTrue(closed.startswith(text))
        self.assertEqual(open_blocks(closed), (False, 0))
        self.assertEqual(st._close_blocks("done\n```\nx\n```\n"), "done\n```\nx\n```\n")

    def test_final_cut_fits_with_a_long_closing_fence(self):
        log = "`" * 400 + "\n" + "\n".join(f"{n:07x} commit" for n in range(6_000))
        body = self.publish_big(log=log)
        self.assertIn("cut to fit GitHub's limit", body)
        self.assertEqual(open_blocks(body), (False, 0))

    def test_changed_files_failure_warns(self):
        def git(*args):
            raise subprocess.CalledProcessError(128, "git")

        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(st.changed_files(git, "main", "x"), set())
        self.assertIn("::warning::", out.getvalue())

    def test_pr_body_fits_githubs_limit(self):
        run = self.root / st.REPORT_ROOT / "2026-11-01"
        run.mkdir(parents=True)
        big = "\n".join(f"-line {n} " + "x" * 80 for n in range(400))  # ~36k
        outcomes = {i: {"outcome": "pending", "diff": big} for i in ("hooks-02", "hooks-03", "docs-01")}
        (run / st.OUTCOMES_FILE).write_text(json.dumps(outcomes))
        execution = self.root / "exec.json"
        execution.write_text(json.dumps([{"type": "result", "result": "r" * 60_000}]))
        args = st.build_parser().parse_args(
            [
                "apply-publish",
                "--workspace",
                str(self.root),
                "--run-date",
                "2026-11-01",
                "--default-branch",
                "main",
                "--run-url",
                RUN_URL,
                "--issue-number",
                "1",
                "--execution-file",
                str(execution),
            ]
        )
        self.assertEqual(st.cmd_apply_publish(args, self.gh, self.git("abc x", "CLAUDE.md")), 0)
        body = self.gh.prs[0]["body"]
        self.assertLessEqual(len(body), st.MAX_BODY)
        self.assertIn("### Pending items", body)

    def test_unticked_item_drops_its_result(self):
        self.publish()
        body = self.gh.issues[1]["body"].replace("- [x] `docs-01`", "- [ ] `docs-01`")
        data = st.decode_data(body)
        marks, _ = st.read_marks(body)
        rerendered = st.render_body(data, marks)
        rows = {ln.split("`")[1]: ln for ln in rerendered.splitlines() if st.CHECKBOX.match(ln)}
        self.assertNotIn(st.TAG_APPLIED, rows["docs-01"])
        self.assertIn(st.TAG_APPLIED, rows["hooks-02"])

    def test_no_commit_marks_every_ticked_item_pending(self):
        self.assertEqual(self.publish(log="", changed=""), 0)
        self.assertEqual(self.gh.prs, [])
        issue = self.gh.issues[1]["body"]
        rows = {ln.split("`")[1]: ln for ln in issue.splitlines() if st.CHECKBOX.match(ln)}
        for id_ in ("hooks-02", "hooks-03", "docs-01"):
            self.assertIn(st.TAG_PENDING, rows[id_])

    def test_next_apply_starts_clean(self):
        self.publish()
        body = self.gh.issues[1]["body"].replace(f"- [ ] **{st.APPLY_LABEL}**", f"- [x] **{st.APPLY_LABEL}**")
        event = self.root / "event.json"
        issue = dict(self.gh.issues[1], body=body)
        event.write_text(json.dumps({"issue": issue, "sender": {"login": "owner"}}))
        stale = self.root / st.REPORT_ROOT / "2026-11-01" / st.OUTCOMES_FILE
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("{}")
        args = st.build_parser().parse_args(
            ["apply-prepare", "--workspace", str(self.root), "--event-path", str(event), "--run-url", RUN_URL]
        )
        self.assertEqual(st.cmd_apply_prepare(args, self.gh), 0)
        self.assertNotIn("outcomes", st.decode_data(self.gh.issues[1]["body"]))
        self.assertNotIn(st.TAG_PENDING, self.gh.issues[1]["body"])
        self.assertFalse(stale.exists())


class MarkFailedTest(Base):
    def test_keeps_report_and_records_failure(self):
        data = {"report_date": "2026-11-01", "items": ITEMS}
        gh = FakeGitHub(
            issues=[
                {
                    "number": 3,
                    "title": st.ISSUE_TITLE,
                    "user": {"type": "Bot", "login": "github-actions[bot]"},
                    "body": st.render_body(data, {"docs-01": True}),
                }
            ]
        )
        args = st.build_parser().parse_args(
            ["mark-failed", "--message", "apply failed", "--run-url", RUN_URL]
        )
        self.assertEqual(st.cmd_mark_failed(args, gh), 0)
        body = gh.issues[3]["body"]
        self.assertIn("apply failed", body)
        self.assertTrue(st.read_marks(body)[0]["docs-01"])
        self.assertEqual(st.decode_data(body)["report_date"], "2026-11-01")


class GitHubClientTest(unittest.TestCase):
    def test_find_issue_pages_and_skips_prs(self):
        gh = st.GitHub("t", "o/r")
        page1 = [{"number": n, "title": "x"} for n in range(100)]
        tracked = {"title": st.ISSUE_TITLE, "user": {"type": "Bot"}, "body": st.BODY_MARKER}
        page2 = [
            {"number": 199, "title": st.ISSUE_TITLE, "user": {"type": "User"}, "body": st.BODY_MARKER},
            {"number": 200, **tracked, "pull_request": {}},
            {"number": 201, **tracked},
        ]
        with mock.patch.object(gh, "request", side_effect=[page1, page2]) as req:
            self.assertEqual(gh.find_issue()["number"], 201)
        self.assertIn("page=2", req.call_args_list[1].args[1])

    def test_request_sends_auth_and_reports_errors(self):
        gh = st.GitHub("secret-token", "o/r")
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            seen["auth"] = req.get_header("Authorization")
            seen["url"] = req.full_url
            return Resp(b'{"default_branch": "trunk"}')

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(gh.default_branch(), "trunk")
        self.assertEqual(seen, {"auth": "Bearer secret-token", "url": "https://api.github.com/repos/o/r"})

        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"no"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(st.GitHubError):
                gh.default_branch()

    def test_find_pr_uses_owner_head(self):
        gh = st.GitHub("t", "o/r")
        with mock.patch.object(gh, "request", return_value=[]) as req:
            self.assertIsNone(gh.find_pr("claude/stocktake"))
        self.assertIn("head=o:claude/stocktake", req.call_args.args[1])


class FinalMessageTest(unittest.TestCase):
    def test_missing_or_bad_file(self):
        self.assertEqual(st.final_message(""), "")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("not json")
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(st.final_message(fh.name), "")


if __name__ == "__main__":
    unittest.main()
