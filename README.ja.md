[English](README.md) | [日本語](README.ja.md)

# harness-stocktake-action

リポジトリの Claude Code ハーネス（`.claude/` の skills・rules・hooks・agents・settings と `CLAUDE.md`）に [harness-stocktake](https://github.com/shimo4228/harness-stocktake)（英語）を月に一度、そしてプルリクエストがハーネスを変えるたびに走らせます。指摘は 1 つの追跡用 issue にまとまります。承認する項目にチェックを入れると、Action がそれらを 1 つのブランチに適用し、プルリクエストを 1 つ開きます。そのプルリクエストをマージすることが承認になります。

## はじめかた

1. 自分の Claude の認証情報をリポジトリのシークレットに保存します。

   ```bash
   claude setup-token | gh secret set CLAUDE_CODE_OAUTH_TOKEN
   ```

   `setup-token` がトークン以外のものも出力するときは、`claude setup-token` を実行してトークンをコピーし、`gh secret set CLAUDE_CODE_OAUTH_TOKEN` に貼り付けます。代わりに API キーを使うときは `gh secret set ANTHROPIC_API_KEY` を実行し、ワークフローのコメントアウトされた行と入れ替えます。

2. ワークフローをコピーします。

   ```bash
   mkdir -p .github/workflows
   curl -sSfL https://raw.githubusercontent.com/shimo4228/harness-stocktake-action/main/examples/harness-stocktake.yml -o .github/workflows/harness-stocktake.yml
   ```

3. ワークフローがプルリクエストを開けるように、**Settings → Actions → General → Workflow permissions → Allow GitHub Actions to create and approve pull requests** を有効にします。

4. コミットしてプッシュし、**Actions → Harness stocktake → Run workflow** から最初の実行を始めます。

[`examples/harness-stocktake.yml`](examples/harness-stocktake.yml) のワークフローです。

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

`v1` は 1.x のリリースが出るたびにそこへ移ります。版を厳密に固定するときは `@v1.0.0` か、そのリリースのコミット SHA を使います。

## 何が起きるか

1. **監査**（毎月、手動、または `.claude/**` か `CLAUDE.md` に触れるプルリクエストで）。Action は [claude-code-action](https://github.com/anthropics/claude-code-action)（英語）を通して `/harness-stocktake:harness-stocktake --project .` を実行します。レポートの全文は、その実行のサマリーに出ます。
2. **issue。** Action は **Harness stocktake** という題の issue を 1 つだけ開いたままにし、実行のたびにその本文を書き換えます。本文には変更する項目がチェックボックス付きで並び、最後に実行した日付も載ります。項目は最初はチェックが外れています。チェックを入れた項目は、次のレポートで同じ指摘がまた出れば、チェックが入ったまま残ります。
3. **チェック。** 承認する項目にチェックを入れ、最後に一番下の **Apply the ticked items**（チェックした項目を適用）にチェックを入れます。✋ *by hand*（手作業）の印が付いた項目は、Action が変更できないパスを編集するものです（[変えられないもの](#変えられないもの) を参照）。チェックは入れられますが、適用は自分で行います。
4. **プルリクエスト。** その編集で適用の実行（apply run）が始まります。適用の実行はチェックした項目だけを適用してコミットし、その結果を固定のブランチ `claude/stocktake` に force push します。そのうえでプルリクエストを開くか、開いているものを更新します。プルリクエストは承認された項目を 2 つに分けます。
   - **Applied**（適用済み）: ブランチをマージすると、その項目が名指すファイルがすべて変わります。
   - **Pending, apply by hand**（保留、手作業で適用）: ブランチは、その項目のファイルの一部またはすべてに手を付けていません。各項目には適用の実行が残したメモと diff が付き、diff が記録されなかったときは承認された変更が付きます。一部だけがブランチに入った項目は、どのファイルが入り、何が残っているかを示します。

   Action はこの状態をブランチそのものから読み取ります。ブランチをマージすると変わるファイルを、各項目の対象と突き合わせます。モデルのレポートからは読み取りません。Action が変更できないパス（✋）も編集する項目は、適用の実行が適用済みと記録したときだけ適用済みに数えます。残りの部分が入っても、保護された部分が欠けていることがあるからです。プルリクエストにはコミットと plugin の完了報告も載ります。追跡用 issue では、チェックした各項目に ✅ *applied*（適用済み）か ✋ **pending, apply by hand**（保留、手作業で適用）の印が付きます。印は、次の適用か監査が issue を書き換えるまで残ります。項目のチェックを外すのは実行を始めない issue の編集なので、それまでは印が見えたままですが、次の適用ではその項目が外れます。
5. **マージ。** マージが唯一の承認です。保留の項目はそこに含まれないので、ブランチの上かマージのあとに自分で適用します。項目を取り下げるときは、チェックを外してもう一度 **Apply** にチェックを入れます。ブランチはその項目を除いて作り直されます。

プルリクエストでの実行は、その実行のサマリーにレポートを書くだけです。追跡用 issue はデフォルトブランチの状態を映すので、プルリクエストでの実行は issue に触れません。

## 変えられないもの

実行時の deny ルールは、Claude Code のファイルツール（`Edit`、`Write`、`MultiEdit`、`NotebookEdit`）が、`.claude/` ディレクトリ全体（`settings.json`、skills、agents、rules、hook スクリプト）、`.mcp.json`、`.claude.json`、`.git`、`.gitmodules`、`.vscode`、`.devcontainer` のどこにも書き込めないようにします。これらのパスがランナーのどこにあっても効き、その範囲には適用の実行の worktree も入ります。Claude Code の[保護されたパス](https://code.claude.com/docs/en/permission-modes#protected-paths)（英語）の大部分にあたります。ルールは `claude_args` ではなく Action が書き出すサンドボックスの設定にあり、許可の確認を切っていても効きます（[サンドボックスと権限](#サンドボックスと権限) を参照）。

| 項目 | Action での扱い |
|---|---|
| `CLAUDE.md`、`docs/`、ほか保護されたパスの外のファイルを編集する | ブランチに適用 |
| `.claude/` の下のファイル（skill や hook スクリプトなど）を退役させる（削除する） | ✋ *by hand*。plugin は `git rm` で削除し、これは deny ルールの対象外だが、そこでは Bash のサンドボックスが拒否する。プルリクエストは削除の diff を付けて保留として載せる |
| `.claude/` の下のファイルを編集する、または `.claude/settings.json` のエントリ（hook、permission）を退役させる | ✋ *by hand*。プルリクエストは保留として載せ、適用の実行が diff を記録していればそれも付ける |

issue は、チェックを入れる前から ✋ *by hand* の項目に印を付けます。この印は項目の対象と判定から立てた見込みです。どの項目が入ったかはプルリクエストが示します。

シェルのコマンドは、代わりにサンドボックスが押さえます。Claude Code のサンドボックスは、`.git/hooks`、`.git/config`、`.gitmodules`、チェックアウトの `.claude/` の settings・skills・hooks への Bash の書き込みを拒否し、ワークスペースのほかのファイルへの書き込みは許します。実行が何を書いても、デフォルトブランチに届くのは自分がマージするプルリクエストを通してだけです。

## 定期実行を止めないために

公開リポジトリでは、60 日間動きが無いと GitHub が定期実行のワークフローを無効にします（[GitHub Docs](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)（英語）、2026-11-02 時点）。追跡用 issue には最後に実行した日付が出ます。その日付が 1 か月より前なら、**Actions → Harness stocktake → Run workflow**（`workflow_dispatch`）から実行を始めるか、GitHub がワークフローを無効にしていれば有効に戻します。

## 何を送り、何を変えるか

- **自分の認証情報で、自分のアカウントで動きます。** 実行には自分が保存したトークンか API キーを使います。Action の作者がトークン・レポート・利用データを受け取ることはなく、Action にテレメトリーはありません。セットアップの手順（Linux では Ubuntu のパッケージミラーから bubblewrap と socat を入れること、GitHub から plugin を取ってくること）を除けば、外への通信は Claude Code のセッションそのものだけで、手元で Claude Code を動かすときと同じく、自分の認証情報でリポジトリの中身を Anthropic に送ります。GitHub API の呼び出しは自分のリポジトリに向かいます。モデルのシェルコマンドが届くのは、plugin の Python 依存のための PyPI だけです（[サンドボックスと権限](#サンドボックスと権限) を参照）。それでもシェルコマンドはジョブの環境を読めて、そこには実行の認証情報も含まれます。また実行は、モデルが書いたものを issue とプルリクエストに投稿します。ハーネスにサードパーティの skill を含む公開リポジトリは、プロンプトインジェクションを通じてこの危険にさらされます。
- **自分がマージしない限り何も変わりません。** Action がプッシュするのは `claude/stocktake` という 1 つのブランチだけで、デフォルトブランチには決してプッシュしません。項目にチェックを入れるまで、そのブランチにも何も書きません。プルリクエストをマージするまで、デフォルトブランチには何も届きません。
- **リポジトリ自身の Claude の設定が効きます。** 実行はリポジトリの中で動く Claude Code なので、コミットされた `.claude/settings.json` の hooks が実行中に走ります。フォークからのプルリクエストは飛ばします。フォークにはシークレットが渡らず、そのハーネスは自分のものではないからです。
- `claude/stocktake` は Action のもので、適用のたびに force push されます。手でコミットしないでください。
- Action はワークフローの `GITHUB_TOKEN` でプルリクエストを開くので、GitHub はそのプルリクエストの `pull_request` の実行を走らせず、承認待ち（`action_required`）として作ります。棚卸しのプルリクエストで CI を走らせるには、その実行を承認するか、プルリクエストを閉じて開き直すか、自分でコミットをプッシュします。

## 費用と時間

サンプルのハーネス（skill 4 件、rule 1 件、agent 1 件、hook 2 件）では、GitHub ホストのランナーで監査の実行が約 2 分、適用の実行が約 1.5 分かかりました（2026-11-01）。費用はまだ測っていません。1 回の実行は 1 つの Claude Code セッションで、監査の情報源ごとにサブエージェントを 1 つ起動します。費用と所要時間はハーネスの大きさで変わります。最初の実行のときに、自分のアカウントで確かめてください。

## 入力

| 入力 | 既定値 | 役割 |
|---|---|---|
| `claude_code_oauth_token` | — | 自分の Claude Code の OAuth トークン。これか `anthropic_api_key` を設定する。 |
| `anthropic_api_key` | — | 自分の Anthropic API キー。 |
| `github_token` | `github.token` | issue、プッシュ、プルリクエストに使う。 |
| `mode` | `auto` | `auto`: `issues` イベントなら適用し、それ以外のイベントなら監査する。`audit` か `apply` も指定できる。 |
| `plugin_ref` | `v0.1.0` | 実行する harness-stocktake のタグ、ブランチ、またはコミット。 |
| `plugin_repository` | `shimo4228/harness-stocktake` | plugin の marketplace がある場所。 |
| `plugin_token` | `github.token` | `plugin_repository` の読み取り権限。それが非公開リポジトリのときだけ必要。 |
| `claude_args` | `--permission-mode bypassPermissions` | Claude Code の CLI にそのまま渡す。[サンドボックスと権限](#サンドボックスと権限) を参照。deny ルールはサンドボックスの設定にあるので、自分で引数を設定しても残る。 |

出力: `mode`、`issue_number`、`pr_url`。

### サンドボックスと権限

plugin のモデルは、`for f in .claude/hooks/*.sh; do cat -n "$f"; done` のようなシェルコマンドでハーネスを読みます。`claude -p` の実行では、Claude Code はシェル変数を使うコマンドを実行前にすべて拒否し、allow ルールではこれを変えられません。そこで Action は、モデルの Bash を Claude Code の[サンドボックス](https://code.claude.com/docs/en/sandboxing)（英語）で動かし、その中では許可の確認を切ります。

- **Bash はサンドボックスで動きます。** Claude Code が起動する前のステップで準備します。Linux では `bubblewrap` と `socat` を入れ、Ubuntu 24.04 ではユーザー名前空間への AppArmor の制限を外します。macOS は組み込みの Seatbelt を使います。claude-code-action に渡す設定は次のとおりです。
  - 書き込み: ワークスペース、一時ディレクトリ、`$RUNNER_TEMP/harness-stocktake-uv`（uv のキャッシュと環境。plugin 自体は `~/.claude` の下に入り、そこへの書き込みはサンドボックスが拒否します）。ほかはホームディレクトリも含めて書けません。
  - ネットワーク: `pypi.org` と `files.pythonhosted.org` だけで、`strictAllowlist` によりほかのホストはすべて拒否します。`allowUnsandboxedCommands: false` なので、サンドボックスの外でコマンドをやり直すことはありません。
- **確認は切ります**: `--permission-mode bypassPermissions`。このモードでは allow ルールが何もしないので、既定値に `--allowedTools` はありません。
- **deny ルール**は同じ設定にあり、どのモードでも効きます。
  - `WebFetch`、`WebSearch`、すべての MCP ツール（`mcp__*`）。これらはサンドボックスの外で動き、claude-code-action は `.mcp.json` のサーバーを有効にするからです。
  - 保護されたパスへの `Edit`、`Write`、`MultiEdit`、`NotebookEdit`（[変えられないもの](#変えられないもの) を参照）。ファイルツールはサンドボックスに入らないので、同じ 4 つのツールは、ランナーのコマンドファイル（`GITHUB_ENV` など）、後続のステップが実行する Action のコード、ホームディレクトリのドットファイルにも使えないようにしています。

サンドボックスはリポジトリを囲む壁ではありません。ワークスペースの中では、シェルコマンドは何でも読めて、上のパス以外はすべて書けます。実行の手が届かないのは、ネットワーク（PyPI と、Claude Code 自身の Anthropic への接続を除く）と、ワークスペースと一時ディレクトリの外のファイルです。リポジトリ自身の `.claude/settings.json` も読み込まれ、hooks を足せるのと同じく、これらの設定を広げられます。Action はハーネスのほかの部分と同じようにそれを信頼します。デフォルトブランチが変わるのは、自分がマージするプルリクエストを通してだけです。

`claude_args` を自分で設定するときは `--permission-mode bypassPermissions` を残してください。これが無いと、監査が許可の拒否で止まり、レポートを書きません。

GitHub ホストの Linux か macOS のランナーが必要で（Linux ではパッケージのために `sudo`）、`git` と Python 3.11 以降も要ります（plugin のスクリプトが必要とし、サンドボックスが uv にダウンロードさせないため）。

## 設計メモ

- `plugin_ref` の既定値は plugin のリリースタグです。plugin のリリースは変更できない GitHub リリースなのでタグは動かず、実行は取得したコミットをログに残します。
- `v1` はただのタグで、リリースではありません。`.github/workflows/major-tag.yml` が、公開された `v1.x.y` のリリースのたびにそこへ動かします。
- claude-code-action v1 の `plugin_marketplaces` は、`https://….git` の URL かローカルのパスしか受け付けません（この Action が固定している v1.0.266 で確認）。そのため `owner/repo` も `#ref` での固定も拒否されます。Action は `plugin_ref` の plugin を取得し、そのローカルのパスを渡します。
- issue のボックスにチェックを入れたことを拾うにはトリガーが要るので、ワークフローは `issues: edited` も待ち受けます。ジョブが走るのは、追跡用 issue の **Apply** ボックスにチェックが入っているときだけです。
- プルリクエストでの実行は実行のサマリーにだけ報告し、追跡用 issue には触れません（上を参照）。
- 適用の実行には GitHub Actions で動いていることを伝えます。Action がブランチをプッシュしてプルリクエストを開き、plugin の ff-only マージの代わりをマージが務めます。適用の実行は、ワークスペースに（ブランチにではなく）`.notes/stocktake/<date>/outcomes.json` も書きます。これは項目ごとの結果・メモ・diff で、プルリクエストが保留の項目に表示する中身です。このファイルは項目を保留にはできますが、適用済みにはできません。それができるのはブランチだけです。プルリクエストの本文は GitHub のサイズ上限に収まるように短くします。長い diff、メモ、完了報告を段階的に縮め、省いた箇所には実行のサマリーへの案内を置きます。サマリーには、保留の各項目のメモ・diff・承認された変更が全部載っています。収まる diff はそのままの形でコードブロックに丸ごと載せ、収まらないものは長さを記したメモに置き換えます。途中で切って載せることはありません。

## 開発

```bash
python3 -m unittest discover -s tests -v
ruff check . && ruff format --check .
actionlint .github/workflows/*.yml examples/*.yml
```

`scripts/stocktake.py` が issue とプルリクエストを扱います。標準ライブラリだけを使い、テストでは GitHub API と git を偽物に差し替えます。

## ライセンス

MIT
