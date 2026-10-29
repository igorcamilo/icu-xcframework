#!/usr/bin/env python3
"""Verify packaged Apple slices, AlphabeticIndex, language consumers, and stripping."""

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import platform
import re
import subprocess
import sys

from apple_platforms import XCFRAMEWORK_PLATFORMS, version
from job_log import progress, stream_command

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests/consumers"


def command(*args):
    # Keep machine-readable symbol/object inventories out of the console, but
    # print their commands and every compiler diagnostic/runtime test message.
    inspection = len(args) > 1 and str(args[1]) in ("nm", "otool", "ar", "lipo", "--sdk")
    return stream_command(args, label="check", capture=True, show_output=not inspection)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def symbols(executable):
    return set(command("xcrun", "nm", "-j", executable).splitlines())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True,
                        help="directory for linked consumers, maps and results.json")
    parser.add_argument("--host-build", type=Path,
                        help="also run runtime tests using this native ICU build")
    parser.add_argument("--simulator-udid", action="append", default=[],
                        help="also run consumers on a booted Simulator; repeat for multiple platforms")
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    (work / "results.json").unlink(missing_ok=True)
    manifest = json.loads((artifact / "BuildManifest.json").read_text())
    with (artifact / "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    report = {"slices": [], "native_runtime": "not requested", "macos_runtime": "not requested",
              "maccatalyst_runtime": "not requested",
              "simulator_runtime": {}, "alphabetic_index_runtime": {}}
    require(not any(p.suffix in (".c", ".cc", ".cpp", ".cxx", ".m", ".mm")
                    for p in artifact.rglob("*") if p.is_file()),
            "Implementation source files must not be packaged")
    available = info["AvailableLibraries"]
    require(len(available) == len({t["variant"] for t in manifest["targets"]}),
            "Unexpected count of platform libraries")
    expected = {(t["variant"], t["arch"]) for t in manifest["targets"]}
    progress(f"Verifying {len(expected)} architecture slices from ICU {manifest['source_commit']}", "verify")
    found = set()
    simulator_programs = {}
    for library in available:
        variant = XCFRAMEWORK_PLATFORMS.get((library["SupportedPlatform"], library.get("SupportedPlatformVariant")))
        require(variant is not None, "Unexpected platform/variant")
        base = artifact / library["LibraryIdentifier"]
        archive = base / library["LibraryPath"]
        headers = base / library["HeadersPath"]
        packaged_headers = {p.relative_to(headers).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in headers.rglob("*") if p.is_file() and p.name != "module.modulemap"}
        require(packaged_headers == manifest["public_headers_sha256"],
                "Packaged public headers differ from the unmodified upstream headers")
        require(all(name.startswith("unicode/") for name in packaged_headers),
                "Custom public headers must not be packaged")
        progress(f"{len(packaged_headers)} public headers match upstream byte-for-byte", variant)
        require(archive.suffix == ".a", "Expected a static archive")
        major = re.search(r"^#define U_ICU_VERSION_MAJOR_NUM (\d+)$",
                          (headers / "unicode/uvernum.h").read_text(), re.M).group(1)
        actual_archs = set(command("xcrun", "lipo", "-archs", archive).split())
        require(actual_archs == set(library["SupportedArchitectures"]), "XCFramework architecture metadata mismatch")
        for arch in sorted(actual_archs):
            label = f"{variant}/{arch}"
            progress("Checking platform, deployment target, archive members and static data", label)
            found.add((variant, arch))
            target = next((t for t in manifest["targets"]
                           if (t["variant"], t["arch"]) == (variant, arch)), None)
            require(target is not None, f"Unexpected slice: {variant}/{arch}")
            directory = work / f"{variant}-{arch}"
            directory.mkdir(exist_ok=True)
            thin = directory / "libICU.a"
            if len(actual_archs) > 1:
                command("xcrun", "lipo", archive, "-thin", arch, "-output", thin)
            else:
                thin.write_bytes(archive.read_bytes())
            members = command("xcrun", "ar", "-t", thin).splitlines()
            require("ustring.ao" in members, "ICU common objects missing")
            require("icudt" + major + "l_dat.o" in members, "Real static ICU data missing")
            require("stubdata.ao" not in members, "Bootstrap stubdata must not be packaged")
            metadata = command("xcrun", "otool", "-l", thin)
            platforms = re.findall(r"\bplatform\s+(\S+)", metadata)
            permitted = {target["platform_id"]}
            require(platforms and set(platforms) <= permitted,
                    f"Wrong Mach-O platform in {variant}/{arch}: {set(platforms)}")
            minimums = re.findall(r"\bminos\s+(\S+)", metadata)
            require(minimums and all(version(v) == version(target["minimum_os"]) for v in minimums),
                    "Inconsistent minimum deployment target")
            progress("Extracting ustring.ao to inspect function-level stripping metadata", label)
            with (directory / "ustring.o").open("wb") as stream:
                subprocess.run(["xcrun", "ar", "-p", str(thin), "ustring.ao"], stdout=stream, check=True)
            require("SUBSECTIONS_VIA_SYMBOLS" in command("xcrun", "otool", "-hv", directory / "ustring.o"),
                    "Missing function-level Mach-O stripping metadata")
            sdk = command("xcrun", "--sdk", target["sdk"], "--show-sdk-path")
            flags = ["-target", target["triple"], "-isysroot", sdk, "-Os",
                     "-DU_STATIC_IMPLEMENTATION", "-I", str(headers)]
            sizes = {}
            progress("Linking C consumers and checking dead-code stripping", label)
            for fixture, strip in (("empty", True), ("string", True), ("string", False)):
                name = fixture + ("-strip" if strip else "-no-strip")
                binary = directory / name
                link_flags = ["-Wl,-dead_strip"] if strip else []
                command("xcrun", "clang", "-std=c11", *flags, FIXTURES / f"{fixture}.c",
                        thin, "-lc++", *link_flags, f"-Wl,-map,{binary}.map", "-o", binary)
                names = symbols(binary)
                if fixture == "empty":
                    require(not any(s.endswith("_" + major) for s in names), "Unused ICU linked into empty consumer")
                else:
                    require("_u_strlen_" + major in names, "Consumer did not link packaged ICU")
                    require(("_u_strcmp_" + major in names) == (not strip),
                            "Unused same-object function was not stripped as expected")
                    for unused in ("_ucol_open_", "_unum_open_", "_uregex_open_"):
                        require(unused + major not in names, "Unrelated ICU API retained")
                require("_icudt" + major + "_dat" not in names, "String/empty consumer retained locale data")
                require(not any("AlphabeticIndex" in s for s in names), "Unused AlphabeticIndex retained")
                sizes[name] = binary.stat().st_size
            require(sizes["string-strip"] < sizes["string-no-strip"], "Stripping did not reduce executable size")
            progress(f"Dead stripping passed: {sizes['string-strip']} bytes stripped, "
                     f"{sizes['string-no-strip']} bytes unstripped", label)
            progress("Linking upstream C++ APIs, feature smoke tests and AlphabeticIndex", label)
            for name in ("cpp", "smoke", "alphabetic-index"):
                command("xcrun", "clang++", "-std=c++17", *flags, FIXTURES / f"{name}.cpp",
                        thin, "-Wl,-dead_strip", "-o", directory / name)
            require(any("icu_" + major in s and "AlphabeticIndex" in s
                        for s in symbols(directory / "alphabetic-index")),
                    "C++ consumer did not link upstream ICU AlphabeticIndex")
            require("_icudt" + major + "_dat" in symbols(directory / "alphabetic-index"),
                    "AlphabeticIndex consumer is missing packaged collation/locale data")
            swift_source = directory / "import.swift"
            progress("Linking Swift + Foundation and Swift C++ interoperability consumers", label)
            swift_source.write_text((FIXTURES / "import.swift.in").read_text().replace("@ICU_MAJOR@", major))
            command("xcrun", "swiftc", "-target", target["triple"], "-sdk", sdk, "-O",
                    "-Xcc", "-DU_STATIC_IMPLEMENTATION",
                    "-I", headers, "-module-cache-path", directory / "module-cache",
                    swift_source, thin, "-Xlinker", "-dead_strip", "-o", directory / "swift")
            require("_u_strlen_" + major in symbols(directory / "swift"), "Swift did not link packaged ICU")
            cpp_swift_source = directory / "cpp.swift"
            cpp_swift_source.write_text((FIXTURES / "cpp.swift.in").read_text().replace("@ICU_MAJOR@", major))
            command("xcrun", "swiftc", "-target", target["triple"], "-sdk", sdk, "-O",
                    "-cxx-interoperability-mode=default", "-Xcc", "-std=c++17",
                    "-Xcc", "-DU_STATIC_IMPLEMENTATION",
                    "-I", headers, "-module-cache-path", directory / "cxx-module-cache",
                    cpp_swift_source, thin, "-Xlinker", "-dead_strip", "-o", directory / "swift-cpp")
            report["slices"].append({"variant": variant, "arch": arch, "minimum_os": target["minimum_os"],
                                     "consumer_bytes": sizes, "c_cpp_swift_link": "passed",
                                     "upstream_swift_cpp_link": "passed",
                                     "upstream_public_headers": len(packaged_headers),
                                     "alphabetic_index_cpp_link": "passed",
                                     "alphabetic_index_cpp_bytes": (directory / "alphabetic-index").stat().st_size,
                                     "same_object_dead_stripping": "passed"})
            if variant.endswith("-simulator"):
                simulator_programs[(variant, arch)] = directory
            if variant in ("macos", "maccatalyst") and arch == platform.machine():
                progress("Running packaged C/C++/Swift and AlphabeticIndex consumers", label)
                for name in ("string-strip", "cpp", "swift", "swift-cpp", "smoke", "alphabetic-index"):
                    if name in ("smoke", "alphabetic-index") and manifest["data_filter_sha256"]:
                        continue
                    result = command(directory / name)
                    if "alphabetic-index" in name:
                        report["alphabetic_index_runtime"][variant + "-" + name] = result.splitlines()
                report[variant + "_runtime"] = "passed on packaged " + arch + " slice"
            progress("PASS: metadata, upstream headers, static data, C/C++/Swift, AlphabeticIndex and stripping", label)

    require(found == expected, "Missing or extra platform/architecture slices")
    headers = artifact / available[0]["LibraryIdentifier"] / available[0]["HeadersPath"]
    if args.host_build:
        progress("Running native C/C++/Swift, feature smoke tests and AlphabeticIndex", "native")
        native = work / "native"
        native.mkdir(exist_ok=True)
        host = args.host_build.resolve()
        archives = [host / "lib" / name for name in ("libicui18n.a", "libicuuc.a", "libicudata.a")]
        # C, C++, and Swift use packaged headers, not source-tree search paths.
        command("xcrun", "clang", "-Os", "-DU_STATIC_IMPLEMENTATION", "-I", headers, FIXTURES / "string.c",
                *archives, "-lc++", "-Wl,-dead_strip", "-o", native / "string")
        command(native / "string")
        for fixture in ("cpp", "smoke", "alphabetic-index"):
            command("xcrun", "clang++", "-std=c++17", "-Os", "-DU_STATIC_IMPLEMENTATION", "-I", headers,
                    FIXTURES / f"{fixture}.cpp", *archives, "-Wl,-dead_strip", "-o", native / fixture)
            if fixture == "cpp" or not manifest["data_filter_sha256"]:
                result = command(native / fixture)
                if fixture == "alphabetic-index":
                    report["alphabetic_index_runtime"]["native-cpp"] = result.splitlines()
        swift_source = native / "import.swift"
        swift_source.write_text((FIXTURES / "import.swift.in").read_text().replace("@ICU_MAJOR@", major))
        command("xcrun", "swiftc", "-O", "-Xcc", "-DU_STATIC_IMPLEMENTATION",
                "-I", headers, "-module-cache-path", native / "module-cache",
                swift_source, *archives, "-Xlinker", "-dead_strip", "-o", native / "swift")
        command(native / "swift")
        cpp_swift_source = native / "cpp.swift"
        cpp_swift_source.write_text((FIXTURES / "cpp.swift.in").read_text().replace("@ICU_MAJOR@", major))
        command("xcrun", "swiftc", "-O", "-cxx-interoperability-mode=default", "-Xcc", "-std=c++17",
                "-Xcc", "-DU_STATIC_IMPLEMENTATION",
                "-I", headers, "-module-cache-path", native / "cxx-module-cache",
                cpp_swift_source, *archives, "-Xlinker", "-dead_strip", "-o", native / "swift-cpp")
        command(native / "swift-cpp")
        report["native_runtime"] = "C/C++/Swift passed; " + (
            "broad smoke skipped for custom data filter" if manifest["data_filter_sha256"] else "all feature smoke tests passed")
        progress(report["native_runtime"], "native")
    for udid in args.simulator_udid:
        progress(f"Running consumers in Simulator {udid}", "verify")
        arch = command("xcrun", "simctl", "getenv", udid, "SIMULATOR_ARCHS")
        devices = json.loads(command("xcrun", "simctl", "list", "devices", "-j"))["devices"]
        runtime = next((key for key, values in devices.items() if any(d["udid"] == udid for d in values)), "")
        family = next((f for token, f in (("iOS-", "ios"), ("tvOS-", "tvos"),
                                         ("watchOS-", "watchos"), ("xrOS-", "visionos"))
                       if ".SimRuntime." + token in runtime), None)
        directory = simulator_programs.get((str(family) + "-simulator", arch))
        require(directory is not None, f"No {family} Simulator slice for architecture {arch}")
        for name in ("string-strip", "cpp", "swift", "swift-cpp", "smoke", "alphabetic-index"):
            if name in ("smoke", "alphabetic-index") and manifest["data_filter_sha256"]:
                continue
            result = command("xcrun", "simctl", "spawn", udid, directory / name)
            if "alphabetic-index" in name:
                report["alphabetic_index_runtime"][str(family) + "-" + name] = result.splitlines()
        report["simulator_runtime"][udid] = "passed on " + str(family)
    (work / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    progress(f"All {len(report['slices'])} slices passed; report: {work / 'results.json'}", "verify")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError) as error:
        sys.exit(str(error))
