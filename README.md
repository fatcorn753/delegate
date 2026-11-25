# delegate

AI コーディングエージェント(Claude Code など)が、作業の一部を
[Antigravity CLI](https://antigravity.google)(`agy`)や
[GitHub Copilot CLI](https://github.com/github/copilot-cli)(`copilot`)へ
**安全に・安く・記録を残して** 委任するための薄いラッパーです。依存は Python 標準ライブラリだけです。

- **安全装置**: 秘匿ファイル(`.env`、秘密鍵、トークンを含むファイルなど)を見つけたら、委任先ごとの方針に従って
  そのファイルだけを読めなくするか、委任をやめます。指定したリポジトリ配下では一切実行しません。
  `--allow-all` / `--dangerously-skip-permissions` は使いません。
- **共通の出力**: どちらの CLI も、同じ形式(応答・トークン数・クレジット・所要時間)で返します。
- **ログ**: 1 回の呼び出しを JSONL に 1 行ずつ記録します。`delegate stats` でコストと成功率を集計できます。
- **フォールバック**: 読み取り系タスクで一方が失敗したら、もう一方で再実行します。

## タスク

| タスク | 既定の委任先 | フォールバック | 権限 |
| --- | --- | --- | --- |
| `survey` | agy(`gemini-4-argon-medium`) | copilot | 読み取りのみ |
| `summarize` | agy(`gemini-3.8-flash-low`) | copilot | 読み取りのみ |
| `ask` | agy(`gemini-4-argon-medium`) | copilot | 読み取りのみ |
| `gh` | copilot | なし | GitHub の読み取りのみ(ローカルのファイルには触れない) |
| `edit` | copilot | なし | 作業ディレクトリ内のファイル書き込みのみ |
| `implement` | copilot | なし | `edit` と同じ。テストは delegate が回して結果を差し戻す |

モデルの既定値は、tokenguard の比較実験(2026-11-24)で決めました。下調べ・調べものは、設問の正答と引用の検証が満点だった中で、
速い Argon medium にしました。要約は、どのモデルも満点だったので、最も軽い Flash low のままです。
根拠は [tokenguard の README](https://github.com/fatcorn753/tokenguard#モデルと役割の分担2026-11-24) にあります。

権限は次の設定で絞っています。

- **agy の読み取り専用**: `--settings` で `command(*)`・`write_file(*)`・`mcp(*)` を deny にします。
  モデルはファイル読み取りと検索ツールだけで調べます。
- **copilot**: シェルと内容検索のツールを常に外し、`--available-tools` で使えるツールを絞ります。
  読み取りでは `view`・`glob`、編集では `view`・`glob`・`apply_patch` だけです。`--disallow-temp-dir` も付けます。
- **copilot の編集**: 書き込みは許可しますが、シェルは使わせません。
  git 管理下で、追跡ファイルに未コミットの変更がないときだけ実行します。実行後に `git diff --stat` と新しい未追跡ファイルを表示します。
- **implement**: 仕様書(`-f` のファイルか本文)を作業役に渡して実装させます。終わるたびに delegate が `--test` のコマンドを回し、
  失敗したらテスト出力の末尾を同じ session(`--session-id`)へ差し戻します。最大 `--rounds` 回(既定 3)です。
  作業役にはシェルを与えません。シェルがあると、秘匿ファイルの deny を `cat` で抜けられるためです。
  差し戻すテスト出力からは、トークンや秘密鍵の形を `[REDACTED]` に伏せます。前提条件は `edit` と同じです。
  終わったあとに HEAD が動いていたら(作業役が commit したら)失敗として扱います。
  実験用の機能です。5〜7 ファイル規模の比較では、呼び出し側の消費の節約は 12〜20% にとどまり、所要時間は 2.4 倍になりました。
- **gh**: 空の一時ディレクトリで動かし、GitHub MCP の読み取り用ツール(`list_issues` など)だけを許可します。
  リポジトリ名は `-C` の `origin` から渡します。

Copilot のツールをここまで絞るのは、copilot 1.0.114 で次の抜け道を確認したためです。

- `read(path)` で読み取りを禁止しても、検索ツール `rg` で中身を読める。
- `cat` などの読み取り系シェルコマンドは確認なしで動き、作業ディレクトリ外の絶対パスも読める。
- 一時ディレクトリは既定で読める。

## 秘匿ファイルの扱い

作業ディレクトリを走査し、秘匿ファイルを2つの方法で見つけます。

- ファイル名で判定: `secret_globs`(`.env`、`*.pem` など)
- 中身で判定: 秘密鍵・Discord/GitHub/OpenAI/Google/AWS/Slack のトークン、URL に埋め込まれた認証情報

`.git/config` も調べます。見つけたファイルは、`guard.secret_policy` で委任先ごとに扱いを決めます。

| 値 | 動作 |
| --- | --- |
| `mask`(既定) | そのファイルだけを委任先から読めなくする(agy は `read_file(...)`、copilot は `read(...)` / `write(...)` を deny) |
| `allow` | そのまま読ませる |
| `refuse` | その委任先には委任しない(フォールバック先があればそちらで実行) |

`mask` は、走査が上限(`scan_depth`・`max_scan_files`)に達しなかったときだけ使います。上限に達すると、隠し漏れがありうるので委任をやめます。
`delegate check <dir>` で、検出したファイルと各委任先での扱いを確認できます。

## 使い方

```bash
delegate survey "認証まわりの実装箇所をまとめて" -C path/to/repo
delegate summarize -f build.log "失敗の原因を3行で"
delegate ask "Python の tomllib はどのバージョンから標準？"
delegate gh "open な issue をラベル別に要約" -C path/to/repo
delegate edit "foo() を bar() にリネーム" -C path/to/repo
delegate implement -f spec.md -C path/to/repo --test "python3 -m pytest -q" --json
echo "長いプロンプト" | delegate ask -
delegate stats --days 7
delegate check path/to/repo   # 委任できる場所かだけ確認
delegate config               # 有効な設定を表示
```

応答本文は stdout に出ます。所要時間・トークン数などの付帯情報は stderr に出ます。`--json` を付けると、全体を JSON で出力します。

| 終了コード | 意味 |
| --- | --- |
| 0 | 成功 |
| 1 | 委任先の失敗 |
| 2 | 安全装置による拒否 |
| 64 | 使い方の誤り |

## インストール

Python 3.11 以上が必要です。`agy` と `copilot` は、それぞれインストールしてログインしておきます。

```bash
pipx install .        # または: pip install --user .
```

依存がないので、`PYTHONPATH=src python3 -m delegate_cli` でもそのまま動きます。

## 設定

`~/.config/delegate/config.toml` に書いた値が、既定値に上書きされます。置き場所は `DELEGATE_CONFIG` で変えられます。
ログの既定の置き場所は `~/.local/state/delegate/log.jsonl` で、`DELEGATE_LOG` で変えられます。
設定の例は [config.example.toml](config.example.toml) にあります。

## テスト

```bash
python3 -m unittest discover -s tests
```

テストでは偽の `agy` / `copilot`(`tests/fakes/fake_backend.py`)を使うので、実際の CLI は呼びません。

## 制限

- agy の読み取り専用モードではシェルを使えないため、`ls` や `git log` は実行できません。ファイルの読み取りと検索ツールで代わりに調べます。
- 応答形式は、確認時点のバージョン(agy 1.5.12、Copilot CLI 1.0.114)に合わせています。
- 安全装置は委任の入口での確認と、委任先 CLI の権限設定の組み合わせです。委任先の挙動そのものを保証するものではありません。
- 秘匿ファイルの中身による判定はパターンに一致したものだけです。独自形式の秘密情報は `secret_globs` に名前を足してください。
- `implement` の作業役は copilot だけです。agy で書き込みを許すと、headless では編集のあとにターンが終わらず、
  `--print-timeout` まで止まりました(2026-11-21、agy 1.5)。
- `implement` の費用: copilot は同じ session の累計を返すので、ラウンドごとに前回との差分を取って合計します。
- `implement` のテストコマンドは、作業役が書いたコードをそのまま実行します。作業役のコードが秘匿ファイルを読んで出力すれば、
  パターンに一致しない値は差し戻しに混ざりえます。秘匿情報のあるリポジトリでは `--test` の出力に注意してください。
- Copilot は内容検索を使えないため、`survey` のフォールバック先としての調査力は agy より落ちます。
