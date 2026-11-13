import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpilot.system.updated import updated


class TestLightweightUpdate(unittest.TestCase):
  def git(self, cwd, *args):
    return updated.run(["git", *map(str, args)], str(cwd)).strip()

  def commit(self, repo, content):
    (repo / "runtime.txt").write_text(content)
    self.git(repo, "add", "--ignore-removal", ".")
    self.git(repo, "commit", "-m", content)
    return self.git(repo, "rev-parse", "HEAD")

  def repo(self, name):
    path = self.root / "remotes" / name
    self.git(self.root, "init", "-b", "master", path)
    self.git(path, "config", "uploadpack.allowFilter", "true")
    self.git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    return path

  def pin(self, repo, path, commit):
    self.git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},{path}")

  def setUp(self):
    self.addCleanup(mock.patch.stopall)
    mock.patch.dict(os.environ, {
      "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "file",
      "GIT_AUTHOR_NAME": "Updater test", "GIT_COMMITTER_NAME": "Updater test",
      "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_EMAIL": "test@example.com",
    }).start()
    tmp = tempfile.TemporaryDirectory(prefix="updated-lightweight-")
    self.addCleanup(tmp.cleanup)
    self.root = Path(tmp.name)
    (self.root / "remotes").mkdir()

    self.tinygrad = self.repo("tinygrad.git")
    fixtures = self.tinygrad / "test/mockgpu"
    fixtures.mkdir(parents=True)
    (fixtures / "old.bin").write_bytes(os.urandom(4096))
    (self.tinygrad / "test/small.txt").write_text("keep other tests")
    self.old_tinygrad = self.commit(self.tinygrad, "installed tinygrad")
    (fixtures / "new.bin").write_bytes(os.urandom(4096))
    self.target_tinygrad = self.commit(self.tinygrad, "pinned tinygrad")
    self.excluded_blob = self.git(self.tinygrad, "rev-parse", "HEAD:test/mockgpu/new.bin")
    self.tip_tinygrad = self.commit(self.tinygrad, "unselected tinygrad HEAD")

    nested = self.repo("nested.git")
    self.target_nested = self.commit(nested, "pinned nested")
    self.commit(nested, "unselected nested HEAD")
    other = self.repo("opendbc.git")
    self.old_other = self.commit(other, "installed opendbc")
    (other / ".gitmodules").write_text('[submodule "nested"]\npath = nested\nurl = ../nested.git\n')
    self.pin(other, "nested", self.target_nested)
    self.target_other = self.commit(other, "pinned opendbc")
    self.commit(other, "unselected opendbc HEAD")

    self.source = self.repo("super.git")
    (self.source / ".gitignore").write_text(".overlay_consistent\n")
    (self.source / ".gitmodules").write_text(
      '[submodule "tinygrad"]\npath = tinygrad_repo\nurl = ../tinygrad.git\n' +
      '[submodule "opendbc"]\npath = opendbc_repo\nurl = ../opendbc.git\n')
    self.pin(self.source, "tinygrad_repo", self.old_tinygrad)
    self.pin(self.source, "opendbc_repo", self.old_other)
    self.installed = self.commit(self.source, "installed superproject")
    self.git(self.source, "branch", "installed")
    self.pin(self.source, "tinygrad_repo", self.target_tinygrad)
    self.pin(self.source, "opendbc_repo", self.target_other)
    self.target = self.commit(self.source, "target superproject")
    self.git(self.source, "tag", "do-not-fetch-this-tag")

    self.active = self.root / "active"
    self.git(self.root, "clone", "--no-tags", "--branch", "installed", self.source.as_uri(), self.active)
    self.stage = self.root / "merged"
    shutil.copytree(self.active, self.stage)
    self.final = self.root / "finalized"
    mock.patch.object(updated, "OVERLAY_MERGED", str(self.stage)).start()
    mock.patch.object(updated, "FINALIZED", str(self.final)).start()
    mock.patch.object(updated, "AGNOS", False).start()
    mock.patch.object(updated.os, "sync").start()
    self.params = mock.MagicMock()
    self.params.get.return_value = "master"
    with mock.patch.object(updated, "Params", return_value=self.params):
      self.updater = updated.Updater()

  def assert_lightweight(self, existing_cache=False):
    self.assertEqual(self.git(self.active, "rev-parse", "HEAD"), self.installed)
    self.assertEqual(self.git(self.final, "rev-parse", "HEAD"), self.target)
    self.assertEqual(self.git(self.final, "rev-list", "--count", "HEAD"), "1")
    self.assertEqual(self.git(self.final, "tag"), "")
    for path, commit in (("tinygrad_repo", self.target_tinygrad), ("opendbc_repo", self.target_other),
                         ("opendbc_repo/nested", self.target_nested)):
      module = self.final / path
      self.assertEqual(self.git(module, "rev-parse", "HEAD"), commit)
      if not existing_cache or path == "tinygrad_repo":
        self.assertEqual(self.git(module, "rev-list", "--count", "HEAD"), "1")
      self.assertEqual(Path(self.git(module, "rev-parse", "--show-toplevel")), module)
      self.assertTrue(Path(self.git(module, "rev-parse", "--absolute-git-dir")).is_relative_to(self.final))
    tinygrad = self.final / "tinygrad_repo"
    self.assertEqual(self.git(tinygrad, "config", "remote.origin.partialclonefilter"), "blob:none")
    self.assertFalse((tinygrad / "test/mockgpu").exists())
    self.assertTrue((tinygrad / "test/small.txt").is_file())
    self.assertEqual((tinygrad / "runtime.txt").read_text(), "pinned tinygrad")
    self.assertEqual(self.git(self.final, "status", "--porcelain"), "")
    self.assertTrue((self.final / ".overlay_consistent").is_file())
    if not existing_cache:
      # Enumerate only local objects: cat-file -e on a promisor repo could download the fixture being tested.
      objects = self.git(tinygrad, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").splitlines()
      self.assertNotIn(self.excluded_blob, objects)

  def test_fresh_submodules_exclude_blobs_and_finalize_at_exact_pins(self):
    # Even an inherited recurse setting must not fetch submodules before sparse setup.
    self.git(self.stage, "config", "fetch.recurseSubmodules", "true")
    self.git(self.stage, "config", "submodule.recurse", "true")
    self.git(self.stage, "config", "submodule.opendbc.update", "none")
    self.updater.fetch_update()
    self.assert_lightweight()

  def test_existing_full_cache_is_preserved(self):
    self.git(self.stage, "submodule", "update", "--init", "--recursive")
    self.updater.fetch_update()
    self.assert_lightweight(existing_cache=True)
    self.assertEqual(self.git(self.final / "tinygrad_repo", "cat-file", "-t", self.old_tinygrad), "commit")

  def test_existing_partial_cache_fetches_only_selected_files(self):
    updated.update_submodules(str(self.stage))
    self.updater.fetch_update()
    self.assert_lightweight()

  def test_deinitialized_submodule_reuses_git_directory(self):
    updated.update_submodules(str(self.stage))
    self.git(self.stage, "submodule", "deinit", "--force", "tinygrad_repo")
    self.updater.fetch_update()
    self.assert_lightweight()

  def test_next_update_keeps_sparse_rules(self):
    self.updater.fetch_update()
    self.pin(self.source, "tinygrad_repo", self.tip_tinygrad)
    self.target = self.commit(self.source, "next update")
    self.updater.fetch_update()
    tinygrad = self.final / "tinygrad_repo"
    self.assertEqual(self.git(self.final, "rev-parse", "HEAD"), self.target)
    self.assertEqual(self.git(tinygrad, "rev-parse", "HEAD"), self.tip_tinygrad)
    self.assertFalse((tinygrad / "test/mockgpu").exists())
    self.assertTrue((self.final / ".overlay_consistent").is_file())

  def test_fetch_failure_invalidates_ready_update(self):
    self.updater.fetch_update()
    real_run = updated.run

    def fail_fetch(cmd, cwd=None):
      if "--filter=blob:none" in cmd:
        raise subprocess.CalledProcessError(1, cmd, output="network unavailable")
      return real_run(cmd, cwd)

    with mock.patch.object(updated, "run", side_effect=fail_fetch), mock.patch.object(updated, "finalize_update") as finalize:
      with self.assertRaises(subprocess.CalledProcessError):
        self.updater.fetch_update()
      finalize.assert_not_called()
    self.assertFalse((self.final / ".overlay_consistent").exists())
    self.params.put_bool.assert_any_call("UpdateAvailable", False, block=True)
    self.assertEqual(self.git(self.active, "rev-parse", "HEAD"), self.installed)

  def test_release_snapshot_without_submodules(self):
    self.git(self.source, "update-index", "--force-remove", "tinygrad_repo", "opendbc_repo", ".gitmodules")
    (self.source / ".gitmodules").unlink()
    self.target = self.commit(self.source, "release snapshot")
    self.updater.fetch_update()
    self.assertEqual(self.git(self.final, "rev-parse", "HEAD"), self.target)
    self.assertTrue((self.final / ".overlay_consistent").is_file())

  def test_lfs_and_agnos_still_gate_finalization(self):
    (self.source / ".gitattributes").write_text("model.bin filter=lfs diff=lfs merge=lfs -text\n")
    (self.source / "model.bin").write_text("model placeholder")
    self.target = self.commit(self.source, "model update")
    real_run = updated.run
    gates = []

    def run(cmd, cwd=None):
      if cmd == ["git", "lfs", "pull"]:
        gates.append("lfs")
        return ""
      return real_run(cmd, cwd)

    def agnos():
      gates.append("agnos")
      self.assertFalse((self.final / ".overlay_consistent").exists())

    with mock.patch.object(updated, "run", side_effect=run), mock.patch.object(updated, "AGNOS", True), \
         mock.patch.object(updated, "handle_agnos_update", side_effect=agnos):
      self.updater.fetch_update()
    self.assertEqual(gates, ["lfs", "agnos"])
    self.assertTrue((self.final / ".overlay_consistent").is_file())

  def test_lfs_failure_does_not_finalize_or_flash_agnos(self):
    (self.source / ".gitattributes").write_text("model.bin filter=lfs diff=lfs merge=lfs -text\n")
    (self.source / "model.bin").write_text("model placeholder")
    self.commit(self.source, "model update")
    real_run = updated.run

    def fail_lfs(cmd, cwd=None):
      if cmd == ["git", "lfs", "pull"]:
        raise subprocess.CalledProcessError(1, cmd, output="LFS download failed")
      return real_run(cmd, cwd)

    with mock.patch.object(updated, "run", side_effect=fail_lfs), mock.patch.object(updated, "AGNOS", True), \
         mock.patch.object(updated, "handle_agnos_update") as agnos, mock.patch.object(updated, "finalize_update") as finalize:
      with self.assertRaises(subprocess.CalledProcessError):
        self.updater.fetch_update()
      agnos.assert_not_called()
      finalize.assert_not_called()
    self.assertFalse((self.final / ".overlay_consistent").exists())

  def test_agnos_failure_does_not_finalize(self):
    with mock.patch.object(updated, "AGNOS", True), \
         mock.patch.object(updated, "handle_agnos_update", side_effect=RuntimeError("incomplete AGNOS")), \
         mock.patch.object(updated, "finalize_update") as finalize:
      with self.assertRaisesRegex(RuntimeError, "incomplete AGNOS"):
        self.updater.fetch_update()
      finalize.assert_not_called()
    self.assertFalse((self.final / ".overlay_consistent").exists())


if __name__ == "__main__":
  unittest.main()
