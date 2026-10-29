import os
from pathlib import Path
import plistlib
import shutil
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import frameworks
import release
from test_release import fixture_artifact


class FrameworkTests(unittest.TestCase):
    def create(self, root, family):
        source = root / "source"
        (source / "unicode").mkdir(parents=True)
        (source / "unicode/upstream.h").write_bytes(b"unchanged upstream header\n")
        archive = root / "library.a"
        archive.write_bytes(b"!<arch>\nstatic archive contents")
        return frameworks.create(archive, source, root / "ICU4C.framework", family, "79.1")

    def test_framework_layout_preserves_archive_headers_and_privacy_manifest(self):
        for family in ("ios", "macos", "maccatalyst", "watchos", "tvos", "visionos"):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                framework = self.create(root, family)
                archive, headers = frameworks.validate(framework, family)
                self.assertEqual(archive.read_bytes(), (root / "library.a").read_bytes())
                self.assertEqual((headers / "unicode/upstream.h").read_bytes(), b"unchanged upstream header\n")
                self.assertEqual((headers / "unicode/unicode/upstream.h").read_bytes(), b"unchanged upstream header\n")
                _, _, _, resources = frameworks.paths(framework)
                manifest = plistlib.loads((resources / "PrivacyInfo.xcprivacy").read_bytes())
                self.assertFalse(manifest["NSPrivacyTracking"])
                self.assertEqual(manifest["NSPrivacyCollectedDataTypes"], [])
                self.assertEqual(manifest["NSPrivacyAccessedAPITypes"][0]["NSPrivacyAccessedAPITypeReasons"], ["C617.1"])
                if family in ("macos", "maccatalyst"):
                    self.assertEqual(resources.resolve(), (framework / "Versions/A/Resources").resolve())
                else:
                    self.assertEqual(resources, framework)

    def test_missing_privacy_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            framework = self.create(Path(temporary), "ios")
            (framework / "PrivacyInfo.xcprivacy").write_bytes(plistlib.dumps({}))
            with self.assertRaisesRegex(RuntimeError, "privacy manifest"):
                frameworks.validate(framework, "ios")

    def test_ci_transfer_preserves_versioned_framework_and_header_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            framework = self.create(root, "macos")
            package = root / "ICU.xcframework/macos/ICU4C.framework"
            package.parent.mkdir(parents=True)
            shutil.copytree(framework, package, symlinks=True)
            destination = root / "parts/icu-part-macos"
            destination.mkdir(parents=True)
            with tarfile.open(destination / "icu-part.tar", "w") as target:
                target.add(root / "ICU.xcframework", arcname="ICU.xcframework")
            frameworks.unpack_parts(root / "parts")
            restored = destination / "ICU.xcframework/macos/ICU4C.framework"
            frameworks.validate(restored, "macos")
            self.assertTrue((restored / "Headers").is_symlink())
            self.assertEqual(os.readlink(restored / "Headers/unicode/unicode"), ".")

    def test_release_zip_preserves_framework_links_as_unix_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact, _, _ = fixture_artifact(root)
            framework = self.create(root, "macos")
            shutil.copytree(framework, artifact / "macos/ICU4C.framework", symlinks=True)
            archive = root / release.ARCHIVE
            release.bundle(artifact, archive, "abc123")
            with zipfile.ZipFile(archive) as source:
                link = source.getinfo("ICU.xcframework/macos/ICU4C.framework/Versions/Current")
                self.assertEqual(stat.S_IFMT(link.external_attr >> 16), stat.S_IFLNK)
                self.assertEqual(source.read(link), b"A")
                self.assertIn("ICU.xcframework/macos/ICU4C.framework/Versions/A/Resources/PrivacyInfo.xcprivacy", source.namelist())


if __name__ == "__main__":
    unittest.main()
