import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assemble_icu
from apple_platforms import PLATFORMS, build_matrix

spec = importlib.util.spec_from_file_location("build_icu", Path(assemble_icu.__file__).with_name("build-icu.py"))
build_icu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_icu)


def fixture_parts(root):
    header = b"upstream header\n"
    paths = {}
    for family, settings in PLATFORMS.items():
        for variant, sdk, _, architectures in settings["variants"]:
            if architectures == "simulator":
                architectures = ["arm64", "x86_64"]
            artifact = root / ("icu-part-" + variant) / "ICU.xcframework"
            headers = artifact / variant / "Headers"
            (headers / "unicode").mkdir(parents=True)
            (headers / "unicode/utypes.h").write_bytes(header)
            (headers / "module.modulemap").write_text('module ICU4C { umbrella "unicode" }\n')
            (artifact / variant / "libICU.a").write_bytes(b"static library fixture")
            platform = "xros" if family == "visionos" else "ios" if family == "maccatalyst" else family
            entry = dict(LibraryIdentifier=variant, LibraryPath="libICU.a", HeadersPath="Headers",
                         SupportedArchitectures=architectures, SupportedPlatform=platform)
            if family == "maccatalyst":
                entry["SupportedPlatformVariant"] = "maccatalyst"
            elif variant.endswith("-simulator"):
                entry["SupportedPlatformVariant"] = "simulator"
            (artifact / "Info.plist").write_bytes(plistlib.dumps({"AvailableLibraries": [entry]}))
            (artifact / "LICENSE").write_text("Upstream license\n")
            targets = [dict(family=family, variant=variant, arch=arch) for arch in architectures]
            manifest = dict(builder="icu-xcframework", recipe=2, source_commit="abc123", xcode="Xcode 27.2",
                            compiler="Apple clang", options=["--enable-static"], cflags="-Os", cxxflags="-Os -std=c++17",
                            cppflags="-DU_STATIC_IMPLEMENTATION", consumer_cppflags=["-DU_STATIC_IMPLEMENTATION"],
                            data_filter_sha256=None, public_headers_sha256={"unicode/utypes.h": hashlib.sha256(header).hexdigest()},
                            targets=targets, sdks={sdk: {"path": "/SDK/" + sdk, "version": "27.2"}},
                            minimum_versions={family: settings["minimum"]})
            verification = dict(slices=[dict(t, c_cpp_swift_link="passed", same_object_dead_stripping="passed",
                                             upstream_public_headers=1, alphabetic_index_cpp_link="passed") for t in targets],
                                native_runtime="passed", macos_runtime="not requested", maccatalyst_runtime="not requested",
                                alphabetic_index_runtime={"native-cpp": ["passed"]}, simulator_runtime={})
            if family in ("macos", "maccatalyst"):
                verification[family + "_runtime"] = "passed"
            (artifact / "BuildManifest.json").write_text(json.dumps(manifest))
            (artifact / "Verification.json").write_text(json.dumps(verification))
            paths[variant] = artifact
    return paths


def edit_json(path, edit):
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))


class AssemblyTests(unittest.TestCase):
    def test_collects_all_architectures_and_runtime_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_parts(root)
            manifest, report, libraries, _, license_bytes = assemble_icu.collect(root)
            self.assertEqual(len(libraries), 10)
            self.assertEqual(len(manifest["targets"]), 17)
            self.assertEqual(len(report["slices"]), 17)
            self.assertEqual(report["macos_runtime"], "passed")
            self.assertEqual(report["maccatalyst_runtime"], "passed")
            self.assertEqual(report["alphabetic_index_runtime"]["native-cpp"], ["passed"])
            self.assertEqual(license_bytes, b"Upstream license\n")
            self.assertEqual({entry["variant"] for entry in build_matrix()["include"]}, set(libraries))

    def test_rejects_missing_and_duplicate_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = fixture_parts(root)
            shutil.copytree(paths["ios"].parent, root / "second-copy")
            with self.assertRaisesRegex(RuntimeError, "duplicate platform"):
                assemble_icu.collect(root)
            shutil.rmtree(root / "second-copy")
            shutil.rmtree(paths["ios"].parent)
            with self.assertRaisesRegex(RuntimeError, "Missing platform"):
                assemble_icu.collect(root)

    def test_rejects_parts_from_other_sources_or_toolchains(self):
        for field, value in (("source_commit", "different"), ("compiler", "different"),
                             ("cxxflags", "-O0"), ("data_filter_sha256", "filtered")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = fixture_parts(root)
                edit_json(paths["ios"] / "BuildManifest.json", lambda data: data.update({field: value}))
                with self.assertRaisesRegex(RuntimeError, "different source, toolchain"):
                    assemble_icu.collect(root)

    def test_rejects_modified_headers_or_module_maps(self):
        for filename in ("unicode/utypes.h", "module.modulemap"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = fixture_parts(root)
                (paths["ios"] / "ios/Headers" / filename).write_text("changed")
                with self.assertRaisesRegex(RuntimeError, "headers.*(changed|disagree)"):
                    assemble_icu.collect(root)

    def test_rejects_missing_or_failed_verification(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = fixture_parts(root)
                def edit(data):
                    if failed:
                        data["slices"][0]["same_object_dead_stripping"] = "failed"
                    else:
                        data["slices"].clear()
                edit_json(paths["ios"] / "Verification.json", edit)
                with self.assertRaisesRegex(RuntimeError, "verification is incomplete"):
                    assemble_icu.collect(root)

    def test_assembly_rejects_changed_archive_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = fixture_parts(root / "parts")
            output = root / "ICU.xcframework"
            def architectures(command, **kwargs):
                variant = Path(command[-1]).parent.name
                info = plistlib.loads((paths[variant] / "Info.plist").read_bytes())
                return " ".join(info["AvailableLibraries"][0]["SupportedArchitectures"])
            def package(command, **kwargs):
                destination = Path(command[-1])
                destination.mkdir()
                entries = []
                for variant, part in paths.items():
                    shutil.copytree(part / variant, destination / variant)
                    entries.extend(plistlib.loads((part / "Info.plist").read_bytes())["AvailableLibraries"])
                (destination / "Info.plist").write_bytes(plistlib.dumps({"AvailableLibraries": entries}))
                (destination / "ios/libICU.a").write_bytes(b"unexpected changes during packaging")
            with patch.object(assemble_icu.subprocess, "check_output", side_effect=architectures), \
                    patch.object(assemble_icu.subprocess, "run", side_effect=package):
                with self.assertRaisesRegex(RuntimeError, "Assembly changed a verified static library"):
                    assemble_icu.assemble(root / "parts", output)
            self.assertFalse(output.exists())

    def test_variant_selection_probes_only_requested_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            (sdk / "SDKSettings.json").write_text(json.dumps({"SupportedTargets": {"iphonesimulator": {
                "ValidDeploymentTargets": ["15.0"], "Archs": ["arm64", "x86_64"], "LLVMTargetTripleSys": "ios",
                "LLVMTargetTripleEnvironment": "simulator", "BuildVersionPlatformID": 7}}}))
            args = SimpleNamespace(platforms=["ios"], variants=["ios-simulator"], minimum_ios="15.0",
                                   simulator_architectures=["arm64", "x86_64"])
            with patch.object(build_icu.subprocess, "run"), \
                    patch.object(build_icu, "capture", return_value="platform 7\nminos 15.0"):
                targets = build_icu.make_targets(args, {"iphonesimulator": str(sdk)}, "clang")
            self.assertEqual([(t["variant"], t["arch"]) for t in targets],
                             [("ios-simulator", "arm64"), ("ios-simulator", "x86_64")])


if __name__ == "__main__":
    unittest.main()
