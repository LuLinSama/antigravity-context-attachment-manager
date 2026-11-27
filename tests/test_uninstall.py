import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
CAM = "antigravity-context-attachment-manager"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "requires Windows PowerShell")
class UninstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.home = root / "profile"
        self.workspace = root / "workspace [sample]"
        self.data = root / "local" / CAM
        self.data.mkdir(parents=True)
        self.history = self.data / "attachments.sqlite3"
        self.history.write_bytes(b"local history")
        (self.data / "usage.sqlite3").write_bytes(b"usage")
        (self.data / "traffic.json").write_bytes(b"{}")
        (self.data / "preview-cache").mkdir()
        (self.data / "preview-cache" / "legacy.jpg").write_bytes(b"cached copy")
        self.config = self.home / ".gemini" / "config" / "config.json"
        self.config.parent.mkdir(parents=True)
        self.global_plugin = self.config.parent / "plugins" / CAM
        self.runtime = self.home / ".gemini" / "antigravity"
        self.workspace.mkdir()
        self.workspace_plugin = self.workspace / ".agents" / "plugins" / CAM
        self.env = {**os.environ, "USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.data.parent)}

    @staticmethod
    def add_plugin(directory, name=CAM):
        (directory / "src").mkdir(parents=True)
        (directory / "src" / "test.py").write_text("# fixture\n", encoding="utf-8")
        (directory / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")

    def powershell(self, *args, check=True):
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", *map(str, args)],
                                env=self.env, capture_output=True, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def uninstall(self, *args, check=True):
        return self.powershell("-File", ROOT / "uninstall.ps1", *args, check=check)

    def test_global_whatif_uninstall_and_repeat(self):
        self.add_plugin(self.global_plugin)
        sibling = self.global_plugin.with_name("another-plugin")
        self.add_plugin(sibling, "another-plugin")
        original = {"plugins": {"another-plugin": {"enabled": True}, CAM: {"enabled": True}},
                    "sidecars": {"unrelated": {"enabled": True}, f"{CAM}/startup": {"enabled": True}}}
        self.config.write_text(json.dumps(original), encoding="utf-8")
        stale = self.config.with_name(".cam-" + "a" * 32 + ".tmp")
        stale.write_bytes(b"stale CAM temp file")
        unrelated = self.config.with_name(".cam-note.tmp")
        unrelated.write_bytes(b"unrelated")
        before = self.config.read_bytes()
        result = self.uninstall("-Global", "-WhatIf")
        self.assertIn(b"shared CAM data", result.stdout)
        self.assertTrue(self.global_plugin.is_dir())
        self.assertEqual(self.history.read_bytes(), b"local history")
        self.assertEqual(stale.read_bytes(), b"stale CAM temp file")
        self.assertEqual(self.config.read_bytes(), before)

        self.uninstall("-Global")
        self.assertFalse(self.global_plugin.exists())
        self.assertFalse(self.data.exists())
        self.assertFalse(stale.exists())
        self.assertEqual(unrelated.read_bytes(), b"unrelated")
        self.assertTrue(sibling.is_dir())
        del original["plugins"][CAM]
        del original["sidecars"][f"{CAM}/startup"]
        self.assertEqual(json.loads(self.config.read_bytes()), original)
        unchanged = self.config.read_bytes()
        self.uninstall("-Global")
        self.assertEqual(self.config.read_bytes(), unchanged)

    def test_workspace_uninstall_does_not_touch_global_scope(self):
        self.add_plugin(self.workspace_plugin)
        self.add_plugin(self.global_plugin)
        config = json.dumps({"plugins": {CAM: {"enabled": True}}, "sidecars": {f"{CAM}/startup": {}}}).encode()
        self.config.write_bytes(config)
        runtime = self.runtime / "plugin_data" / CAM
        runtime.mkdir(parents=True)
        self.uninstall("-Workspace", self.workspace, "-WhatIf")
        self.assertTrue(runtime.is_dir())
        self.assertTrue(self.workspace_plugin.is_dir())
        self.assertTrue(self.history.is_file())
        self.uninstall("-Workspace", self.workspace)
        self.assertFalse(self.workspace_plugin.exists())
        self.assertFalse(runtime.exists())
        self.assertFalse(self.data.exists())
        self.assertTrue(self.workspace_plugin.parent.is_dir())
        self.assertTrue(self.global_plugin.is_dir())
        self.assertEqual(self.config.read_bytes(), config)

    def test_removes_runtime_only_residue_without_touching_other_plugins(self):
        shutil.rmtree(self.data)
        artifacts = [self.runtime / "mcp" / f"{CAM}_media",
                     self.runtime / "mcp" / f"{CAM}_attachments",
                     self.runtime / "plugin_data" / CAM,
                     self.runtime / "sidecar_data" / CAM]
        for path in artifacts:
            path.mkdir(parents=True)
            (path / "state.json").write_bytes(b"CAM state")
            neighbor = path.with_name(path.name + "-other")
            neighbor.mkdir()
            (neighbor / "state.json").write_bytes(b"other plugin")

        result = self.uninstall("-Global", "-WhatIf")
        for path in artifacts:
            self.assertIn(path.name.encode(), result.stdout)
            self.assertEqual((path / "state.json").read_bytes(), b"CAM state")
        self.uninstall("-Global")
        for path in artifacts:
            self.assertFalse(path.exists())
            other = path.with_name(path.name + "-other")
            self.assertEqual((other / "state.json").read_bytes(), b"other plugin")
        self.assertFalse(self.config.exists())

    def test_refuses_runtime_junction(self):
        self.add_plugin(self.global_plugin)
        original_root = self.home / "other-plugin"
        original_root.mkdir()
        original = original_root / "state.json"
        original.write_bytes(b"unrelated data")
        link = self.runtime / "mcp" / f"{CAM}_media"
        link.parent.mkdir(parents=True)
        self.junction(link, original_root)
        self.addCleanup(link.rmdir)
        result = self.uninstall("-Global", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"junction", result.stderr)
        self.assertEqual(original.read_bytes(), b"unrelated data")
        self.assertTrue(self.global_plugin.is_dir())
        self.assertTrue(self.data.is_dir())

    def test_removes_orphaned_data_and_preserves_originals_and_siblings(self):
        original = self.workspace / "original video.mp4"
        original.write_bytes(b"original video")
        (self.data / "record.json").write_text(json.dumps({"path": str(original)}), encoding="utf-8")
        sibling = self.data.with_name(CAM + "-other")
        sibling.mkdir()
        (sibling / "keep.txt").write_bytes(b"not CAM data")
        native_history = self.home / ".gemini" / "antigravity" / "conversations" / "native.pb"
        native_history.parent.mkdir(parents=True)
        native_history.write_bytes(b"native history")

        self.uninstall("-Global")
        self.assertFalse(self.data.exists())
        self.assertTrue(self.data.parent.is_dir())
        self.assertFalse(self.config.exists())
        self.assertEqual(original.read_bytes(), b"original video")
        self.assertEqual((sibling / "keep.txt").read_bytes(), b"not CAM data")
        self.assertEqual(native_history.read_bytes(), b"native history")

    def test_uses_profile_local_appdata_when_env_is_absent(self):
        default_data = self.home / "AppData" / "Local" / CAM
        default_data.mkdir(parents=True)
        (default_data / "usage.sqlite3").write_bytes(b"usage")
        del self.env["LOCALAPPDATA"]
        self.uninstall("-Global")
        self.assertFalse(default_data.exists())
        self.assertTrue(self.history.is_file())

    def test_refuses_relative_local_appdata(self):
        self.add_plugin(self.global_plugin)
        self.env["LOCALAPPDATA"] = "."
        result = self.uninstall("-Global", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"absolute LOCALAPPDATA path", result.stderr)
        self.assertTrue(self.global_plugin.is_dir())
        self.assertTrue(self.history.is_file())

    def test_global_registration_only(self):
        self.config.write_text(json.dumps({"plugins": {CAM: {"enabled": True}}}), encoding="utf-8")
        self.uninstall("-Global")
        self.assertEqual(json.loads(self.config.read_bytes()), {"plugins": {}})

    def test_global_empty_directory_and_registration(self):
        self.global_plugin.mkdir(parents=True)
        original = {"plugins": {CAM: {"enabled": True}, "another-plugin": {"enabled": True}}}
        self.config.write_text(json.dumps(original), encoding="utf-8")
        self.uninstall("-Global", "-WhatIf")
        self.assertTrue(self.global_plugin.is_dir())
        self.assertEqual(json.loads(self.config.read_bytes()), original)

        self.uninstall("-Global")
        self.assertFalse(self.global_plugin.exists())
        self.assertTrue(self.global_plugin.parent.is_dir())
        del original["plugins"][CAM]
        self.assertEqual(json.loads(self.config.read_bytes()), original)

    def test_nonempty_directory_without_manifest_is_preserved(self):
        self.global_plugin.mkdir(parents=True)
        unknown = self.global_plugin / "user-file.txt"
        unknown.write_bytes(b"keep")
        config = json.dumps({"plugins": {CAM: {"enabled": True}}}).encode()
        self.config.write_bytes(config)
        result = self.uninstall("-Global", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Non-empty plugin directory", result.stderr)
        self.assertEqual(unknown.read_bytes(), b"keep")
        self.assertEqual(self.config.read_bytes(), config)
        self.assertTrue(self.history.is_file())

    def test_refuses_foreign_manifest_and_malformed_config(self):
        self.add_plugin(self.workspace_plugin, "another-plugin")
        result = self.uninstall("-Workspace", self.workspace, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.workspace_plugin.is_dir())

        self.add_plugin(self.global_plugin)
        self.config.write_text("not JSON", encoding="utf-8")
        result = self.uninstall("-Global", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.global_plugin.is_dir())
        self.assertEqual(self.config.read_bytes(), b"not JSON")

    def test_refuses_junction_at_target_or_inside_plugin(self):
        # Windows PowerShell expands wildcard brackets in New-Item's junction target.
        workspace = self.workspace.with_name("plain workspace")
        plugin = workspace / ".agents" / "plugins" / CAM
        outside = workspace / "outside-plugin"
        self.add_plugin(outside)
        plugin.parent.mkdir(parents=True)
        self.junction(plugin, outside)
        try:
            result = self.uninstall("-Workspace", workspace, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"junction", result.stderr)
            self.assertTrue((outside / "src" / "test.py").is_file())
        finally:
            plugin.rmdir()

        self.add_plugin(plugin)
        nested = plugin / "linked"
        self.junction(nested, outside)
        try:
            result = self.uninstall("-Workspace", workspace, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"junction", result.stderr)
            self.assertTrue((outside / "src" / "test.py").is_file())
        finally:
            nested.rmdir()

        linked_workspace = workspace.with_name("linked workspace")
        self.junction(linked_workspace, workspace)
        try:
            result = self.uninstall("-Workspace", linked_workspace, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"junction", result.stderr)
            self.assertTrue((plugin / "plugin.json").is_file())
        finally:
            linked_workspace.rmdir()

    def test_refuses_junction_at_or_above_data_directory(self):
        self.add_plugin(self.global_plugin)
        fake_app_data = self.home / "fake local"
        fake_app_data.mkdir()
        junctions = [(fake_app_data / CAM, self.data, fake_app_data),
                     (self.home / "linked local", self.data.parent, self.home / "linked local")]
        for link, target, local_app_data in junctions:
            with self.subTest(link=link):
                self.junction(link, target)
                self.addCleanup(link.rmdir)
                self.env["LOCALAPPDATA"] = str(local_app_data)
                result = self.uninstall("-Global", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"junction", result.stderr)
                self.assertTrue(self.global_plugin.is_dir())
                self.assertTrue(self.history.is_file())

    def test_refuses_junction_inside_data_directory(self):
        self.add_plugin(self.global_plugin)
        target = self.home / "originals"
        target.mkdir()
        original = target / "video.mp4"
        original.write_bytes(b"original video")
        link = self.data / "linked"
        self.junction(link, target)
        self.addCleanup(link.rmdir)
        result = self.uninstall("-Global", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"junction", result.stderr)
        self.assertTrue(self.global_plugin.is_dir())
        self.assertTrue(self.history.is_file())
        self.assertEqual(original.read_bytes(), b"original video")

    def junction(self, link, target):
        def quote(path):
            return "'" + str(path).replace("'", "''") + "'"

        command = f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)}"
        self.powershell("-Command", command + " -ErrorAction Stop | Out-Null")
