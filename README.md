[English](README.md) | [日本語](README.ja.md)

# harness-stocktake-action

Runs [harness-stocktake](https://github.com/shimo4228/harness-stocktake) on your repository's Claude Code harness (`.claude/` skills, rules, hooks, agents, settings, and `CLAUDE.md`) once a month and whenever a pull request changes it. The findings go into one tracked issue. You tick what you approve, and the Action applies those items on one branch and opens one pull request. Merging that pull request is the approval.

## Quick start

1. Store your own Claude credential as a repository secret:

   ```bash
   claude setup-token | gh secret set CLAUDE_CODE_OAUTH_TOKEN
   ```

   If `setup-token` prints more than the token, run `claude setup-token`, copy the token, and paste it into `gh secret set CLAUDE_CODE_OAUTH_TOKEN`. To use an API key instead: `gh secret set ANTHROPIC_API_KEY`, then swap the commented line in the workflow.

2. Copy the workflow:

   ```bash
   mkdir -p .github/workflows
   curl -sSfL https://raw.githubusercontent.com/shimo4228/harness-stocktake-action/main/examples/harness-stocktake.yml -o .github/workflows/harness-stocktake.yml
   ```

3. Let the workflow open pull requests: **Settings → Actions → General → Workflow permissions → Allow GitHub Actions to create and approve pull requests**.

4. Commit, push, and start the first run from **Actions → Harness stocktake → Run workflow**.

The workflow in [`examples/harness-stocktake.yml`](examples/harness-stocktake.yml):

```yaml
name: Harness stocktake

on:
  schedule:
    - cron: "0 7 1 * *"
  workflow_dispatch:
  pull_request:
    paths: [".claude/**", "CLAUDE.md"]
  issues:
    types: [edited]

permissions:
  contents: write
  issues: write
  pull-requests: write
  id-token: write

concurrency:
  # Pull request runs queue apart, so they never cancel a pending apply or monthly run.
  group: harness-stocktake-${{ github.event_name == 'pull_request' && github.event.pull_request.number || 'repo' }}
  cancel-in-progress: false

jobs:
  stocktake:
    if: >-
      (github.event_name != 'issues' ||
        (github.event.issue.title == 'Harness stocktake' &&
         github.event.issue.user.type == 'Bot' &&
         contains(github.event.issue.body, '- [x] **Apply the ticked items**'))) &&
      (github.event_name != 'pull_request' ||
        (github.event.pull_request.head.repo.full_name == github.repository &&
         github.head_ref != 'claude/stocktake'))
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: shimo4228/harness-stocktake-action@v1
        with:
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          # or: anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
```

`v1` moves to each 1.x release. To pin exactly, use `@v1.0.0` or that release's commit SHA.

## What happens

1. **Audit** (monthly, by hand, or on a pull request that touches `.claude/**` or `CLAUDE.md`). The Action runs `/harness-stocktake:harness-stocktake --project .` through [claude-code-action](https://github.com/anthropics/claude-code-action). The full report appears in the run's summary.
2. **Issue.** The Action keeps one open issue titled **Harness stocktake** and rewrites its body in place on every run. It lists each item to change with a checkbox and shows the date of the last run. Items start unticked. A box you ticked stays ticked in the next report if the same finding comes back.
3. **Tick.** Tick the items you approve, then tick **Apply the ticked items** at the bottom. Items marked ✋ *by hand* edit a path the Action cannot change (see [What it can't change](#what-it-cant-change)). You can tick them, but you apply them yourself.
4. **Pull request.** That edit starts an apply run. It applies only the ticked items, commits them, and force-pushes the result to the fixed branch `claude/stocktake`. Then it opens a pull request, or updates the open one. The pull request splits the approved items in two:
   - **Applied:** merging the branch changes every file the item names.
   - **Pending, apply by hand:** the branch leaves some or all of the item's files alone. Each one comes with the apply run's note and diff, or with the approved change when the run recorded no diff. An item that is only partly on the branch says which files landed and what is left.

   The Action reads the status from the branch itself: the files that merging the branch would change, matched to each item's targets. It does not read it from the model's report. An item that also edits a path the Action can't change (✋) counts as applied only when the apply run recorded it as applied, because its protected part can be missing while the rest landed. The pull request also carries the commits and the plugin's completion report. The tracked issue marks each ticked item ✅ *applied* or ✋ **pending, apply by hand**. The mark stays until the next apply or audit rewrites the issue. Unticking an item is an issue edit that starts no run, so its mark stays visible until then, but the next apply leaves the item out.
5. **Merge.** Merging is the one approval. Pending items are not in it: apply them yourself, on the branch or after the merge. To drop an item, untick it and tick **Apply** again. The branch is rebuilt without it.

Pull request runs only write the report to that run's summary. The tracked issue follows the default branch, so a pull request run leaves it alone.

## What it can't change

The run's deny rules stop Claude Code's file tools (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`) from writing anywhere in the whole `.claude/` directory (`settings.json`, skills, agents, rules and hook scripts), `.mcp.json`, `.claude.json`, `.git`, `.gitmodules`, `.vscode` and `.devcontainer`. They apply wherever those paths are on the runner, the apply run's worktree included. These are most of Claude Code's [protected paths](https://code.claude.com/docs/en/permission-modes#protected-paths). The rules live in the sandbox settings the Action writes, not in `claude_args`, and they hold with permission prompts off (see [Sandbox and permissions](#sandbox-and-permissions)).

| Item | In the Action |
|---|---|
| Edits `CLAUDE.md`, `docs/`, or other files outside the protected paths | Applied on the branch |
| Retires (deletes) a file under `.claude/`, for example a skill or a hook script | ✋ *by hand*. The plugin deletes with `git rm`, which the deny rules don't cover, but the Bash sandbox refuses it there. The pull request lists the item as pending, with the deletion diff |
| Edits a file under `.claude/`, or retires an entry in `.claude/settings.json` (a hook, a permission) | ✋ *by hand*. The pull request lists it as pending, with the diff when the apply run recorded one |

The issue marks the ✋ *by hand* items before you tick anything. The mark is a forecast from the item's target and verdict. The pull request says which items landed.

Shell commands are held by the sandbox instead. Claude Code's sandbox refuses Bash writes to `.git/hooks`, `.git/config`, `.gitmodules` and the checkout's `.claude/` settings, skills and hooks; it lets them write other files in the workspace. Whatever the run writes reaches your default branch only through the pull request you merge.

## Keeping the schedule alive

In a public repository, GitHub disables scheduled workflows after 60 days without repository activity ([GitHub Docs](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), as of 2026-11-02). The tracked issue shows the date of the last run. If that date is more than a month old, start a run from **Actions → Harness stocktake → Run workflow** (`workflow_dispatch`), or re-enable the workflow if GitHub disabled it.

## What it sends, and what it changes

- **Your credential, your account.** The run uses the token or API key you stored. The Action's author never receives a token, a report, or usage data, and the Action has no telemetry. Apart from the setup steps (on Linux, installing bubblewrap and socat from Ubuntu's package mirrors; fetching the plugin from GitHub), the only outside traffic is the Claude Code session itself: it sends your repository content to Anthropic under your credential, the same as running Claude Code locally. GitHub API calls go to your own repository. The model's shell commands reach only PyPI, for the plugin's Python dependencies (see [Sandbox and permissions](#sandbox-and-permissions)). They can still read the job's environment, the run's credentials included, and the run posts what the model writes to the issue and the pull request; a public repository whose harness includes third-party skills is exposed to that through prompt injection.
- **No change without your merge.** The Action pushes to one branch only, `claude/stocktake`, and it never pushes to your default branch. It writes nothing to that branch until you tick items. Nothing reaches the default branch until you merge the pull request.
- **Your repository's own Claude settings apply.** The run is Claude Code inside your repository, so hooks in your committed `.claude/settings.json` run during it. Pull requests from forks are skipped: they get no secrets, and their harness is not yours.
- `claude/stocktake` belongs to the Action, and each apply force-pushes it. Don't commit to it by hand.
- The Action opens the pull request with the workflow's `GITHUB_TOKEN`, so GitHub creates its `pull_request` runs waiting for approval (`action_required`) instead of running them. To run CI on the stocktake pull request, approve those runs, close and reopen the pull request, or push a commit to it yourself.

## Cost and time

On a sample harness (4 skills, 1 rule, 1 agent, 2 hooks), an audit run took about 2 minutes and an apply run about 1.5 minutes on a GitHub-hosted runner (2026-11-01). Cost is not measured yet. A run is one Claude Code session that starts a subagent for each audit source. Cost and duration depend on how big the harness is. Check them on your first run with your own account.

## Inputs

| Input | Default | What it does |
|---|---|---|
| `claude_code_oauth_token` | — | Your Claude Code OAuth token. Set this or `anthropic_api_key`. |
| `anthropic_api_key` | — | Your Anthropic API key. |
| `github_token` | `github.token` | Used for the issue, the push, and the pull request. |
| `mode` | `auto` | `auto`: an `issues` event applies, and any other event audits. Can also be `audit` or `apply`. |
| `plugin_ref` | `v0.1.0` | The harness-stocktake tag, branch, or commit to run. |
| `plugin_repository` | `shimo4228/harness-stocktake` | Where the plugin marketplace lives. |
| `plugin_token` | `github.token` | Read access to `plugin_repository`. Needed only when it is a private repository. |
| `claude_args` | `--permission-mode bypassPermissions` | Passed through to the Claude Code CLI. See [Sandbox and permissions](#sandbox-and-permissions). The deny rules are in the sandbox settings, so setting your own arguments keeps them. |

Outputs: `mode`, `issue_number`, `pr_url`.

### Sandbox and permissions

The plugin's model reads the harness with shell commands such as `for f in .claude/hooks/*.sh; do cat -n "$f"; done`. In a `claude -p` run, Claude Code refuses any command that uses a shell variable before it runs, and no allow rule changes that. So the Action runs the model's Bash in Claude Code's [sandbox](https://code.claude.com/docs/en/sandboxing) and turns permission prompts off inside it:

- **Bash runs in the sandbox.** A step before Claude Code starts prepares it: on Linux it installs `bubblewrap` and `socat` and, on Ubuntu 24.04, lifts the AppArmor limit on user namespaces. macOS uses its built-in Seatbelt. The settings it passes to claude-code-action:
  - writes: the workspace, the temp directory, and `$RUNNER_TEMP/harness-stocktake-uv` (uv's cache and environment; the plugin itself is installed under `~/.claude`, where the sandbox refuses writes). Nothing else, the home directory included.
  - network: `pypi.org` and `files.pythonhosted.org` only, with `strictAllowlist`, so every other host is refused. `allowUnsandboxedCommands: false`, so no command retries outside the sandbox.
- **Prompts are off**: `--permission-mode bypassPermissions`. Allow rules do nothing in this mode, so the default has no `--allowedTools`.
- **Deny rules** in the same settings, which hold in every mode:
  - `WebFetch`, `WebSearch` and every MCP tool (`mcp__*`). They run outside the sandbox, and claude-code-action turns on the servers in your `.mcp.json`.
  - `Edit`, `Write`, `MultiEdit` and `NotebookEdit` on the protected paths (see [What it can't change](#what-it-cant-change)). The file tools aren't sandboxed, so the same four are also denied on the runner's command files (`GITHUB_ENV` and the others), the Action code the later steps run, and the dotfiles in the home directory.

The sandbox is not a wall around your repository: inside the workspace, shell commands can read anything and write everything but the paths above. What stays outside the run's reach is the network (other than PyPI and Claude Code's own connection to Anthropic) and every file outside the workspace and the temp directories. Your repository's own `.claude/settings.json` is loaded too and can widen these settings, as it can add hooks; the Action trusts it as it trusts the rest of your harness. The default branch changes only through the pull request you merge.

If you set `claude_args` yourself, keep `--permission-mode bypassPermissions`: without it the audit stops on permission denials and writes no report.

Needs a GitHub-hosted Linux or macOS runner (`sudo` for the packages on Linux), with `git` and Python 3.11 or later (the plugin's scripts need it, and the sandbox keeps uv from downloading one).

## Design notes

- `plugin_ref` defaults to a plugin release tag. Plugin releases are immutable GitHub releases, so the tag cannot move, and the run logs the commit it fetched.
- `v1` is a plain tag, not a release. `.github/workflows/major-tag.yml` moves it to each published `v1.x.y` release.
- `plugin_marketplaces` in claude-code-action v1 (checked in v1.0.266, the version this Action pins) accepts only an `https://….git` URL or a local path, so `owner/repo` and a `#ref` pin are both rejected. The Action fetches the plugin at `plugin_ref` and passes that local path.
- Ticking an issue box needs a trigger, so the workflow also listens to `issues: edited`. The job runs only when the tracked issue's **Apply** box is ticked.
- Pull request runs report to the run summary only and leave the tracked issue alone (see above).
- The apply run is told it runs in GitHub Actions: the Action pushes the branch and opens the pull request, and the merge stands in for the plugin's ff-only merge. It also writes `.notes/stocktake/<date>/outcomes.json` in the workspace (not on the branch), the per-item outcome, note and diff that the pull request shows for pending items. That file can mark an item pending, but it cannot mark one applied: only the branch can. The pull request body is shortened to fit GitHub's size limit: long diffs, notes and the completion report shrink step by step, and anything they leave out points to the run's summary, which holds every pending item's note, diff and approved change in full. A diff that fits is shown whole and as written, in a code block; one that doesn't is replaced by a note with its length, never shown cut.

## Development

```bash
python3 -m unittest discover -s tests -v
ruff check . && ruff format --check .
actionlint .github/workflows/*.yml examples/*.yml
```

`scripts/stocktake.py` handles the issue and the pull request. It uses only the standard library, and its tests replace the GitHub API and git with fakes.

## License

MIT
