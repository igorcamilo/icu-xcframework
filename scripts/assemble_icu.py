#!/usr/bin/env python3
"""Assemble verified per-variant CI artifacts into one release XCFramework."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

from apple_platforms import XCFRAMEWORK_PLATFORMS, build_matrix
from release import checksum, validate_artifact
from job_log import progress
from frameworks import unpack_parts, validate as validate_framework

COMMON_FIELDS = (
    "builder", "recipe", "source_commit", "xcode", "compiler", "options", "cflags",
    "cxxflags", "cppflags", "data_filter_sha256", "consumer_cppflags", "public_headers_sha256",
    "framework_format", "privacy_manifest_sha256",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def header_hashes(directory):
    return {p.relative_to(directory).as_posix(): checksum(p)
            for p in directory.rglob("*") if p.is_file()}


def collect(parts):
    unpack_parts(parts)
    manifests = sorted(parts.glob("*/ICU.xcframework/BuildManifest.json"))
    require(manifests, "No per-variant XCFramework artifacts found")
    progress(f"Validating {len(manifests)} downloaded platform parts", "assembly")
    combined = None
    report = {"slices": [], "alphabetic_index_runtime": {}, "simulator_runtime": {}}
    libraries = {}
    expected_headers = None
    license_bytes = None
    for path in manifests:
        artifact = path.parent
        manifest = json.loads(path.read_text())
        verification = json.loads((artifact / "Verification.json").read_text())
        if combined is None:
            combined = dict(manifest, targets=[], sdks={}, minimum_versions={})
        require(all(key in manifest and manifest[key] == combined.get(key) for key in COMMON_FIELDS),
                "Parts have different source, toolchain, flags or upstream headers")
        require(not any(p.suffix in (".c", ".cc", ".cpp", ".cxx", ".m", ".mm")
                        for p in artifact.rglob("*") if p.is_file()),
                "Implementation source files must not be packaged")
        with (artifact / "Info.plist").open("rb") as stream:
            entries = plistlib.load(stream)["AvailableLibraries"]
        require(len(entries) == 1, "Each CI part must contain exactly one platform variant")
        entry = entries[0]
        variant = XCFRAMEWORK_PLATFORMS.get((entry["SupportedPlatform"], entry.get("SupportedPlatformVariant")))
        require(variant is not None and variant not in libraries, "Unknown or duplicate platform variant")
        targets = manifest["targets"]
        architectures = set(entry["SupportedArchitectures"])
        require(targets and {t["variant"] for t in targets} == {variant}
                and {t["arch"] for t in targets} == architectures
                and len(targets) == len(architectures), "Part target metadata is inconsistent")
        require({(t["variant"], t["arch"]) for t in verification["slices"]}
                == {(t["variant"], t["arch"]) for t in targets}
                and len(verification["slices"]) == len(targets), "Part verification is incomplete")
        base = artifact / entry["LibraryIdentifier"]
        framework = base / entry["LibraryPath"]
        _, headers = validate_framework(framework, targets[0]["family"])
        inventory = header_hashes(headers)
        require(inventory == manifest["public_headers_sha256"], "Part public headers changed after verification")
        if expected_headers is None:
            expected_headers = inventory
            license_bytes = (artifact / "LICENSE").read_bytes()
        require(inventory == expected_headers and (artifact / "LICENSE").read_bytes() == license_bytes,
                "Part headers or licenses disagree")
        libraries[variant] = (framework, headers, architectures)
        progress(f"{variant}: {', '.join(sorted(architectures))}; headers and verification report validated", "assembly")
        combined["targets"].extend(targets)
        for key in ("sdks", "minimum_versions"):
            for name, value in manifest[key].items():
                require(name not in combined[key] or combined[key][name] == value,
                        f"Parts disagree about {key}/{name}")
                combined[key][name] = value
        report["slices"].extend(verification["slices"])
        for key, value in verification.items():
            if key == "slices":
                continue
            if isinstance(value, dict):
                destination = report.setdefault(key, {})
                for name, result in value.items():
                    require(name not in destination or destination[name] == result,
                            f"Parts have conflicting verification results for {key}/{name}")
                    destination[name] = result
            elif value != "not requested":
                require(key not in report or report[key] == value, f"Conflicting {key} verification")
                report[key] = value
    order = [item["variant"] for item in build_matrix()["include"]]
    require(set(libraries) == set(order), "Missing platform variants")
    combined["targets"].sort(key=lambda t: (order.index(t["variant"]), t["arch"]))
    report["slices"].sort(key=lambda t: (order.index(t["variant"]), t["arch"]))
    combined["built_at"] = datetime.now(timezone.utc).isoformat()
    combined["assembled_parts"] = len(libraries)
    validate_artifact(combined, report, combined["source_commit"])
    return combined, report, libraries, expected_headers, license_bytes


def assemble(parts, output):
    require(output.suffix == ".xcframework" and not output.exists() and not output.is_symlink(),
            "Output must be a new .xcframework path")
    manifest, report, libraries, headers, license_bytes = collect(parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".icu-assembly-", dir=output.parent) as temporary:
        artifact = Path(temporary) / "ICU.xcframework"
        command = ["xcodebuild", "-create-xcframework"]
        for variant in [entry["variant"] for entry in build_matrix()["include"]]:
            framework, include, architectures = libraries[variant]
            family = next(t["family"] for t in manifest["targets"] if t["variant"] == variant)
            archive, _ = validate_framework(framework, family)
            actual = subprocess.check_output(["xcrun", "lipo", "-archs", str(archive)], text=True).split()
            require(set(actual) == architectures, f"Wrong library architectures for {variant}")
            command += ["-framework", str(framework)]
        subprocess.run([*command, "-output", str(artifact)], check=True)
        with (artifact / "Info.plist").open("rb") as stream:
            entries = plistlib.load(stream)["AvailableLibraries"]
        seen = set()
        for entry in entries:
            variant = XCFRAMEWORK_PLATFORMS.get((entry["SupportedPlatform"], entry.get("SupportedPlatformVariant")))
            require(variant in libraries and variant not in seen, "Unexpected assembled platform")
            seen.add(variant)
            original_framework, _, architectures = libraries[variant]
            family = next(t["family"] for t in manifest["targets"] if t["variant"] == variant)
            original, _ = validate_framework(original_framework, family)
            base = artifact / entry["LibraryIdentifier"]
            binary, include = validate_framework(base / entry["LibraryPath"], family)
            require(set(entry["SupportedArchitectures"]) == architectures
                    and checksum(binary) == checksum(original),
                    "Assembly changed a verified static library")
            require(header_hashes(include) == headers,
                    "Assembly changed verified headers")
            progress(f"{variant}: assembled library and headers preserved byte-for-byte", "assembly")
        require(seen == set(libraries), "Assembly omitted a platform")
        (artifact / "BuildManifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (artifact / "Verification.json").write_text(json.dumps(report, indent=2) + "\n")
        (artifact / "LICENSE").write_bytes(license_bytes)
        artifact.rename(output)
    print(f"Assembled {len(libraries)} verified variants into {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parts", type=Path, help="downloaded CI artifacts directory")
    parser.add_argument("--output", type=Path, default=Path("dist/ICU.xcframework"))
    args = parser.parse_args()
    assemble(args.parts.resolve(), args.output.absolute())


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
