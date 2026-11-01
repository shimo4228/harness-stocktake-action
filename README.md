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

## What happens

1. **Audit** (monthly, by hand, or on a pull request that touches `.claude/**` or `CLAUDE.md`). The Action runs `/harness-stocktake:harness-stocktake --project .` through [claude-code-action](https://github.com/anthropics/claude-code-action). The full report appears in the run's summary.
2. **Issue.** The Action keeps one open issue titled **Harness stocktake** and rewrites its body in place on every run. It lists each item to change with a checkbox and shows the date of the last run. Items start unticked. A box you ticked stays ticked in the next report if the same finding comes back.
3. **Tick.** Tick the items you approve, then tick **Apply the ticked items** at the bottom.
4. **Pull request.** That edit starts an apply run. It applies only the ticked items, commits them, and force-pushes the result to the fixed branch `claude/stocktake`. Then it opens a pull request, or updates the open one. The pull request lists the approved items, the commits, and the plugin's completion report, including any items it left pending.
5. **Merge.** Merging is the one approval. To drop an item, untick it and tick **Apply** again. The branch is rebuilt without it.

Pull request runs only write the report to that run's summary. The tracked issue follows the default branch, so a pull request run leaves it alone.

## Keeping the schedule alive

GitHub pauses scheduled workflows after 60 days without repository activity. The tracked issue shows the date of the last run. If that date is more than a month old, start a run from **Actions → Harness stocktake → Run workflow** (`workflow_dispatch`), or re-enable the workflow if GitHub disabled it.

## What it sends, and what it changes

- **Your credential, your account.** The run uses the token or API key you stored. The Action's author never receives a token, a report, or usage data, and the Action has no telemetry. The only outside traffic is the Claude Code session itself: it sends your repository content to Anthropic under your credential, the same as running Claude Code locally. GitHub API calls go to your own repository.
- **No change without your merge.** The Action pushes to one branch only, `claude/stocktake`, and it never pushes to your default branch. It writes nothing to that branch until you tick items. Nothing reaches the default branch until you merge the pull request.
- **Your repository's own Claude settings apply.** The run is Claude Code inside your repository, so hooks in your committed `.claude/settings.json` run during it. Pull requests from forks are skipped: they get no secrets, and their harness is not yours.
- `claude/stocktake` belongs to the Action, and each apply force-pushes it. Don't commit to it by hand.
- Pull requests opened with the workflow's `GITHUB_TOKEN` don't trigger other workflows. To run CI on the stocktake pull request, close and reopen it, or push an empty commit to it yourself.

## Cost and time

**Not measured yet.** A run is one Claude Code session that starts a subagent for each audit source. Cost and duration depend on how big the harness is. Check them on your first run with your own account.

## Inputs

| Input | Default | What it does |
|---|---|---|
| `claude_code_oauth_token` | — | Your Claude Code OAuth token. Set this or `anthropic_api_key`. |
| `anthropic_api_key` | — | Your Anthropic API key. |
| `github_token` | `github.token` | Used for the issue, the push, and the pull request. |
| `mode` | `auto` | `auto`: an `issues` event applies, and any other event audits. Can also be `audit` or `apply`. |
| `plugin_ref` | `v0.1.0` | The harness-stocktake tag, branch, or commit to run. |
| `plugin_repository` | `shimo4228/harness-stocktake` | Where the plugin marketplace lives. |
| `plugin_token` | `github.token` | Read access to `plugin_repository`. Only needed while that repository is private. |
| `claude_args` | allowed tools for the plugin | Passed through to the Claude Code CLI. |

Outputs: `mode`, `issue_number`, `pr_url`.

Needs a Linux or macOS runner, with `git` and Python 3.10 or later. GitHub-hosted runners have both.

## Differences from the plan

- `plugin_marketplaces` in claude-code-action v1 accepts only an `https://….git` URL or a local path, so `owner/repo` and a `#ref` pin are both rejected. The Action fetches the plugin at `plugin_ref` and passes that local path.
- Ticking an issue box needs a trigger, so the workflow also listens to `issues: edited`. The job runs only when the tracked issue's **Apply** box is ticked.
- Pull request runs report to the run summary only and leave the tracked issue alone (see above).

## Development

```bash
python3 -m unittest discover -s tests -v
ruff check . && ruff format --check .
actionlint .github/workflows/*.yml examples/*.yml
```

`scripts/stocktake.py` handles the issue and the pull request. It uses only the standard library, and its tests replace the GitHub API and git with fakes.

## License

MIT
