import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from delegate_cli import backends, cli, guard, runlog  # noqa: E402
from delegate_cli.config import DEFAULTS  # noqa: E402

FAKE = ROOT / "tests" / "fakes" / "fake_backend.py"


class Env(unittest.TestCase):
    """偽バックエンドと一時設定で cli.main を動かす土台。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for n in ("agy", "copilot"):
            (self.bin / n).symlink_to(FAKE)
        os.chmod(FAKE, 0o755)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.secret_repo = self.tmp / "secret_repo"
        self.secret_repo.mkdir()
        self.cfg = self.tmp / "config.toml"
        self.cfg.write_text(
            f'[guard]\ndeny_paths = ["{self.secret_repo}"]\n'
            f'[backends.agy]\ncommand = "{self.bin / "agy"}"\ntimeout = 30\n'
            f'[backends.copilot]\ncommand = "{self.bin / "copilot"}"\ntimeout = 30\n'
        )
        self.log = self.tmp / "log.jsonl"
        self.argv_log = self.tmp / "argv.jsonl"
        self.env = mock.patch.dict(os.environ, {
            "DELEGATE_CONFIG": str(self.cfg),
            "DELEGATE_LOG": str(self.log),
            "FAKE_ARGV_LOG": str(self.argv_log),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_cli(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def calls(self):
        if not self.argv_log.exists():
            return []
        return [json.loads(l) for l in self.argv_log.read_text().splitlines()]

    def logged(self):
        return runlog.read(self.log)


class TestRun(Env):
    def test_survey_uses_agy_readonly(self):
        code, out, err = self.run_cli("survey", "what is here", "-C", str(self.work))
        self.assertEqual(code, 0, err)
        self.assertIn("agy answer", out)
        [call] = self.calls()
        self.assertEqual(call["name"], "agy")
        argv = call["argv"]
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertIn("command(*)", settings["permissions"]["deny"])
        self.assertIn("write_file(*)", settings["permissions"]["deny"])
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(Path(call["cwd"]).resolve(), self.work.resolve())
        [rec] = self.logged()
        self.assertEqual((rec["backend"], rec["task"], rec["ok"]), ("agy", "survey", True))
        self.assertEqual((rec["tokens_in"], rec["tokens_out"]), (100, 15))

    def test_survey_includes_file_listing(self):
        (self.work / "src").mkdir()
        (self.work / "src" / "main.py").write_text("x")
        (self.work / "node_modules" / "p").mkdir(parents=True)
        (self.work / "node_modules" / "p" / "i.js").write_text("x")
        (self.work / ".git").mkdir()
        (self.work / ".git" / "HEAD").write_text("x")
        self.run_cli("survey", "q", "-C", str(self.work))
        argv = self.calls()[0]["argv"]
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("src/main.py", prompt)
        self.assertNotIn("node_modules", prompt)
        self.assertNotIn(".git/HEAD", prompt)
        self.run_cli("ask", "q", "-C", str(self.work))
        argv = self.calls()[1]["argv"]
        self.assertNotIn("Files in the working directory", argv[argv.index("-p") + 1])

    def test_survey_and_ask_default_to_argon_medium(self):
        for task in ("survey", "ask"):
            code, out, err = self.run_cli(task, "q", "-C", str(self.work))
            self.assertEqual(code, 0, err)
            argv = self.calls()[-1]["argv"]
            self.assertEqual(argv[argv.index("--model") + 1], "gemini-4-argon-medium")

    def test_summarize_passes_flash_model_and_files(self):
        f = self.work / "notes.txt"
        f.write_text("hello")
        code, out, err = self.run_cli("summarize", "-f", str(f), "-C", str(self.work))
        self.assertEqual(code, 0, err)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-low")
        self.assertIn(str(f.resolve()), argv[argv.index("-p") + 1])

    def test_file_outside_cwd_is_added_as_dir(self):
        other = self.tmp / "other"
        other.mkdir()
        f = other / "a.log"
        f.write_text("x")
        self.run_cli("summarize", "-f", str(f), "-C", str(self.work))
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(other.resolve()))

    def test_fallback_to_copilot_when_agy_fails(self):
        code, out, err = self.run_cli("ask", "q", "-C", str(self.work), env={"FAKE_MODE_AGY": "fail"})
        self.assertEqual(code, 0, err)
        self.assertIn("copilot answer", out)
        self.assertIn("falling back to copilot", err)
        names = [c["name"] for c in self.calls()]
        self.assertEqual(names, ["agy", "copilot"])
        cp_argv = self.calls()[1]["argv"]
        self.assertIn(["--deny-tool", "write"], [cp_argv[i:i + 2] for i in range(len(cp_argv))])
        self.assertIn(["--deny-tool", "shell"], [cp_argv[i:i + 2] for i in range(len(cp_argv))])
        self.assertIn("--disallow-temp-dir", cp_argv)
        at = cp_argv.index("--available-tools")
        self.assertEqual(cp_argv[at + 1:at + 3], ["view", "glob"])
        self.assertNotIn("--model", cp_argv)  # agy 用のモデル名を copilot に渡さない
        recs = self.logged()
        self.assertEqual([r["ok"] for r in recs], [False, True])
        self.assertEqual(recs[1]["fallback_of"], "agy")

    def test_model_override_not_passed_to_fallback(self):
        code, _, err = self.run_cli("ask", "q", "-C", str(self.work), "--model", "agy-only",
                                    env={"FAKE_MODE_AGY": "fail"})
        self.assertEqual(code, 0, err)
        agy_argv, cp_argv = self.calls()[0]["argv"], self.calls()[1]["argv"]
        self.assertEqual(agy_argv[agy_argv.index("--model") + 1], "agy-only")
        self.assertNotIn("--model", cp_argv)
        self.assertIn("(quota exceeded)", err)

    def test_no_fallback_flag(self):
        code, _, _ = self.run_cli("ask", "q", "-C", str(self.work), "--no-fallback", env={"FAKE_MODE_AGY": "fail"})
        self.assertEqual(code, 1)
        self.assertEqual(len(self.calls()), 1)

    def test_both_fail(self):
        code, _, err = self.run_cli("ask", "q", "-C", str(self.work), env={"FAKE_MODE": "fail"})
        self.assertEqual(code, 1)
        self.assertIn("FAILED", err)

    def test_garbage_output_is_failure(self):
        code, _, _ = self.run_cli("ask", "q", "-C", str(self.work), "--no-fallback", env={"FAKE_MODE": "garbage"})
        self.assertEqual(code, 1)

    def test_json_output(self):
        code, out, _ = self.run_cli("gh", "list issues", "-C", str(self.work), "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual((d["backend"], d["text"], d["credits"]), ("copilot", "copilot answer", 0.25))
        self.assertEqual(d["attempts"], ["copilot"])

    def test_missing_command_reports_error(self):
        self.cfg.write_text(self.cfg.read_text().replace(str(self.bin / "agy"), "/nonexistent/agy"))
        code, _, err = self.run_cli("survey", "q", "-C", str(self.work), "--no-fallback")
        self.assertEqual(code, 1)
        self.assertIn("command not found", err)

    def test_empty_prompt_is_usage_error(self):
        code, _, _ = self.run_cli("ask", "", "-C", str(self.work))
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_stdin_prompt(self):
        with mock.patch("sys.stdin", io.StringIO("from stdin")):
            code, _, _ = self.run_cli("ask", "-", "-C", str(self.work))
        self.assertEqual(code, 0)
        self.assertIn("from stdin", self.calls()[0]["argv"][1])


class TestGuard(Env):
    def test_denied_path_refused_without_calling_backend(self):
        code, _, err = self.run_cli("survey", "q", "-C", str(self.secret_repo))
        self.assertEqual(code, cli.EXIT_GUARD)
        self.assertIn("denied path", err)
        self.assertEqual(self.calls(), [])

    def test_denied_subdir_and_symlink(self):
        sub = self.secret_repo / "a" / "b"
        sub.mkdir(parents=True)
        link = self.tmp / "link"
        link.symlink_to(sub)
        for p in (sub, link):
            code, _, _ = self.run_cli("survey", "q", "-C", str(p))
            self.assertEqual(code, cli.EXIT_GUARD, p)

    def test_unicode_normalization(self):
        import unicodedata
        nfd = self.tmp / unicodedata.normalize("NFD", "ダウンロード")
        nfd.mkdir()
        self.cfg.write_text(self.cfg.read_text().replace(
            f'deny_paths = ["{self.secret_repo}"]',
            f'deny_paths = ["{self.tmp / unicodedata.normalize("NFC", "ダウンロード")}"]'))
        code, _, _ = self.run_cli("survey", "q", "-C", str(nfd))
        self.assertEqual(code, cli.EXIT_GUARD)

    def test_sibling_with_same_prefix_is_allowed(self):
        sib = self.tmp / "secret_repo2"
        sib.mkdir()
        code, _, _ = self.run_cli("survey", "q", "-C", str(sib))
        self.assertEqual(code, 0)

    def test_file_in_denied_path_refused(self):
        f = self.secret_repo / "x.txt"
        f.write_text("x")
        code, _, _ = self.run_cli("summarize", "-f", str(f), "-C", str(self.work))
        self.assertEqual(code, cli.EXIT_GUARD)

    def test_secret_file_masked_for_agy(self):
        (self.work / "deep" / "er").mkdir(parents=True)
        env = self.work / "deep" / "er" / ".env"
        env.write_text("TOKEN=x")
        code, _, err = self.run_cli("survey", "q", "-C", str(self.work))
        self.assertEqual(code, 0, err)
        self.assertIn("hiding 1 secret-like file", err)
        argv = self.calls()[0]["argv"]
        deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
        self.assertIn(f"read_file({env.resolve()})", deny)

    def test_secret_file_masked_for_copilot(self):
        env = self.work / ".env"
        env.write_text("TOKEN=x")
        code, _, err = self.run_cli("ask", "q", "-C", str(self.work), "--backend", "copilot")
        self.assertEqual(code, 0, err)
        argv = self.calls()[0]["argv"]
        self.assertIn(f"read({env.resolve()})", argv)
        self.assertIn(f"write({env.resolve()})", argv)

    def test_secret_detected_by_content(self):
        bak = self.work / "config.json.migrated-to-server"
        bak.write_text('{"token": "MTA' + "x" * 23 + '.GaBcDe.' + "y" * 30 + '"}')
        key = self.work / "notes.txt"
        key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
        ok = self.work / "plain.json"
        ok.write_text('{"token": "<your token here>"}')
        found = guard.find_secrets(self.work, DEFAULTS["guard"]).secrets
        self.assertEqual(sorted(f.name for f in found), ["config.json.migrated-to-server", "notes.txt"])

    def test_git_config_with_credential_url(self):
        (self.work / ".git").mkdir()
        (self.work / ".git" / "config").write_text('[remote "origin"]\n\turl = https://me:ghp_' + "a" * 36 + "@github.com/x/y\n")
        found = guard.find_secrets(self.work, DEFAULTS["guard"]).secrets
        self.assertEqual([f.name for f in found], ["config"])

    def test_policy_allow_passes_no_mask(self):
        (self.work / ".env").write_text("TOKEN=x")
        self.cfg.write_text(self.cfg.read_text().replace("[guard]\n", '[guard]\nsecret_policy = {agy = "allow", copilot = "mask"}\n'))
        code, _, err = self.run_cli("survey", "q", "-C", str(self.work))
        self.assertEqual(code, 0, err)
        argv = self.calls()[0]["argv"]
        deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
        self.assertFalse(any(d.startswith("read_file(") for d in deny))
        self.assertNotIn("hiding", err)

    def test_policy_refuse_falls_back(self):
        (self.work / ".env").write_text("TOKEN=x")
        self.cfg.write_text(self.cfg.read_text().replace("[guard]\n", '[guard]\nsecret_policy = {agy = "refuse", copilot = "mask"}\n'))
        code, out, err = self.run_cli("ask", "q", "-C", str(self.work))
        self.assertEqual(code, 0, err)
        self.assertEqual([c["name"] for c in self.calls()], ["copilot"])
        self.assertIn("copilot answer", out)

    def test_all_refused_is_guard_exit(self):
        (self.work / ".env").write_text("TOKEN=x")
        self.cfg.write_text(self.cfg.read_text().replace("[guard]\n", '[guard]\nsecret_policy = {agy = "refuse", copilot = "refuse"}\n'))
        code, _, err = self.run_cli("ask", "q", "-C", str(self.work))
        self.assertEqual(code, cli.EXIT_GUARD)
        self.assertEqual(self.calls(), [])
        self.assertIn("refuse", err)

    def test_example_env_and_skipped_dirs_allowed(self):
        (self.work / ".env.example").write_text("TOKEN=")
        (self.work / "node_modules" / "pkg").mkdir(parents=True)
        (self.work / "node_modules" / "pkg" / "key.pem").write_text("x")
        code, _, err = self.run_cli("survey", "q", "-C", str(self.work))
        self.assertEqual(code, 0, err)

    def test_incomplete_scan_refuses_mask(self):
        d = self.work / "a" / "b" / "c" / "d"
        d.mkdir(parents=True)
        (d / ".env").write_text("x")
        self.cfg.write_text(self.cfg.read_text().replace("[guard]\n", "[guard]\nscan_depth = 2\n"))
        scan = guard.find_secrets(self.work, {**DEFAULTS["guard"], "scan_depth": 2})
        self.assertFalse(scan.complete)
        self.assertEqual(scan.secrets, [])
        code, _, err = self.run_cli("ask", "q", "-C", str(self.work))
        self.assertEqual(code, cli.EXIT_GUARD)
        self.assertIn("limit", err)
        self.assertEqual(self.calls(), [])

    def test_check_command(self):
        code, out, _ = self.run_cli("check", str(self.secret_repo))
        self.assertEqual(code, cli.EXIT_GUARD)
        (self.work / ".env").write_text("TOKEN=x")
        code, out, _ = self.run_cli("check", str(self.work))
        self.assertEqual(code, 0)
        self.assertIn("1 secret-like file", out)
        self.assertIn("agy: ok, hides 1 file", out)


class TestEdit(Env):
    def git(self, *a):
        subprocess.run(["git", *a], cwd=self.work, check=True, capture_output=True)

    def init_repo(self):
        self.git("init", "-q")
        (self.work / "target.txt").write_text("orig\n")
        self.git("add", ".")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    def test_edit_requires_git(self):
        code, _, err = self.run_cli("edit", "rename", "-C", str(self.work))
        self.assertEqual(code, cli.EXIT_GUARD)
        self.assertIn("git repository", err)

    def test_edit_requires_clean_tree(self):
        self.init_repo()
        (self.work / "target.txt").write_text("dirty\n")
        code, _, err = self.run_cli("edit", "rename", "-C", str(self.work))
        self.assertEqual(code, cli.EXIT_GUARD)
        self.assertEqual(self.calls(), [])

    def test_edit_reports_changes_and_allows_write(self):
        self.init_repo()
        (self.work / "preexisting.txt").write_text("u\n")  # 既存の未追跡は報告しない
        code, out, err = self.run_cli("edit", "rename", "-C", str(self.work), "--backend", "copilot",
                                      env={"FAKE_MODE": "edit"})
        self.assertEqual(code, 0, err)
        self.assertIn("target.txt", out)
        self.assertIn("new.txt", out)
        self.assertNotIn("preexisting.txt", out)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--allow-tool") + 1], "write")
        at = argv.index("--available-tools")
        self.assertEqual(argv[at + 1:at + 4], ["view", "glob", "apply_patch"])
        self.assertIn(["--deny-tool", "shell"], [argv[i:i + 2] for i in range(len(argv))])
        self.assertNotIn("--allow-all", argv)
        self.assertNotIn("--allow-all-tools", argv)

    def agy_settings_of(self, call) -> dict:
        argv = call["argv"]
        return json.loads(argv[argv.index("--settings") + 1])

    def test_edit_uses_agy_with_write_limited_to_workdir(self):
        self.init_repo()
        code, out, err = self.run_cli("edit", "rename", "-C", str(self.work), env={"FAKE_MODE": "edit"})
        self.assertEqual(code, 0, err)
        self.assertIn("target.txt", out)
        call = self.calls()[0]
        self.assertEqual(call["name"], "agy")
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-4-hydrogen-high")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        perm = self.agy_settings_of(call)["permissions"]
        work = os.path.realpath(self.work)
        self.assertIn(f"write_file({work})", perm["allow"])
        self.assertIn("command(ruff check)", perm["allow"])
        self.assertFalse(any("pytest" in a or "unittest" in a for a in perm["allow"]))  # テストは任意コード実行になる
        self.assertIn(f"write_file({work}/.git)", perm["deny"])
        self.assertIn("mcp(*)", perm["deny"])
        self.assertNotIn("command(*)", perm["deny"])
        self.assertNotIn("write_file(*)", perm["deny"])
        self.assertIn("ruff check", argv[argv.index("-p") + 1])  # 使えるコマンドをプロンプトでも伝える

    def test_edit_with_secrets_blocks_agy_shell(self):
        self.init_repo()
        self.cfg.write_text(self.cfg.read_text().replace("[guard]\n", '[guard]\nsecret_policy = {agy = "allow", copilot = "mask"}\n'))
        (self.work / ".gitignore").write_text(".env\n")
        self.git("add", ".gitignore")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "ignore")
        (self.work / ".env").write_text("TOKEN=x\n")
        code, _, err = self.run_cli("edit", "x", "-C", str(self.work), env={"FAKE_MODE": "edit"})
        self.assertEqual(code, 0, err)
        perm = self.agy_settings_of(self.calls()[0])["permissions"]
        self.assertIn("command(*)", perm["deny"])
        self.assertIn(f"write_file({os.path.realpath(self.work / '.env')})", perm["deny"])

    def test_edit_falls_back_when_agy_failed_without_changes(self):
        self.init_repo()
        code, out, err = self.run_cli("edit", "x", "-C", str(self.work),
                                      env={"FAKE_MODE_AGY": "fail", "FAKE_MODE_COPILOT": "edit"})
        self.assertEqual(code, 0, err)
        self.assertEqual([c["name"] for c in self.calls()], ["agy", "copilot"])

    def test_edit_does_not_fall_back_after_partial_changes(self):
        self.init_repo()
        code, out, err = self.run_cli("edit", "x", "-C", str(self.work), env={"FAKE_MODE_AGY": "editfail"})
        self.assertEqual(code, 1)
        self.assertEqual([c["name"] for c in self.calls()], ["agy"])
        self.assertIn("not falling back", err)
        self.assertIn("target.txt", out)

    def test_agy_rejects_implement_mode(self):
        with self.assertRaises(backends.BackendError):
            backends.build_agy_argv("agy", "p", "", "implement", [], 60)


class TestImplement(TestEdit):
    """implement: 作業役の実装 → delegate がテスト → 失敗なら同じ session で差し戻す。"""

    TWO_LINES = '[ "$(wc -l < impl.txt)" -ge 2 ] || { echo "need 2 lines"; exit 1; }'

    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.fenv = {"FAKE_MODE": "implement", "FAKE_STATE_DIR": str(self.state)}

    def test_requires_test_command(self):
        self.init_repo()
        code, _, err = self.run_cli("implement", "spec", "-C", str(self.work), env=self.fenv)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--test", err)
        self.assertEqual(self.calls(), [])

    def test_requires_clean_git(self):
        code, _, err = self.run_cli("implement", "spec", "-C", str(self.work), "--test", "true", env=self.fenv)
        self.assertEqual(code, cli.EXIT_GUARD)
        self.init_repo()
        (self.work / "target.txt").write_text("dirty\n")
        code, _, _ = self.run_cli("implement", "spec", "-C", str(self.work), "--test", "true", env=self.fenv)
        self.assertEqual(code, cli.EXIT_GUARD)
        self.assertEqual(self.calls(), [])

    def test_retries_with_test_output_in_same_session_until_pass(self):
        self.init_repo()
        code, out, err = self.run_cli("implement", "add a line", "-C", str(self.work), "--test", self.TWO_LINES,
                                      "--json", env=self.fenv)
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertTrue(d["passed"])
        self.assertEqual([r["test_passed"] for r in d["rounds"]], [False, True])
        # 累計値から差分を取るので、各ラウンドは 1 回分(200 / 0.25)になる
        self.assertEqual([r["tokens_in"] for r in d["rounds"]], [200, 200])
        self.assertEqual(d["tokens_in"], 400)
        self.assertAlmostEqual(d["credits"], 0.5)
        self.assertIn("impl.txt", d["edit_report"])
        c = self.calls()
        self.assertEqual(len(c), 2)
        sid = [a["argv"][a["argv"].index("--session-id") + 1] for a in c]
        self.assertEqual(sid[0], sid[1])
        first, second = (a["argv"][a["argv"].index("-p") + 1] for a in c)
        self.assertIn("add a line", first)
        self.assertIn("need 2 lines", second)
        for a in c:
            argv = a["argv"]
            self.assertIn(["--deny-tool", "shell"], [argv[i:i + 2] for i in range(len(argv))])
            at = argv.index("--available-tools")
            self.assertEqual(argv[at + 1:at + 4], ["view", "glob", "apply_patch"])
        log = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([e["round"] for e in log], [1, 2])

    def test_gives_up_after_rounds(self):
        self.init_repo()
        code, out, err = self.run_cli("implement", "x", "-C", str(self.work), "--test", "echo nope; exit 1",
                                      "--rounds", "2", env=self.fenv)
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("FAIL after 2 round(s)", out)
        self.assertIn("nope", out)
        self.assertEqual(len(self.calls()), 2)

    def test_spec_file_is_inlined_not_added_as_dir(self):
        self.init_repo()
        spec = self.tmp / "spec.md"
        spec.write_text("SPEC-BODY-123\n")
        code, _, err = self.run_cli("implement", "-f", str(spec), "-C", str(self.work), "--test", "true",
                                    env=self.fenv)
        self.assertEqual(code, 0, err)
        argv = self.calls()[0]["argv"]
        self.assertIn("SPEC-BODY-123", argv[argv.index("-p") + 1])
        self.assertNotIn("--add-dir", argv)

    def test_agy_backend_is_rejected(self):
        self.init_repo()
        code, _, err = self.run_cli("implement", "x", "-C", str(self.work), "--test", "true",
                                    "--backend", "agy", env=self.fenv)
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("read and edit tasks only", err)

    def test_secret_masked_and_redacted_from_test_output(self):
        self.init_repo()
        (self.work / ".env").write_text("X=1\n")
        token = "ghp_" + "a" * 36
        code, _, err = self.run_cli("implement", "x", "-C", str(self.work), "--test",
                                    "python3 -c \"print('ghp_' + 'a' * 36)\"; exit 1",
                                    "--rounds", "2", env=self.fenv)
        self.assertEqual(code, cli.EXIT_FAIL)
        c = self.calls()
        self.assertIn(f"read({(self.work / '.env').resolve()})", c[0]["argv"])
        second = c[1]["argv"][c[1]["argv"].index("-p") + 1]
        self.assertNotIn(token, second)
        self.assertIn("[REDACTED]", second)


class TestGithub(Env):
    def test_gh_runs_in_empty_dir_with_repo_slug_and_mcp_only(self):
        subprocess.run(["git", "init", "-q"], cwd=self.work, check=True)
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:me/proj.git"], cwd=self.work, check=True)
        (self.work / ".env").write_text("TOKEN=x")  # gh はローカルを読まないので隠す必要もない
        code, out, err = self.run_cli("gh", "list issues", "-C", str(self.work))
        self.assertEqual(code, 0, err)
        call = self.calls()[0]
        self.assertNotEqual(Path(call["cwd"]).resolve(), self.work.resolve())
        self.assertFalse(Path(call["cwd"]).exists(), "temporary dir is removed")
        argv = call["argv"]
        self.assertTrue(argv[argv.index("-p") + 1].startswith("Repository: me/proj"))
        at = argv.index("--available-tools")
        tools = []
        for a in argv[at + 1:]:
            if a.startswith("--"):
                break
            tools.append(a)
        self.assertTrue(tools and all(t.startswith("github-mcp-server-") for t in tools), tools)
        self.assertNotIn("view", tools)
        self.assertIn("github-mcp-server(list_issues)", argv)
        self.assertNotIn("hiding", err)

    def test_gh_with_agy_is_rejected(self):
        code, _, err = self.run_cli("gh", "x", "-C", str(self.work), "--backend", "agy")
        self.assertEqual(code, 1)
        self.assertIn("read and edit tasks only", err)


class TestStatsAndParsers(Env):
    def test_stats_table_and_days_filter(self):
        runlog.append(self.log, {"ts": 0, "task": "ask", "backend": "agy", "ok": True, "duration": 2,
                                 "tokens_in": 5, "tokens_out": 1, "credits": None})
        with self.log.open("a") as f:
            f.write("broken line\n")
        runlog.append(self.log, {"ts": runlog.now(), "task": "gh", "backend": "copilot", "ok": False,
                                 "duration": 4, "tokens_in": 7, "tokens_out": 2, "credits": 0.5})
        code, out, _ = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("agy", out)
        self.assertIn("0.500", out)
        code, out, _ = self.run_cli("stats", "--days", "1", "--json")
        self.assertEqual([s["backend"] for s in json.loads(out)], ["copilot"])

    def test_stats_empty(self):
        code, out, _ = self.run_cli("stats")
        self.assertIn("no calls", out)

    def test_parse_agy_denied_empty_response(self):
        out = json.dumps({"status": "SUCCESS", "response": "", "usage": {},
                          "denied_actions": [{"action": "read_file"}]})
        r = backends.parse_agy(0, out, "")
        self.assertFalse(r.ok)
        self.assertIn("denied", r.error)

    def test_parse_copilot_nonzero_result(self):
        out = "\n".join(json.dumps(e) for e in [
            {"type": "assistant.message", "data": {"content": "partial"}},
            {"type": "result", "exitCode": 1},
        ])
        self.assertFalse(backends.parse_copilot(0, out, "").ok)

    def test_bad_config(self):
        self.cfg.write_text('[tasks.ask]\nbackend = "nope"\n')
        code, _, err = self.run_cli("stats")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("unknown backend", err)


if __name__ == "__main__":
    unittest.main()
