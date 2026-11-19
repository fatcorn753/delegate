# delegate

AI コーディングエージェント(Claude Code など)が、作業の一部を
[Antigravity CLI](https://antigravity.google)(`agy`)や
[GitHub Copilot CLI](https://github.com/github/copilot-cli)(`copilot`)へ
**安全に・安く・記録を残して** 委任するための薄いラッパーです。依存は Python 標準ライブラリだけです。

- **安全装置**: 指定したリポジトリ配下では実行しません。`.env` や秘密鍵らしきファイルがある場所でも実行しません。
  `--allow-all` / `--dangerously-skip-permissions` は使いません。
- **共通の出力**: どちらの CLI も、同じ形式(応答・トークン数・クレジット・所要時間)で返します。
- **ログ**: 1 回の呼び出しを JSONL に 1 行ずつ記録します。`delegate stats` でコストと成功率を集計できます。
- **フォールバック**: 読み取り系タスクで一方が失敗したら、もう一方で再実行します。

## タスク

| タスク | 既定の委任先 | フォールバック | 権限 |
| --- | --- | --- | --- |
| `survey` | agy | copilot | 読み取りのみ |
| `summarize` | agy(`gemini-3.8-flash-low`) | copilot | 読み取りのみ |
| `ask` | agy | copilot | 読み取りのみ |
| `gh` | copilot | なし | 読み取りのみ(GitHub 連携) |
| `edit` | copilot | なし | 作業ディレクトリ内のファイル書き込みのみ |

権限は次の設定で絞っています。

- **agy の読み取り専用**: `--settings` で `command(*)`・`write_file(*)`・`mcp(*)` を deny にします。
  モデルはファイル読み取りと検索ツールだけで調べます。
- **copilot の読み取り**: `--deny-tool write` を付けます。非対話モードでは、許可ルールのないシェルの変更系コマンドが自動で拒否されます。
- **copilot の編集**: `--allow-tool write` だけを許可し、`git commit` / `git push` は deny にします。
  git 管理下で、追跡ファイルに未コミットの変更がないときだけ実行します。実行後に `git diff --stat` と新しい未追跡ファイルを表示します。

## 使い方

```bash
delegate survey "認証まわりの実装箇所をまとめて" -C path/to/repo
delegate summarize -f build.log "失敗の原因を3行で"
delegate ask "Python の tomllib はどのバージョンから標準？"
delegate gh "open な issue をラベル別に要約" -C path/to/repo
delegate edit "foo() を bar() にリネーム" -C path/to/repo
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
- 安全装置は委任の入口での確認です。委任先の挙動そのものを保証するものではありません。
