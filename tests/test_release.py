import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release


def upstream(tag="release-79.1", **overrides):
    return dict(tag_name=tag, draft=False, prerelease=False,
                html_url=f"https://github.com/unicode-org/icu/releases/tag/{tag}", **overrides)


def fixture_artifact(directory):
    artifact = directory / "ICU.xcframework"
    artifact.mkdir()
    targets = [{"family": family, "variant": variant, "arch": "arm64"}
               for family, settings in release.PLATFORMS.items()
               for variant, _, _, _ in settings["variants"]]
    manifest = {"builder": "icu-xcframework", "source_commit": "abc123",
                "data_filter_sha256": None, "targets": targets,
                "public_headers_sha256": {"unicode/utypes.h": "upstream-hash"}}
    verification = {"slices": [dict(t, same_object_dead_stripping="passed",
                                     c_cpp_swift_link="passed", upstream_public_headers=1) for t in targets]}
    (artifact / "BuildManifest.json").write_text(json.dumps(manifest))
    (artifact / "Verification.json").write_text(json.dumps(verification))
    return artifact, manifest, verification


class FakeGitHub:
    def __init__(self):
        self.current = {"id": 123, "draft": True, "body": "Maintainer notes", "assets": []}
        self.assets = {}
        self.events = []

    def release(self, repository, tag):
        return copy.deepcopy(self.current)

    def releases(self, repository):
        return [upstream()]

    def upload(self, repository, tag, path):
        if path.name in self.assets:
            raise AssertionError("An existing asset would have been overwritten")
        self.events.append(path.name)
        self.assets[path.name] = path.read_bytes()
        self.current["assets"].append({"name": path.name})

    def download(self, repository, tag, name, directory):
        path = directory / name
        path.write_bytes(self.assets[name])
        return path

    def api(self, endpoint, method="GET", data=None, **kwargs):
        self.events.append(method)
        self.current.update(data)
        return copy.deepcopy(self.current)


def fake_command(*args, **kwargs):
    if args[:3] == ("swift", "package", "compute-checksum"):
        return release.checksum(Path(args[3]))
    if "rev-parse" in args:
        return "abc123"
    if "ls-tree" in args:
        return "160000 commit abc123\ticu"
    return ""


class ReleaseTests(unittest.TestCase):
    def test_published_release_lookup_does_not_list_releases(self):
        github = release.GitHub()
        published = dict(upstream(), id=123)
        with patch.object(github, "api", return_value=published) as api, \
                patch.object(github, "releases") as releases:
            self.assertEqual(github.release("owner/repo", "release-79.1"), published)
            api.assert_called_once_with("repos/owner/repo/releases/tags/release-79.1", missing_ok=True)
            releases.assert_not_called()

    def test_draft_release_lookup_falls_back_to_authenticated_listing(self):
        github = release.GitHub()
        draft = dict(upstream(), id=123, draft=True)
        with patch.object(github, "api", return_value=None), \
                patch.object(github, "releases", return_value=[upstream("release-78.3"), draft]) as releases:
            self.assertEqual(github.release("owner/repo", "release-79.1"), draft)
            releases.assert_called_once_with("owner/repo")

    def test_missing_release_lookup_checks_drafts_before_returning_none(self):
        github = release.GitHub()
        with patch.object(github, "api", return_value=None), \
                patch.object(github, "releases", return_value=[upstream("release-78.3")]):
            self.assertIsNone(github.release("owner/repo", "release-79.1"))

    def test_update_commit_subject_follows_create_commits(self):
        message = release.submodule_commit_message("79.1")
        self.assertEqual(message, "⬆️ Update ICU to 79.1")
        self.assertLess(len(message), 50)
        self.assertFalse(message.endswith("."))
        prefix_length = len("⬆️ Update ICU to ")
        self.assertEqual(len(release.submodule_commit_message("1" * (49 - prefix_length))), 49)
        for invalid in ("1" * (50 - prefix_length), "79.1\nInjected line", "79.1\r", "79.1."):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                release.submodule_commit_message(invalid)

    def test_stable_versions_handle_both_upstream_tag_formats(self):
        self.assertEqual(release.release_version("release-77-1"), (77, 1, 0))
        self.assertEqual(release.release_version("release-79.1.2"), (79, 1, 2))
        for tag in ("release-79.1rc2", "icu4x/2026-08-27/79.x", "main"):
            self.assertIsNone(release.release_version(tag))
        candidates = [upstream("release-78.3"), upstream("release-79.1"),
                      dict(upstream("release-80.1"), prerelease=True),
                      dict(upstream("release-81.1"), draft=True)]
        self.assertEqual(release.stable_release(candidates)["tag_name"], "release-79.1")

    def test_notes_have_exact_asset_url_checksum_and_preserve_maintainer_text(self):
        body = release.release_notes("owner/repo", "release-79.1", upstream(), "a" * 64, "My notes")
        self.assertIn('name: "ICU4C"', body)
        self.assertIn("https://github.com/owner/repo/releases/download/release-79.1/ICU.xcframework.zip", body)
        self.assertIn('checksum: "' + "a" * 64 + '"', body)
        self.assertTrue(body.startswith("My notes\n\n"))
        replacement = release.release_notes("owner/repo", "release-79.1", upstream(), "b" * 64, body)
        self.assertEqual(replacement.count(release.BEGIN), 1)
        self.assertNotIn("a" * 64, replacement)
        self.assertIn("My notes", replacement)

    def test_bundle_has_one_xcframework_at_archive_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact, manifest, verification = fixture_artifact(directory)
            archive = directory / release.ARCHIVE
            release.bundle(artifact, archive, "abc123")
            with zipfile.ZipFile(archive) as contents:
                self.assertTrue(all(name.startswith("ICU.xcframework/") for name in contents.namelist()))
                self.assertEqual(json.loads(contents.read("ICU.xcframework/BuildManifest.json")), manifest)

    def test_invalid_snapshot_incomplete_platforms_and_filtered_data_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, manifest, verification = fixture_artifact(Path(temporary))
            for key, value in (("source_commit", "different"), ("data_filter_sha256", "filtered"),
                               ("targets", manifest["targets"][:-1]), ("public_headers_sha256", {})):
                candidate = dict(manifest, **{key: value})
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    release.validate_artifact(candidate, verification, "abc123")

    @patch.object(release, "icu_version", return_value="79.1")
    @patch.object(release, "run", side_effect=fake_command)
    def test_publish_uploads_before_notes_and_reuses_existing_binary_on_retry(self, run, version):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact, _, _ = fixture_artifact(directory)
            github = FakeGitHub()
            release.publish(github, "owner/repo", "release-79.1", artifact, directory / "output")
            self.assertEqual(github.events, [release.ARCHIVE, release.CHECKSUM, "PATCH"])
            self.assertFalse(github.current["draft"])
            original = github.assets[release.ARCHIVE]
            digest = hashlib.sha256(original).hexdigest()
            self.assertIn(digest, github.current["body"])
            self.assertEqual(github.assets[release.CHECKSUM].decode().split()[0], digest)
            # A different local build must never replace the already published asset.
            (artifact / "new-toolchain-build").write_text("different SDK")
            github.events.clear()
            with patch.object(release, "bundle", side_effect=AssertionError("should reuse the remote archive")):
                release.publish(github, "owner/repo", "release-79.1", artifact, directory / "retry")
            self.assertEqual(github.events, ["PATCH"])
            self.assertEqual(github.assets[release.ARCHIVE], original)
            self.assertEqual(github.current["body"].count(release.BEGIN), 1)

    @patch.object(release, "icu_version", return_value="79.1")
    @patch.object(release, "outputs")
    @patch.object(release, "run", return_value="")
    def test_completed_release_check_is_a_noop(self, run, outputs, version):
        github = FakeGitHub()
        github.current.update(draft=False, body=release.BEGIN + release.END,
                              assets=[{"name": release.ARCHIVE}, {"name": release.CHECKSUM}])
        release.check_upstream(github, "owner/repo", "main")
        outputs.assert_called_once_with(tag="release-79.1", publish="false")
        self.assertFalse(any("push" in call.args or "commit" in call.args for call in run.call_args_list))

    @patch.object(release, "icu_version", return_value="80.1")
    @patch.object(release, "outputs")
    @patch.object(release, "run", return_value="")
    def test_check_never_downgrades_a_newer_submodule(self, run, outputs, version):
        github = Mock()
        github.releases.return_value = [upstream()]
        release.check_upstream(github, "owner/repo", "main")
        outputs.assert_called_once_with(tag="release-79.1", publish="false")
        github.release.assert_not_called()
        self.assertFalse(any("push" in call.args for call in run.call_args_list))

    @patch.object(release, "icu_version", return_value="79.1")
    @patch.object(release, "outputs")
    @patch.object(release.subprocess, "run", return_value=SimpleNamespace(returncode=0))
    @patch.object(release, "run", side_effect=fake_command)
    def test_pending_draft_retries_without_another_commit_or_tag_push(self, run, diff, outputs, version):
        github = Mock()
        github.releases.return_value = [upstream()]
        github.release.return_value = {"draft": True, "assets": [], "body": ""}
        github.api.return_value = {"object": {"sha": "existing-snapshot"}}
        release.check_upstream(github, "owner/repo", "main")
        self.assertFalse(any("push" in call.args or "commit" in call.args for call in run.call_args_list))
        self.assertFalse(any(len(c.args) > 1 and c.args[1] == "POST" for c in github.api.call_args_list))
        outputs.assert_called_once_with(tag="release-79.1", publish="true")

    @patch.object(release, "icu_version", side_effect=["78.3", "79.1"])
    @patch.object(release, "outputs")
    @patch.object(release.subprocess, "run", return_value=SimpleNamespace(returncode=1))
    @patch.object(release, "run", side_effect=fake_command)
    def test_new_release_commits_only_the_submodule_and_pushes_snapshot_tag(self, run, diff, outputs, version):
        github = Mock()
        github.releases.return_value = [upstream()]
        github.release.return_value = None
        github.api.return_value = None
        release.check_upstream(github, "owner/repo", "main")
        commits = [call.args for call in run.call_args_list if "commit" in call.args]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0][-2:], ("--", "icu"))
        message = commits[0][commits[0].index("-m") + 1]
        self.assertEqual(message, "⬆️ Update ICU to 79.1")
        self.assertLess(len(message), 50)
        self.assertIn(("git", "push", "origin", "HEAD:refs/heads/main"), [c.args for c in run.call_args_list])
        self.assertIn(("git", "push", "origin", "refs/tags/release-79.1"), [c.args for c in run.call_args_list])
        post = next(c for c in github.api.call_args_list if len(c.args) > 1 and c.args[1] == "POST")
        self.assertTrue(post.args[2]["draft"])
        outputs.assert_called_once_with(tag="release-79.1", publish="true")


if __name__ == "__main__":
    unittest.main()
