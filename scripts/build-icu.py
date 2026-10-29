#!/usr/bin/env python3
"""Build ICU's Apple-platform static XCFramework without modifying ICU."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from apple_platforms import PLATFORMS, version

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "icu" / "icu4c" / "source"
OPTIONS = [
    "--enable-static", "--disable-shared", "--with-data-packaging=static",
    "--disable-tests", "--disable-samples", "--disable-extras",
    "--disable-icuio", "--disable-dyload",
]


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def run(command, cwd, log, env=None):
    with log.open("w") as stream:
        stream.write("Command: " + repr([str(a) for a in command]) + "\n")
        stream.flush()
        result = subprocess.run(command, cwd=cwd, env=env, stdout=stream,
                                stderr=subprocess.STDOUT)
    if result.returncode:
        print("\n".join(log.read_text(errors="replace").splitlines()[-45:]),
              file=sys.stderr)
        raise RuntimeError(f"Command failed; complete log: {log}")


def build(directory, env, options, jobs):
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / ".complete").is_file():
        print(f"Reusing {directory.name}", flush=True)
        return
    print(f"Building {directory.name}; logs: {directory}", flush=True)
    run([str(SOURCE / "configure"), *options, f"--prefix={directory / 'install'}"],
        directory, directory / "configure.log", env)
    if "--disable-tools" in options:
        definitions = (directory / "icudefs.mk").read_text()
        if not re.search(r"^cross_compiling\s*=\s*yes$", definitions, re.M):
            raise RuntimeError(f"Expected cross-compilation in {directory}")
    run(["make", f"-j{jobs}"], directory, directory / "build.log", env)
    run(["make", "install"], directory, directory / "install.log", env)
    (directory / ".complete").touch()


def make_targets(args, sdk_paths, clang):
    targets = []
    with tempfile.TemporaryDirectory(prefix="icu-target-probes-") as temporary:
        probe = Path(temporary) / "probe.o"
        for family in args.platforms:
            minimum = getattr(args, "minimum_" + family)
            for variant, sdk, sdk_target, architectures in PLATFORMS[family]["variants"]:
                settings = json.loads((Path(sdk_paths[sdk]) / "SDKSettings.json").read_text())
                supported = settings["SupportedTargets"][sdk_target]
                if minimum not in supported["ValidDeploymentTargets"]:
                    raise ValueError(f"{variant} does not support deployment target {minimum}")
                candidates = args.simulator_architectures if architectures == "simulator" else architectures
                selected = [arch for arch in dict.fromkeys(candidates) if arch in supported["Archs"]]
                if not selected:
                    raise ValueError(f"No requested architectures available for {variant}")
                for arch in selected:
                    system = supported["LLVMTargetTripleSys"]
                    environment = supported.get("LLVMTargetTripleEnvironment", "")
                    suffix = "-" + environment if environment else ""
                    triple = f"{arch}-apple-{system}{minimum}{suffix}"
                    # Clang can raise a minimum per architecture (e.g. watchOS arm64).
                    subprocess.run([clang, "-target", triple, "-isysroot", sdk_paths[sdk],
                                    "-x", "c", "-c", "-", "-o", str(probe)],
                                   input="int icu_target_probe;\n", text=True, check=True)
                    metadata = capture("xcrun", "otool", "-l", str(probe))
                    actual = re.search(r"\bminos\s+(\S+)", metadata).group(1)
                    platform_id = re.search(r"\bplatform\s+(\S+)", metadata).group(1)
                    expected_id = str(supported["BuildVersionPlatformID"])
                    if platform_id != expected_id:
                        raise ValueError(f"Unexpected platform for {triple}: {platform_id}")
                    if version(actual) != version(minimum):
                        print(f"{variant}/{arch}: compiler raises minimum {minimum} to {actual}", flush=True)
                    targets.append({"family": family, "variant": variant, "sdk": sdk, "arch": arch,
                                    "triple": f"{arch}-apple-{system}{actual}{suffix}",
                                    "platform_id": expected_id, "minimum_os": actual,
                                    "requested_minimum_os": minimum})
    return targets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/ICU.xcframework",
                        help="destination .xcframework path (may contain spaces)")
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build",
                        help="persistent build/cache directory (no whitespace)")
    parser.add_argument("--platforms", nargs="+", choices=list(PLATFORMS), default=list(PLATFORMS),
                        help="Apple app platforms to include (default: all)")
    for family, settings in PLATFORMS.items():
        parser.add_argument("--minimum-" + family, default=settings["minimum"])
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 4, 8))
    parser.add_argument("--parallel-builds", type=int, default=2,
                        help="concurrent architecture builds sharing --jobs (default: 2)")
    parser.add_argument("--simulator-architectures", nargs="+",
                        choices=["arm64", "x86_64"], default=["arm64", "x86_64"])
    parser.add_argument("--data-filter", type=Path,
                        help="optional ICU data filter in JSON format")
    parser.add_argument("--simulator-udid", action="append", default=[],
                        help="also run checks on a booted Simulator; repeat for multiple platforms")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing XCFramework produced by this script")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("macOS with a full Xcode installation is required")
    if args.jobs < 1 or args.parallel_builds < 1:
        parser.error("--jobs and --parallel-builds must be positive")
    args.platforms = list(dict.fromkeys(args.platforms))
    for family in args.platforms:
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", getattr(args, "minimum_" + family)):
            parser.error("minimum deployment versions must have the form 15.0")
    output = args.output.expanduser().absolute()
    build_root = args.build_dir.expanduser().resolve()
    if output.suffix != ".xcframework":
        parser.error("--output must end in .xcframework")
    if output.is_symlink():
        parser.error("--output must not be a symbolic link")
    output = output.resolve()
    for path in (output, build_root):
        if path == ROOT or path == ROOT / "icu" or ROOT / "icu" in path.parents:
            parser.error("output and build directories must be outside the ICU submodule")
    if output == build_root or output in build_root.parents:
        parser.error("the output artifact must not contain the build directory")
    if any(c.isspace() for c in str(SOURCE) + str(build_root)):
        parser.error("ICU's makefiles require source/build paths without whitespace; --output may contain it")
    if output.exists():
        marker = output / "BuildManifest.json"
        if not args.force or not marker.is_file() or json.loads(marker.read_text()).get("builder") != "icu-xcframework":
            parser.error("output exists; --force only replaces artifacts produced by this script")
    if not (SOURCE / "configure").is_file():
        parser.error("initialize ICU first: git submodule update --init icu")
    if capture("git", "-C", str(ROOT / "icu"), "status", "--porcelain"):
        parser.error("the ICU submodule must have a clean working tree")
    filter_bytes = None
    if args.data_filter:
        filter_bytes = args.data_filter.expanduser().read_bytes()
        json.loads(filter_bytes)  # Validate before starting the expensive build.
        if list((SOURCE / "data/in").glob("*.dat")):
            parser.error("prebuilt ICU .dat input would bypass the requested data filter")

    developer = capture("xcode-select", "-p")
    tools = {name: capture("xcrun", "--find", name)
             for name in ("clang", "clang++", "ar", "ranlib", "libtool", "lipo")}
    sdk_names = {sdk for family in args.platforms for _, sdk, _, _ in PLATFORMS[family]["variants"]} | {"macosx"}
    sdk_paths = {name: capture("xcrun", "--sdk", name, "--show-sdk-path") for name in sorted(sdk_names)}
    targets = make_targets(args, sdk_paths, tools["clang"])

    config = {
        "recipe": 2,
        "source_commit": capture("git", "-C", str(ROOT / "icu"), "rev-parse", "HEAD"),
        "xcode": capture("xcodebuild", "-version"),
        "developer_dir": os.environ.get("DEVELOPER_DIR", developer),
        "compiler": capture(tools["clang"], "--version"),
        "sdks": {name: {"path": path, "version": capture("xcrun", "--sdk", name, "--show-sdk-version")}
                 for name, path in sdk_paths.items()},
        "minimum_versions": {family: getattr(args, "minimum_" + family) for family in args.platforms},
        "targets": targets,
        "options": OPTIONS,
        "cflags": "-Os", "cxxflags": "-Os -std=c++17 -fvisibility-inlines-hidden",
        "cppflags": "-DU_STATIC_IMPLEMENTATION",
        "data_filter_sha256": hashlib.sha256(filter_bytes).hexdigest() if filter_bytes else None,
    }
    key = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]
    build_root.mkdir(parents=True, exist_ok=True)
    # Serialize users of the same cache; never remove another build's directory.
    with (build_root / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        work = build_root / key
        if output == work or output in work.parents:
            parser.error("the output artifact must not contain the build cache")
        work.mkdir(exist_ok=True)
        (work / "configuration.json").write_text(json.dumps(config, indent=2) + "\n")
        env = os.environ.copy()
        for name in ("CC", "CXX", "CPP", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS",
                     "AR", "ARFLAGS", "RANLIB", "LD", "SDKROOT", "CPATH", "C_INCLUDE_PATH",
                     "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "DYLD_LIBRARY_PATH", "ICU_DATA",
                     "ICU_DATA_FILTER_FILE", "ICU_DATA_BUILDTOOL_OPTS", "MACOSX_DEPLOYMENT_TARGET",
                     "IPHONEOS_DEPLOYMENT_TARGET", "TVOS_DEPLOYMENT_TARGET",
                     "WATCHOS_DEPLOYMENT_TARGET", "XROS_DEPLOYMENT_TARGET"):
            env.pop(name, None)
        env.update(CC=tools["clang"], CXX=tools["clang++"], AR=tools["ar"],
                   RANLIB=tools["ranlib"], CPPFLAGS=config["cppflags"],
                   PYTHONDONTWRITEBYTECODE="1", PYTHON=capture("xcrun", "--find", "python3"))
        if filter_bytes:
            profile = work / "data-filter.json"
            profile.write_bytes(filter_bytes)
            env["ICU_DATA_FILTER_FILE"] = str(profile)

        host = work / "host"
        host_env = dict(env, CFLAGS=f"{config['cflags']} -isysroot {sdk_paths['macosx']}",
                        CXXFLAGS=f"{config['cxxflags']} -isysroot {sdk_paths['macosx']}")
        build(host, host_env, OPTIONS, args.jobs)
        build_triple = capture("sh", str(SOURCE / "config.guess"))
        workers = min(args.parallel_builds, args.jobs, len(targets))

        def build_target(target):
            directory = work / f"{target['variant']}-{target['arch']}"
            flags = f"-target {target['triple']} -isysroot {sdk_paths[target['sdk']]}"
            target_env = dict(env, CFLAGS=f"{config['cflags']} {flags}",
                              CXXFLAGS=f"{config['cxxflags']} {flags}")
            cpu = "x86_64" if target["arch"] == "x86_64" else "aarch64"
            build(directory, target_env,
                  [*OPTIONS, "--disable-tools", f"--build={build_triple}",
                   f"--host={cpu}-apple-darwin", f"--with-cross-build={host}"], max(1, args.jobs // workers))
            run([tools["libtool"], "-static", "-o", str(directory / "libICU.a"),
                 *[str(directory / "lib" / name) for name in
                   ("libicui18n.a", "libicuuc.a", "libicudata.a")]],
                work, directory / "combine.log", env)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            failures = []
            futures = [pool.submit(build_target, target) for target in targets]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    failures.append(str(error))
            if failures:
                raise RuntimeError("Architecture builds failed:\n" + "\n".join(failures))

        headers = work / "headers"
        if headers.exists():
            shutil.rmtree(headers)
        headers.mkdir()
        first = targets[0]
        installed_headers = work / f"{first['variant']}-{first['arch']}/install/include/unicode"
        shutil.copytree(installed_headers, headers / "unicode")
        for target in targets[1:]:
            other = work / f"{target['variant']}-{target['arch']}/install/include/unicode"
            if {p.name: p.read_bytes() for p in other.glob("*.h")} != {
                    p.name: p.read_bytes() for p in installed_headers.glob("*.h")}:
                raise RuntimeError("Installed headers differ between architectures")
        upstream_headers = {}
        for component in ("common", "i18n"):
            for header in (SOURCE / component / "unicode").rglob("*.h"):
                name = "unicode/" + header.relative_to(SOURCE / component / "unicode").as_posix()
                contents = header.read_bytes()
                if name in upstream_headers and upstream_headers[name] != contents:
                    raise RuntimeError(f"Conflicting upstream public header: {name}")
                upstream_headers[name] = contents
        installed = {p.relative_to(headers).as_posix(): p.read_bytes()
                     for p in headers.rglob("*") if p.is_file()}
        if installed != upstream_headers:
            raise RuntimeError("Installed public headers must exactly match the upstream source headers")
        public_header_hashes = {name: hashlib.sha256(contents).hexdigest()
                                for name, contents in sorted(upstream_headers.items())}
        (headers / "module.modulemap").write_text(
            'module ICU4C [system] {\n  umbrella "unicode"\n  export *\n  link "c++"\n}\n')

        products = work / "products"
        products.mkdir(exist_ok=True)
        command = ["xcodebuild", "-create-xcframework"]
        for variant in dict.fromkeys(t["variant"] for t in targets):
            thin = [work / f"{t['variant']}-{t['arch']}/libICU.a"
                    for t in targets if t["variant"] == variant]
            product = products / variant
            product.mkdir(exist_ok=True)
            archive = product / "libICU.a"
            if len(thin) == 1:
                shutil.copy2(thin[0], archive)
            else:
                run([tools["lipo"], "-create", *map(str, thin), "-output", str(archive)],
                    work, product / "lipo.log", env)
            command += ["-library", str(archive), "-headers", str(headers)]

        output.parent.mkdir(parents=True, exist_ok=True)
        # Build/verify alongside the destination so a successful rename stays on one filesystem.
        with tempfile.TemporaryDirectory(prefix=".icu-package-", dir=output.parent) as staging:
            artifact = Path(staging) / "ICU.xcframework"
            print("Packaging and verifying ICU.xcframework", flush=True)
            run([*command, "-output", str(artifact)], work, work / "package.log", env)
            manifest = dict(config, builder="icu-xcframework",
                            public_headers_sha256=public_header_hashes,
                            consumer_cppflags=["-DU_STATIC_IMPLEMENTATION"],
                            built_at=datetime.now(timezone.utc).isoformat())
            (artifact / "BuildManifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            shutil.copy2(ROOT / "icu/icu4c/LICENSE", artifact / "LICENSE")
            if filter_bytes:
                (artifact / "DataFilter.json").write_bytes(filter_bytes)
            verification = [sys.executable, "-B", str(ROOT / "scripts/verify-icu.py"), str(artifact),
                            "--work-dir", str(work / "verification"), "--host-build", str(host)]
            for udid in args.simulator_udid:
                verification += ["--simulator-udid", udid]
            run(verification,
                work, work / "verify.log", env)
            shutil.copy2(work / "verification/results.json", artifact / "Verification.json")
            if capture("git", "-C", str(ROOT / "icu"), "status", "--porcelain"):
                raise RuntimeError("The build unexpectedly changed the ICU source tree")
            if output.exists():
                marker = output / "BuildManifest.json"
                if (not args.force or output.is_symlink() or not marker.is_file()
                        or json.loads(marker.read_text()).get("builder") != "icu-xcframework"):
                    raise RuntimeError("Destination appeared during the build; refusing to replace it")
                # The original artifact survives all build/verification failures.
                backup = Path(staging) / "previous.xcframework"
                output.rename(backup)
                try:
                    artifact.rename(output)
                except OSError:
                    backup.rename(output)
                    raise
            else:
                artifact.rename(output)
        print(f"Created {output}\nVerification report: {output / 'Verification.json'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError) as error:
        sys.exit(str(error))
