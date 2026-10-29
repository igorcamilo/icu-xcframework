#!/usr/bin/env python3
"""Discover upstream ICU releases and publish verified XCFramework release assets."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote
import zipfile

from apple_platforms import PLATFORMS
from job_log import progress, stream_command
from frameworks import FORMAT, NAME, PRIVACY_SHA256, safe_link

UPSTREAM = "unicode-org/icu"
ARCHIVE = "ICU.xcframework.zip"
CHECKSUM = ARCHIVE + ".sha256"
BEGIN = "<!-- icu-xcframework:begin -->"
END = "<!-- icu-xcframework:end -->"
FAMILIES = set(PLATFORMS)
VARIANTS = {variant for settings in PLATFORMS.values() for variant, _, _, _ in settings["variants"]}
ROOT = Path(__file__).resolve().parent.parent


def run(*args, cwd=ROOT, stream=False):
    if stream:
        return stream_command(args, cwd=cwd, label="release")
    progress("START $ " + shlex.join([str(arg) for arg in args]))
    started = time.monotonic()
    result = subprocess.run([str(a) for a in args], cwd=cwd, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        progress(f"FAILED: exit {result.returncode} after {time.monotonic() - started:.1f}s")
        raise RuntimeError(f"Command failed: {args}\n{result.stderr}")
    if result.stderr:
        progress(result.stderr)
    progress(f"DONE in {time.monotonic() - started:.1f}s")
    return result.stdout.strip()


class GitHub:
    def api(self, endpoint, method="GET", data=None, missing_ok=False):
        progress(f"GitHub {method} {endpoint}")
        started = time.monotonic()
        args = ["gh", "api", "--method", method, endpoint]
        if data is not None:
            args += ["--input", "-"]
        result = subprocess.run(args, input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True)
        if result.returncode:
            if missing_ok and "HTTP 404" in result.stderr:
                progress("GitHub: release/ref not found; continuing")
                return None
            raise RuntimeError(f"GitHub API failed for {endpoint}: {result.stderr}")
        progress(f"GitHub {method} completed in {time.monotonic() - started:.1f}s")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def releases(self, repository):
        pages = json.loads(run("gh", "api", "--paginate", "--slurp",
                               f"repos/{repository}/releases?per_page=100"))
        return [release for page in pages for release in page]

    def release(self, repository, tag):
        release = self.api(f"repos/{repository}/releases/tags/{quote(tag, safe='')}", missing_ok=True)
        if release is not None:
            return release
        # The tag endpoint only finds published releases. The authenticated
        # listing also includes drafts when the token has push access.
        return next((release for release in self.releases(repository)
                     if release["tag_name"] == tag), None)

    def submodule_commit(self, repository, ref):
        entry = self.api(f"repos/{repository}/contents/icu?ref={quote(ref, safe='')}")
        if not isinstance(entry, dict) or not entry.get("submodule_git_url"):
            raise RuntimeError("The repository ref does not pin an ICU submodule")
        return entry["sha"]

    def tag_commit(self, repository, tag):
        reference = self.api(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}")
        target = reference["object"]
        seen = set()
        while target["type"] == "tag":
            if target["sha"] in seen:
                raise RuntimeError("Cyclic annotated tag")
            seen.add(target["sha"])
            target = self.api(f"repos/{repository}/git/tags/{target['sha']}")["object"]
        if target["type"] != "commit":
            raise RuntimeError("Release tag must point to a commit")
        return target["sha"]

    def upload(self, repository, tag, path, replace=False):
        options = ["--clobber"] if replace else []
        progress(f"{'Replacing' if replace else 'Uploading'} {path.name} ({path.stat().st_size:,} bytes) for {tag}")
        run("gh", "release", "upload", tag, str(path), "--repo", repository, *options, stream=True)

    def download(self, repository, tag, name, directory):
        run("gh", "release", "download", tag, "--repo", repository,
            "--pattern", name, "--dir", str(directory), stream=True)
        return directory / name


def release_version(tag):
    match = re.fullmatch(r"release-(\d+(?:[.-]\d+)+)", tag)
    if not match:
        return None
    parts = tuple(map(int, re.split(r"[.-]", match.group(1))))
    return parts + (0,) * max(0, 3 - len(parts))


def stable_release(releases):
    releases = [r for r in releases if not r["draft"] and not r["prerelease"]
                and release_version(r["tag_name"]) is not None]
    if not releases:
        raise RuntimeError("No stable upstream ICU release was found")
    return max(releases, key=lambda r: release_version(r["tag_name"]))


def icu_version(commit, github):
    entry = github.api(f"repos/{UPSTREAM}/contents/icu4c/source/common/unicode/uvernum.h?ref={quote(commit, safe='')}")
    header = base64.b64decode(entry["content"]).decode("utf-8")
    match = re.search(r'^\s*#\s*define\s+U_ICU_VERSION\s+"([0-9.]+)"', header, re.M)
    if match is None:
        raise RuntimeError("Cannot read the ICU version from upstream uvernum.h")
    return match.group(1)


def outputs(**values):
    for key, value in values.items():
        if "\n" in str(value) or "\r" in str(value):
            raise ValueError("Workflow outputs must be single-line values")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
    print(json.dumps(values))


def complete(release):
    names = {asset["name"] for asset in release.get("assets", [])}
    return (not release["draft"] and {ARCHIVE, CHECKSUM} <= names
            and BEGIN in (release.get("body") or "") and END in (release.get("body") or ""))


def submodule_commit_message(version):
    subject = f"⬆️ Update ICU to {version}"
    # create-commits: actual Unicode gitmoji, imperative subject, <50 characters.
    if len(subject) >= 50 or "\n" in subject or "\r" in subject or subject.endswith("."):
        raise ValueError("The ICU update commit subject must be one line under 50 Unicode characters, without a trailing period")
    return subject


def check_upstream(github, repository, branch, rebuild=False):
    run("git", "check-ref-format", f"refs/heads/{branch}")
    if run("git", "status", "--porcelain"):
        raise RuntimeError("Release discovery requires a clean checkout")
    upstream = stable_release(github.releases(UPSTREAM))
    tag = upstream["tag_name"]
    pinned_sha = run("git", "rev-parse", "HEAD:icu")
    current_version = icu_version(pinned_sha, github)
    progress(f"Current pin: ICU {current_version} ({pinned_sha}); latest stable release: {tag}")
    current = release_version("release-" + current_version)
    if release_version(tag) < current:
        progress("Keeping the newer pinned version; nothing to publish")
        outputs(tag=tag, publish="false")
        return
    existing = github.release(repository, tag)
    if not rebuild and release_version(tag) == current and existing and complete(existing):
        progress("Release is complete; no rebuild requested")
        outputs(tag=tag, publish="false")
        return

    source_sha = github.tag_commit(UPSTREAM, tag)
    progress(f"Selected upstream commit: {source_sha}; rebuild requested: {rebuild}")
    version = current_version if source_sha == pinned_sha else icu_version(source_sha, github)
    if release_version("release-" + version) != release_version(tag):
        raise RuntimeError("Upstream tag and ICU version header disagree")
    message = submodule_commit_message(version)
    # A gitlink records a commit ID; neither the source checkout nor its Git
    # objects are needed to update it in this initially clean CI index.
    run("git", "update-index", "--cacheinfo", f"160000,{source_sha},icu")
    changed = subprocess.run(["git", "diff", "--cached", "--quiet", "--", "icu"], cwd=ROOT).returncode
    if changed not in (0, 1):
        raise RuntimeError("Cannot inspect the staged submodule update")
    if changed:
        progress(f"Committing ICU gitlink update: {message}")
        run("git", "-c", "user.name=github-actions[bot]", "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", message, stream=True)
        # A concurrent branch update fails instead of force-pushing over it.
        run("git", "push", "origin", f"HEAD:refs/heads/{branch}", stream=True)
    else:
        progress("ICU gitlink is unchanged; no commit needed")

    # Create the Git tag explicitly; a draft release alone need not create it.
    reference = github.api(f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}", missing_ok=True)
    if reference:
        progress(f"Checking existing repository tag {tag}")
        if github.submodule_commit(repository, tag) != source_sha:
            raise RuntimeError("Existing release tag pins a different ICU revision; refusing to move it")
    else:
        progress(f"Creating repository tag {tag}")
        run("git", "tag", tag, "HEAD")
        run("git", "push", "origin", f"refs/tags/{tag}", stream=True)
    if not existing:
        github.api(f"repos/{repository}/releases", "POST", {
            "tag_name": tag, "target_commitish": run("git", "rev-parse", "HEAD"),
            "name": f"ICU {version}", "body": "", "draft": True, "prerelease": False,
        })
    outputs(tag=tag, publish="true" if rebuild or not (existing and complete(existing)) else "false")


def validate_artifact(manifest, verification, source_sha):
    if manifest.get("builder") != "icu-xcframework" or manifest["source_commit"] != source_sha:
        raise RuntimeError("Artifact provenance does not match the release's pinned ICU revision")
    if manifest["data_filter_sha256"] is not None:
        raise RuntimeError("Official releases must contain the default, unfiltered ICU data")
    if manifest.get("framework_format") != FORMAT or manifest.get("privacy_manifest_sha256") != PRIVACY_SHA256:
        raise RuntimeError("Official releases require static frameworks with the current privacy manifest")
    if {t["family"] for t in manifest["targets"]} != FAMILIES:
        raise RuntimeError("Official releases must include all six Apple app platforms")
    if {t["variant"] for t in manifest["targets"]} != VARIANTS:
        raise RuntimeError("Official releases must include every device, Simulator and desktop variant")
    headers = manifest.get("public_headers_sha256", {})
    if not headers or any(not name.startswith("unicode/") for name in headers):
        raise RuntimeError("Official releases must expose only verified upstream public headers")
    expected = {(t["variant"], t["arch"]) for t in manifest["targets"]}
    actual = {(t["variant"], t["arch"]) for t in verification["slices"]}
    if expected != actual or any(t["same_object_dead_stripping"] != "passed"
                                 or t["c_cpp_swift_link"] != "passed"
                                 or t["upstream_public_headers"] != len(headers)
                                 or t.get("privacy_manifest") != "passed"
                                 for t in verification["slices"]):
        raise RuntimeError("Artifact verification is incomplete")


def bundle(artifact, archive, source_sha):
    progress(f"Validating and packaging {artifact} into {archive.name}")
    if artifact.name != "ICU.xcframework":
        raise RuntimeError("The release artifact must be named ICU.xcframework")
    manifest = json.loads((artifact / "BuildManifest.json").read_text())
    verification = json.loads((artifact / "Verification.json").read_text())
    validate_artifact(manifest, verification, source_sha)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for path in sorted(artifact.rglob("*")):
            if path.is_symlink():
                link = safe_link(path, artifact)
                entry = zipfile.ZipInfo(path.relative_to(artifact.parent).as_posix())
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                target.writestr(entry, link.encode())
            elif path.is_file():
                if path.name == NAME:
                    progress(f"Compressing {path.relative_to(artifact)} ({path.stat().st_size:,} bytes)")
                target.write(path, path.relative_to(artifact.parent).as_posix())
    progress(f"ZIP ready: {archive.stat().st_size:,} bytes")


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def release_notes(repository, tag, upstream, digest, body):
    url = f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/{ARCHIVE}"
    notes = f'''{BEGIN}
## ICU XCFramework

[Upstream ICU release notes]({upstream["html_url"]})

One static XCFramework for iOS, macOS, Mac Catalyst, tvOS, watchOS and visionOS,
including their Simulator variants. Public headers and implementation come from ICU.

### Swift Package Manager

Add this entry to the `targets` array in your `Package.swift`, then add `"ICU4C"`
to the dependencies of the target that uses it:

```swift
.binaryTarget(
    name: "ICU4C",
    url: "{url}",
    checksum: "{digest}"
)
```

Import `ICU4C`. Configure consumers with `-DU_STATIC_IMPLEMENTATION`
(Swift: `-Xcc -DU_STATIC_IMPLEMENTATION`) and enable dead-code stripping in Release.
Consumers supply any bridges they need for upstream C++ APIs.

Each slice is a static framework with its own privacy manifest. Xcode 15 or later
embeds the framework resources while statically linking and stripping unused code.

[Download the XCFramework]({url}) · SHA-256: `{digest}`
{END}'''
    body = body or ""
    if BEGIN in body and END in body:
        return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: notes, body, count=1, flags=re.S)
    return body.rstrip() + ("\n\n" if body.strip() else "") + notes


def publish(github, repository, tag, artifact, directory, rebuild=False):
    release = github.release(repository, tag)
    if release is None:
        raise RuntimeError("Create the repository release before publishing its artifact")
    source_sha = github.submodule_commit(repository, tag)
    version = icu_version(source_sha, github)
    progress(f"Publishing ICU {version} ({source_sha}) to {repository}/{tag}; replace assets: {rebuild}")
    candidates = [r for r in github.releases(UPSTREAM)
                  if release_version(r["tag_name"]) == release_version("release-" + version)
                  and not r["draft"] and not r["prerelease"]]
    if len(candidates) != 1:
        raise RuntimeError("Cannot identify exactly one stable upstream release for this ICU version")
    upstream = candidates[0]
    if github.tag_commit(UPSTREAM, upstream["tag_name"]) != source_sha:
        raise RuntimeError("The submodule revision does not match the upstream release tag")
    names = {a["name"] for a in release.get("assets", [])}
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="icu-release-", dir=directory) as temporary:
        staging = Path(temporary)
        if ARCHIVE in names and not rebuild:
            progress("Reusing and validating the existing release archive")
            archive = github.download(repository, tag, ARCHIVE, staging)
            with zipfile.ZipFile(archive) as existing:
                validate_artifact(json.loads(existing.read("ICU.xcframework/BuildManifest.json")),
                                  json.loads(existing.read("ICU.xcframework/Verification.json")), source_sha)
        else:
            archive = staging / ARCHIVE
            bundle(artifact, archive, source_sha)
        digest = checksum(archive)
        progress(f"Archive SHA-256: {digest}; checking against SwiftPM")
        # Check SwiftPM's own checksum implementation against the value in the snippet.
        if run("swift", "package", "compute-checksum", archive, cwd=staging) != digest:
            raise RuntimeError("SwiftPM checksum disagrees with the archive SHA-256")
        if rebuild or ARCHIVE not in names:
            github.upload(repository, tag, archive, replace=rebuild)
        if CHECKSUM in names and not rebuild:
            previous = github.download(repository, tag, CHECKSUM, staging).read_text().split()
            if not previous or previous[0] != digest:
                raise RuntimeError("The existing checksum asset disagrees with the existing archive")
        else:
            sidecar = staging / CHECKSUM
            sidecar.write_text(f"{digest}  {ARCHIVE}\n")
            github.upload(repository, tag, sidecar, replace=rebuild)
        # Only publish drafts after both uploads; preserve manually written release text.
        current = github.release(repository, tag)
        body = release_notes(repository, tag, upstream, digest, current.get("body"))
        progress(f"Updating release title to ICU {version} and refreshing release notes")
        github.api(f"repos/{repository}/releases/{current['id']}", "PATCH",
                   {"name": f"ICU {version}", "body": body, "draft": False})
        (directory / "release-notes.md").write_text(body)
        progress(body, "release notes")
        print(f"Published {repository} {tag}: {digest}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GH_REPO", os.environ.get("GITHUB_REPOSITORY")))
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser("check-upstream")
    check.add_argument("--branch", required=True)
    check.add_argument("--rebuild", action="store_true",
                       help="publish even when the current upstream release is complete")
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("--tag", required=True)
    inspect.add_argument("--resolve-source", action="store_true",
                         help="also resolve the release's ICU gitlink using metadata only")
    release = subcommands.add_parser("publish")
    release.add_argument("--tag", required=True)
    release.add_argument("--artifact", type=Path, default=ROOT / "dist/ICU.xcframework")
    release.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    release.add_argument("--rebuild", action="store_true",
                         help="replace existing release assets with the verified local build")
    args = parser.parse_args()
    if not args.repo or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("set GH_REPO or --repo to owner/repository")
    github = GitHub()
    if args.command == "check-upstream":
        check_upstream(github, args.repo, args.branch, rebuild=args.rebuild)
    else:
        if args.tag.startswith("-"):
            parser.error("release tags must not start with a dash")
        run("git", "check-ref-format", f"refs/tags/{args.tag}")
        if args.command == "inspect":
            release = github.release(args.repo, args.tag)
            if release is None:
                raise RuntimeError("Release not found")
            values = dict(archive_exists=str(any(a["name"] == ARCHIVE for a in release.get("assets", []))).lower())
            if args.resolve_source:
                values["source_commit"] = github.submodule_commit(args.repo, args.tag)
            outputs(**values)
        else:
            publish(github, args.repo, args.tag, args.artifact.resolve(), args.output_dir.resolve(),
                    rebuild=args.rebuild)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        raise SystemExit(str(error))
