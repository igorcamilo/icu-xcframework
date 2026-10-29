import copy
import base64
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release
from frameworks import FORMAT, PRIVACY_SHA256


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
                "framework_format": FORMAT, "privacy_manifest_sha256": PRIVACY_SHA256,
                "data_filter_sha256": None, "targets": targets,
                "public_headers_sha256": {"unicode/utypes.h": "upstream-hash"}}
    verification = {"slices": [dict(t, same_object_dead_stripping="passed",
                                     c_cpp_swift_link="passed", upstream_public_headers=1) for t in targets]}
    for item in verification["slices"]:
        item["privacy_manifest"] = "passed"
    (artifact / "BuildManifest.json").write_text(json.dumps(manifest))
    (artifact / "Verification.json").write_text(json.dumps(verification))
    return artifact, manifest, verification


class FakeGitHub:
    def __init__(self):
        self.current = {"id": 123, "draft": True, "body": "Maintainer notes", "assets": []}
        self.assets = {}
        self.events = []
        self.uploads = []

    def release(self, repository, tag):
        return copy.deepcopy(self.current)

    def releases(self, repository):
        return [upstream()]

    def submodule_commit(self, repository, ref):
        return "abc123"

    def tag_commit(self, repository, tag):
        return "abc123"

    def upload(self, repository, tag, path, replace=False):
        if path.name in self.assets and not replace:
            raise AssertionError("An existing asset would have been overwritten")
        self.events.append(path.name)
        self.uploads.append((path.name, replace))
        if path.name not in self.assets:
            self.current["assets"].append({"name": path.name})
        self.assets[path.name] = path.read_bytes()

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
    def test_shallow_checker_updates_gitlink_without_an_icu_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed, origin, checkout = (root / name for name in ("seed", "origin.git", "checkout"))
            seed.mkdir()
            def git(directory, *args):
                return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                                "-C", str(directory), *args], stderr=subprocess.PIPE, text=True).strip()
            old_sha, new_sha = "1" * 40, "2" * 40
            git(seed, "init", "-b", "main")
            (seed / ".gitmodules").write_text('[submodule "icu"]\n\tpath = icu\n\turl = https://github.com/unicode-org/icu.git\n')
            (seed / "scripts").mkdir()
            (seed / "scripts/tool.py").write_text("# tooling\n")
            git(seed, "add", ".gitmodules", "scripts")
            git(seed, "update-index", "--add", "--cacheinfo", f"160000,{old_sha},icu")
            git(seed, "commit", "-m", "Initial source pin")
            git(root, "clone", "--bare", str(seed), str(origin))
            git(origin, "config", "uploadpack.allowFilter", "true")
            git(root, "clone", "--depth=1", "--no-tags", "--filter=blob:none", "--sparse", origin.as_uri(), str(checkout))
            git(checkout, "sparse-checkout", "set", "scripts")
            self.assertEqual(git(checkout, "rev-list", "--count", "HEAD"), "1")

            class MetadataGitHub:
                current = None
                def releases(self, repository):
                    return [upstream()]
                def release(self, repository, tag):
                    return self.current
                def tag_commit(self, repository, tag):
                    return new_sha
                def submodule_commit(self, repository, ref):
                    return git(origin, "rev-parse", ref + ":icu")
                def api(self, endpoint, method="GET", data=None, **kwargs):
                    if "/contents/" in endpoint:
                        version = "78.3" if endpoint.endswith(old_sha) else "79.1"
                        return {"content": base64.b64encode(f'#define U_ICU_VERSION "{version}"\n'.encode()).decode()}
                    if "/git/ref/tags/" in endpoint:
                        tag = endpoint.rsplit("/", 1)[-1]
                        if git(origin, "tag", "--list", tag):
                            return {"object": {"type": "commit", "sha": git(origin, "rev-parse", tag)}}
                        return None
                    if method == "POST":
                        self.current = dict(data, assets=[])
                        return self.current
                    raise AssertionError(endpoint)

            github = MetadataGitHub()
            real_run = release.run
            with patch.object(release, "ROOT", checkout), \
                    patch.object(release, "run", side_effect=lambda *args, **kwargs: real_run(*args, cwd=checkout)) as run, \
                    patch.object(release, "outputs") as outputs:
                release.check_upstream(github, "owner/repo", "main", rebuild=True)
                first = git(checkout, "rev-parse", "HEAD")
                self.assertEqual(git(checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "icu")
                self.assertEqual(git(origin, "rev-parse", "main:icu"), new_sha)
                self.assertEqual(git(origin, "rev-parse", "release-79.1:icu"), new_sha)
                release.check_upstream(github, "owner/repo", "main", rebuild=True)
                self.assertEqual(git(checkout, "rev-parse", "HEAD"), first)
                outputs.assert_called_with(tag="release-79.1", publish="true")
                self.assertFalse(any("fetch" in c.args or "checkout" in c.args for c in run.call_args_list))
            self.assertFalse((checkout / "icu/.git").exists())
            self.assertFalse(git(checkout, "status", "--porcelain"))
            result = subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", new_sha], capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_gitlink_is_resolved_using_only_contents_metadata(self):
        github = release.GitHub()
        with patch.object(github, "api", return_value={"sha": "abc123", "submodule_git_url": "https://github.com/unicode-org/icu.git"}) as api:
            self.assertEqual(github.submodule_commit("owner/repo", "release-79.1"), "abc123")
            api.assert_called_once_with("repos/owner/repo/contents/icu?ref=release-79.1")
        with patch.object(github, "api", return_value={"sha": "a-directory"}), self.assertRaises(RuntimeError):
            github.submodule_commit("owner/repo", "main")

    def test_upstream_tag_resolution_supports_lightweight_and_annotated_tags(self):
        github = release.GitHub()
        commit = {"type": "commit", "sha": "abc123"}
        with patch.object(github, "api", return_value={"object": commit}) as api:
            self.assertEqual(github.tag_commit("unicode-org/icu", "release-79.1"), "abc123")
            self.assertEqual(api.call_count, 1)
        with patch.object(github, "api", side_effect=[{"object": {"type": "tag", "sha": "annotated"}}, {"object": commit}]) as api:
            self.assertEqual(github.tag_commit("unicode-org/icu", "release-79.1"), "abc123")
            self.assertEqual(api.call_args.args, ("repos/unicode-org/icu/git/tags/annotated",))

    def test_version_lookup_downloads_only_the_header_at_the_exact_commit(self):
        github = release.GitHub()
        header = '#define U_ICU_VERSION "79.1"\n'
        with patch.object(github, "api", return_value={"content": base64.b64encode(header.encode()).decode()}) as api:
            self.assertEqual(release.icu_version("abc123", github), "79.1")
            api.assert_called_once_with("repos/unicode-org/icu/contents/icu4c/source/common/unicode/uvernum.h?ref=abc123")

    @patch.object(release, "run")
    def test_upload_only_clobbers_assets_when_requested(self, run):
        github = release.GitHub()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ICU.xcframework.zip"
            path.write_bytes(b"archive")
            github.upload("owner/repo", "release-79.1", path)
            self.assertNotIn("--clobber", run.call_args.args)
            github.upload("owner/repo", "release-79.1", path, replace=True)
            self.assertEqual(run.call_args.args,
                             ("gh", "release", "upload", "release-79.1", str(path),
                              "--repo", "owner/repo", "--clobber"))
            self.assertTrue(run.call_args.kwargs["stream"])

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
            self.assertFalse(any(c.args[0] == "git" for c in run.call_args_list))

    @patch.object(release, "icu_version", return_value="79.1")
    @patch.object(release, "run", side_effect=fake_command)
    def test_rebuild_replaces_both_assets_and_refreshes_title_and_notes(self, run, version):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact, _, _ = fixture_artifact(directory)
            github = FakeGitHub()
            release.publish(github, "owner/repo", "release-79.1", artifact, directory / "first")
            original_archive = github.assets[release.ARCHIVE]
            github.current["name"] = "Old title"
            old_checksum = hashlib.sha256(original_archive).hexdigest()
            (artifact / "new-toolchain-build").write_text("rebuilt with current scripts")
            github.events.clear()
            github.uploads.clear()
            with patch.object(github, "download", side_effect=AssertionError("must use rebuilt artifact")):
                release.publish(github, "owner/repo", "release-79.1", artifact,
                                directory / "rebuilt", rebuild=True)
            self.assertEqual(github.events, [release.ARCHIVE, release.CHECKSUM, "PATCH"])
            self.assertEqual(github.uploads, [(release.ARCHIVE, True), (release.CHECKSUM, True)])
            self.assertNotEqual(github.assets[release.ARCHIVE], original_archive)
            digest = hashlib.sha256(github.assets[release.ARCHIVE]).hexdigest()
            self.assertIn(digest, github.current["body"])
            self.assertNotIn(old_checksum, github.current["body"])
            self.assertEqual(github.assets[release.CHECKSUM].decode().split()[0], digest)
            self.assertEqual(github.current["name"], "ICU 79.1")
            self.assertTrue(github.current["body"].startswith("Maintainer notes\n\n"))
            self.assertEqual(github.current["body"].count(release.BEGIN), 1)
            self.assertFalse(github.current["draft"])

    @patch.object(release, "icu_version", return_value="79.1")
    @patch.object(release, "run", side_effect=fake_command)
    def test_invalid_rebuild_does_not_replace_existing_assets(self, run, version):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifact, _, _ = fixture_artifact(directory)
            github = FakeGitHub()
            release.publish(github, "owner/repo", "release-79.1", artifact, directory / "first")
            original_assets = dict(github.assets)
            (artifact / "Verification.json").write_text('{"slices": []}')
            github.events.clear()
            with self.assertRaisesRegex(RuntimeError, "verification is incomplete"):
                release.publish(github, "owner/repo", "release-79.1", artifact,
                                directory / "rebuilt", rebuild=True)
            self.assertEqual(github.assets, original_assets)
            self.assertEqual(github.events, [])

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

    @patch.object(release, "icu_version", return_value="79.1")
    @patch.object(release, "outputs")
    @patch.object(release.subprocess, "run", return_value=SimpleNamespace(returncode=0))
    @patch.object(release, "run", side_effect=fake_command)
    def test_rebuild_of_completed_release_publishes_without_commits_or_tag_changes(self, run, diff, outputs, version):
        github = Mock()
        github.releases.return_value = [upstream()]
        github.release.return_value = {"draft": False, "body": release.BEGIN + release.END,
                                       "assets": [{"name": release.ARCHIVE}, {"name": release.CHECKSUM}]}
        github.api.return_value = {"object": {"sha": "existing-snapshot"}}
        github.tag_commit.return_value = "abc123"
        github.submodule_commit.return_value = "abc123"
        release.check_upstream(github, "owner/repo", "main", rebuild=True)
        outputs.assert_called_once_with(tag="release-79.1", publish="true")
        self.assertFalse(any("push" in call.args or "commit" in call.args or "tag" in call.args
                             for call in run.call_args_list))
        self.assertFalse(any(len(c.args) > 1 and c.args[1] == "POST" for c in github.api.call_args_list))

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
        github.tag_commit.return_value = "abc123"
        github.submodule_commit.return_value = "abc123"
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
        github.tag_commit.return_value = "abc123"
        run.side_effect = lambda *args, **kwargs: "old456" if args == ("git", "rev-parse", "HEAD:icu") else fake_command(*args, **kwargs)
        release.check_upstream(github, "owner/repo", "main")
        commits = [call.args for call in run.call_args_list if "commit" in call.args]
        self.assertEqual(len(commits), 1)
        run.assert_any_call("git", "update-index", "--cacheinfo", "160000,abc123,icu")
        self.assertFalse(any("fetch" in c.args or "checkout" in c.args for c in run.call_args_list))
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
