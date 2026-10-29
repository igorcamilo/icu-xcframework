"""Static-framework layout and privacy metadata; ICU headers stay unchanged."""

import hashlib
import os
import plistlib
import shutil
import tarfile

from job_log import progress

NAME = "ICU4C"
FORMAT = "static-framework-v1"
MODULE_MAP = b'framework module ICU4C [system] {\n  umbrella "Headers/unicode"\n  export *\n  link "c++"\n}\n'
PRIVACY = {
    "NSPrivacyTracking": False,
    "NSPrivacyTrackingDomains": [],
    "NSPrivacyCollectedDataTypes": [],
    "NSPrivacyAccessedAPITypes": [{
        "NSPrivacyAccessedAPIType": "NSPrivacyAccessedAPICategoryFileTimestamp",
        "NSPrivacyAccessedAPITypeReasons": ["C617.1"],
    }],
}
PRIVACY_BYTES = plistlib.dumps(PRIVACY)
PRIVACY_SHA256 = hashlib.sha256(PRIVACY_BYTES).hexdigest()


def paths(framework):
    resources = framework / "Resources" if (framework / "Versions").is_dir() else framework
    return framework / NAME, framework / "Headers", framework / "Modules/module.modulemap", resources


def create(archive, headers, framework, family, version):
    if framework.exists():
        shutil.rmtree(framework)
    versioned = family in ("macos", "maccatalyst")
    contents = framework / "Versions/A" if versioned else framework
    resources = contents / "Resources" if versioned else contents
    resources.mkdir(parents=True)
    shutil.copy2(archive, contents / NAME)
    # The include-directory link lets unmodified ICU headers resolve their
    # quoted unicode/... includes within the framework, without SDK ICU mixing.
    shutil.copytree(headers / "unicode", contents / "Headers/unicode")
    (contents / "Headers/unicode/unicode").symlink_to(".", target_is_directory=True)
    (contents / "Modules").mkdir()
    (contents / "Modules/module.modulemap").write_bytes(MODULE_MAP)
    (resources / "PrivacyInfo.xcprivacy").write_bytes(PRIVACY_BYTES)
    info = {"CFBundleIdentifier": "org.unicode.icu4c", "CFBundleName": NAME,
            "CFBundleExecutable": NAME, "CFBundlePackageType": "FMWK",
            "CFBundleVersion": version, "CFBundleShortVersionString": version,
            "CFBundleInfoDictionaryVersion": "6.0"}
    (resources / "Info.plist").write_bytes(plistlib.dumps(info))
    if versioned:
        (framework / "Versions/Current").symlink_to("A", target_is_directory=True)
        for name in (NAME, "Headers", "Modules", "Resources"):
            (framework / name).symlink_to("Versions/Current/" + name,
                                         target_is_directory=name != NAME)
    return framework


def validate(framework, family):
    archive, headers, module, resources = paths(framework)
    if framework.name != NAME + ".framework" or not archive.is_file():
        raise RuntimeError("Missing ICU4C static framework")
    if family in ("macos", "maccatalyst") and resources.resolve() != (framework / "Versions/A/Resources").resolve():
        raise RuntimeError("macOS/Catalyst framework resources must be versioned")
    if module.read_bytes() != MODULE_MAP:
        raise RuntimeError("Unexpected ICU framework module map")
    if (resources / "PrivacyInfo.xcprivacy").read_bytes() != PRIVACY_BYTES:
        raise RuntimeError("Missing or modified privacy manifest")
    if not (headers / "unicode/unicode").is_symlink() or os.readlink(headers / "unicode/unicode") != ".":
        raise RuntimeError("Missing upstream include-directory link")
    info = plistlib.loads((resources / "Info.plist").read_bytes())
    if info.get("CFBundlePackageType") != "FMWK" or info.get("CFBundleExecutable") != NAME:
        raise RuntimeError("Invalid static framework bundle metadata")
    return archive, headers


def safe_link(path, root):
    target = os.readlink(path)
    if os.path.isabs(target) or not (path.parent / target).resolve().is_relative_to(root.resolve()):
        raise RuntimeError(f"Framework link escapes the artifact: {path}")
    return target


def unpack_parts(parts):
    for archive in sorted(parts.glob("*/icu-part.tar")):
        destination = archive.parent.resolve()
        progress(f"Unpacking {archive.parent.name}; preserving framework links", "assembly")
        with tarfile.open(archive) as source:
            for member in source:
                target = destination / member.name
                if member.name.split("/", 1)[0] != "ICU.xcframework" or not target.resolve().is_relative_to(destination):
                    raise RuntimeError("Invalid framework part path")
                if member.issym():
                    if os.path.isabs(member.linkname) or not (target.parent / member.linkname).resolve().is_relative_to(destination):
                        raise RuntimeError("Framework part link escapes its directory")
                elif not (member.isdir() or member.isfile()):
                    raise RuntimeError("Unexpected framework part entry")
                source.extract(member, destination)
