"""Behavioral tests for the catalog contract, using disposable projects only."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from skillorbit.cli import CONFIG, OrbitError, init, load_json, scan, config_for, sync, start, stop, running


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()
        self.output = self.project / "SKILLS.md"
        self.capture = contextlib.redirect_stdout(io.StringIO())
        self.capture.__enter__()

    def tearDown(self):
        self.capture.__exit__(None, None, None)
        self.temp.cleanup()

    def skill(self, folder="one", name="one", description="Do one thing. Use when needed.", project="Demo"):
        path = self.project / ".agents" / "skills" / folder / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {json.dumps(name)}\nproject: {json.dumps(project)}\n"
                        f"description: {json.dumps(description)}\n---\n\n# Example\n", encoding="utf-8")
        return path

    def test_add_remove_readd_restores_notes(self):
        path = self.skill()
        init(self.project)
        sync(self.project, note=("one", "常用 | 中文\nnext", None))
        self.assertIn("常用 &#124; 中文<br>next", self.output.read_text())
        path.unlink()
        sync(self.project)
        self.assertNotIn("| `one`", self.output.read_text())
        self.skill()
        sync(self.project)
        self.assertIn("常用 &#124; 中文<br>next", self.output.read_text())

    def test_direct_markdown_edit_and_empty_note_survive(self):
        self.skill()
        init(self.project)
        text = self.output.read_text().replace("| `one` | Demo | Do one thing. |  |",
                                              "| `one` | Demo | Do one thing. | 手写备注 \\| 保留 |")
        self.output.write_text(text)
        sync(self.project)
        self.assertIn("手写备注 &#124; 保留", self.output.read_text())
        text = self.output.read_text().replace("手写备注 &#124; 保留", "")
        self.output.write_text(text)
        sync(self.project)
        self.assertNotIn("手写备注", self.output.read_text())

    def test_same_name_different_owner_remains_distinct(self):
        self.skill("a", project="Alpha")
        self.skill("b", project="Beta")
        init(self.project)
        self.assertEqual(len(scan(self.project, config_for(self.project))), 2)
        sync(self.project, note=("one", "only alpha", "Alpha"))
        with self.assertRaises(OrbitError):
            sync(self.project, note=("one", "ambiguous", None))
        rows = [s for s in self.output.read_text().splitlines() if s.startswith("| `one`")]
        self.assertIn("only alpha", rows[0])
        self.assertNotIn("only alpha", rows[1])

    def test_duplicate_variants_retained_in_source_index(self):
        self.skill("one")
        self.skill("overlays/one")
        init(self.project)
        text = self.output.read_text()
        self.assertIn("1 个技能 · 2 份定义", text)
        self.assertIn("overlays/one/SKILL.md", text)

    def test_invalid_yaml_preserves_last_good_catalog(self):
        self.skill()
        init(self.project)
        before = self.output.read_bytes()
        broken = self.skill("broken")
        broken.write_text("---\nname: [\ndescription: broken\n---\n")
        with self.assertRaises(OrbitError):
            sync(self.project)
        self.assertEqual(self.output.read_bytes(), before)

    def test_folded_yaml_and_description_refresh(self):
        path = self.skill()
        path.write_text("---\nname: one\nproject: Demo\ndescription: >-\n  第一行描述。\n  第二句。\n---\n")
        init(self.project)
        self.assertIn("第一行描述。", self.output.read_text())
        path.write_text(path.read_text().replace("第一行描述。", "新用途。"))
        sync(self.project)
        self.assertIn("新用途。", self.output.read_text())

    def test_check_is_read_only_and_noop_preserves_mtime(self):
        self.skill()
        init(self.project)
        stamp = self.output.stat().st_mtime_ns
        state = (self.project / ".skillorbit/state.json").read_bytes()
        self.assertEqual(sync(self.project, check=True), 0)
        sync(self.project)
        self.assertEqual(self.output.stat().st_mtime_ns, stamp)
        self.skill("two", name="two")
        self.assertEqual(sync(self.project, check=True), 1)
        self.assertEqual((self.project / ".skillorbit/state.json").read_bytes(), state)

    def test_preserves_user_text_outside_markers(self):
        self.skill()
        init(self.project)
        self.output.write_text("User introduction\n" + self.output.read_text() + "\nUser footer\n")
        self.skill("two", name="two")
        sync(self.project)
        self.assertTrue(self.output.read_text().startswith("User introduction"))
        self.assertTrue(self.output.read_text().endswith("User footer\n"))

    def test_legacy_import_backup_alias_and_freshness(self):
        path = self.skill("folder-name", name="alias")
        old = self.project / "legacy.md"
        old.write_text("| Skill 名称 | 归属项目 | 一句话作用 | 备注 |\n|---|---|---|---|\n"
                       "| `alias` | Legacy Suite | 中文简介。 | 备注 |\n")
        init(self.project, output="legacy.md", import_md="legacy.md")
        self.assertTrue((self.project / "legacy.md.pre-skillorbit.bak").exists())
        self.assertIn("中文简介。", old.read_text())
        self.assertIn("Legacy Suite", old.read_text())
        path.write_text(path.read_text().replace("Do one thing.", "New purpose."))
        sync(self.project)
        self.assertIn("New purpose.", old.read_text())
        self.assertIn("备注", old.read_text())

    def test_output_paths_with_spaces_and_pipes(self):
        self.skill("a b", name="a|b", project="Demo | Tools")
        init(self.project, output="docs/my skills.md")
        text = (self.project / "docs/my skills.md").read_text()
        self.assertIn("a&#124;b", text)
        self.assertIn("a%20b", text)
        sync(self.project, note=("a|b", "persist", None))
        sync(self.project)
        self.assertIn("persist", (self.project / "docs/my skills.md").read_text())

    @unittest.skipIf(os.name == "nt", "Symlink privilege differs on Windows")
    def test_symlink_cycle_and_external_escape(self):
        path = self.skill()
        (path.parent / "loop").symlink_to(path.parent, target_is_directory=True)
        (path.parent / "escape").symlink_to(self.project.parent, target_is_directory=True)
        init(self.project)
        self.assertIn("1 个技能 · 1 份定义", self.output.read_text())

    def test_project_rule_and_unknown_owner(self):
        self.skill(project="Ignored by configured rule")
        init(self.project)
        config = load_json(self.project / CONFIG, {})
        config["project_rules"] = [{"match": ".agents/skills/*", "project": "Owned"}]
        (self.project / CONFIG).write_text(json.dumps(config))
        sync(self.project)
        self.assertIn("| Owned |", self.output.read_text())

    def test_live_watcher_addition_deletion_and_error_recovery(self):
        self.skill()
        init(self.project)
        process = subprocess.Popen([sys.executable, "-m", "skillorbit", "--project", str(self.project),
                                    "watch", "--interval", "0.2"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        def wait_for(predicate):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.05)
            self.fail("Watcher did not reconcile within 8 seconds")
        try:
            path = self.skill("two", name="two")
            wait_for(lambda: "| `two`" in self.output.read_text())
            path.write_text("---\nname: [\n---\n")
            time.sleep(0.4)
            self.assertIn("| `two`", self.output.read_text())
            path.unlink()
            wait_for(lambda: "| `two`" not in self.output.read_text())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_background_start_singleton_sync_and_stop(self):
        self.skill()
        init(self.project)
        config = load_json(self.project / CONFIG, {})
        config["interval"] = 0.2
        (self.project / CONFIG).write_text(json.dumps(config))
        try:
            start(self.project)
            first_pid = running(self.project)["pid"]
            start(self.project)
            self.assertEqual(running(self.project)["pid"], first_pid)
            self.skill("two", name="two")
            deadline = time.monotonic() + 6
            while "| `two`" not in self.output.read_text() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn("| `two`", self.output.read_text())
        finally:
            stop(self.project)
        self.assertIsNone(running(self.project))

    def test_fresh_heartbeat_with_denied_pid_probe(self):
        self.skill()
        init(self.project)
        runtime = self.project / ".skillorbit/runtime.json"
        runtime.write_text(json.dumps({"pid": 99999, "heartbeat": time.time(), "interval": 2}))
        with mock.patch("skillorbit.cli.os.kill", side_effect=PermissionError):
            self.assertIsNotNone(running(self.project))
        runtime.write_text(json.dumps({"pid": 99999, "heartbeat": 0, "interval": 2}))
        self.assertIsNone(running(self.project))


if __name__ == "__main__":
    unittest.main()
