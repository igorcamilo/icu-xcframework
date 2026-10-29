# ICU static XCFramework

Builds the pinned ICU4C submodule into a self-contained static XCFramework for
all Apple app platforms, without modifying ICU: iOS, macOS, Mac Catalyst, tvOS,
watchOS, and visionOS, including all four Simulator families.
Requires macOS, full Xcode, and Python 3; Xcode's Python is sufficient. No package
manager dependencies or network access are needed after initializing the submodule.

The artifact exposes only ICU's upstream APIs. Every packaged public header is
copied unchanged from the submodule and checked byte-for-byte. No custom headers,
implementations, adapters, aliases, or Swift bindings are added. Generated files
are limited to packaging metadata: the module map, XCFramework plist, provenance
manifest and verification report. Consumer-specific bridges belong to consumers.
The public header inventory is discovered from the checked-out submodule on each
build; there is no maintained list of API declarations or language bindings to
update for each ICU release.

```sh
git submodule update --init icu
./scripts/build-icu.py --output "/any/output directory/ICU.xcframework"
```

Omit `--output` to create `dist/ICU.xcframework`. Relative paths are relative to
the current working directory, and output paths may contain spaces. Existing
artifacts are preserved unless `--force` is given; replacement is limited to
artifacts produced by this script and happens after successful verification.

Useful options:

```sh
./scripts/build-icu.py \
  --output /tmp/ICU.xcframework \
  --build-dir /tmp/icu-build-cache \
  --minimum-ios 15.0 \
  --jobs 8
```

The default build includes the following variants and architectures when the
selected SDK supports them:

| Platform | Architectures | Default deployment minimum |
| --- | --- | --- |
| iOS | arm64 | 15.0 |
| iOS Simulator | arm64, x86_64 | 15.0 |
| macOS | arm64, x86_64 | 12.0 |
| Mac Catalyst | arm64, x86_64 | 15.0 (iOS version) |
| tvOS | arm64 | 15.0 |
| tvOS Simulator | arm64, x86_64 | 15.0 |
| watchOS | arm64_32, arm64 | 9.0; the current compiler raises arm64 to 26.0 |
| watchOS Simulator | arm64, x86_64 | 9.0 |
| visionOS | arm64 | 1.0 |
| visionOS Simulator | arm64, x86_64 | 1.0 |

Use `--platforms ios macos` to select a subset. Each family has its own minimum
option: `--minimum-ios`, `--minimum-macos`, `--minimum-maccatalyst`,
`--minimum-tvos`, `--minimum-watchos`, and `--minimum-visionos`. Required SDKs must
be installed. The script probes the compiler's effective minimum for each slice
and records it in the manifest; a platform/architecture mismatch fails the build.

`--simulator-architectures arm64` builds
only the Apple silicon Simulator slices if Intel support is unnecessary. Source
and build/cache paths must not contain whitespace because of ICU's makefiles;
the artifact destination has no such restriction. Select another Xcode using
`DEVELOPER_DIR` as usual.

The script builds native macOS generators, cross-builds each destination, combines
archive members with `libtool -static`, merges only the Simulator architectures
with `lipo`, and packages the libraries with `xcodebuild -create-xcframework`.
Native Mach-O objects are preserved, without LTO or an intermediate `ld -r` link.
Builds are cached under `build/`, keyed by source commit, compiler, SDKs,
deployment target, flags, architectures, and data-filter contents. Per-stage logs,
consumer executables and link maps remain in that cache for inspection.
Two architecture builds run concurrently by default and share the `--jobs`
budget. Use `--parallel-builds 1` to serialize them. This covers Apple application
platforms; DriverKit/kernel extension environments are not targets of this build.

## Using the artifact

Add the XCFramework to the app target as a linked library. Enable
`DEAD_CODE_STRIPPING=YES` on the app's Release configuration. No ICU-specific
`-force_load` or `-all_load` is needed. The packaged module autolinks `libc++`;
manual C/C++ consumers that do not use modules should link `-lc++` themselves.

The public module is **`ICU4C`**, because Apple's SDK already defines a different
module named `ICU`. Upstream versioned symbol names are preserved. C/C++ callers
use the usual ICU names through header macros; Swift calls the versioned names:

```swift
import Foundation
import ICU4C

let text: [UChar] = [0x41, 0x42, 0]
let length = text.withUnsafeBufferPointer { u_strlen_79($0.baseAddress) }
```

```cpp
#include <unicode/ustring.h>
#include <unicode/unistr.h>

auto length = u_strlen(u"AB");
icu::UnicodeString text = icu::UnicodeString::fromUTF8("Grüße");
```

Compile consumers with `-DU_STATIC_IMPLEMENTATION`, as required by ICU's static
build. For Swift, pass `-Xcc -DU_STATIC_IMPLEMENTATION` to the Swift compiler.
The build does not inject this macro into `uconfig.h` or any other public header.
Public common and i18n APIs are included; ICU I/O and
command-line tools are not distributed. Include the artifact's `LICENSE`, which
also contains third-party notices, in your application's acknowledgements.

Unused code is removed at the final app link. ICU's packaged locale data remains
a single blob: services such as collation retain that blob when used. To reduce
it at build time, pass an upstream JSON data filter:

```sh
./scripts/build-icu.py --data-filter /path/to/filter.json --output /tmp/ICU.xcframework
```

Custom filters must retain the resources and fallback dependencies needed by your
app. Broad runtime tests for the default data set are skipped for custom profiles;
provide app-specific runtime tests for those profiles. Ordinary linking, module
import and code-stripping checks still run.

## Upstream API scope

ICU's C and C++ public APIs are distributed as provided upstream. Swift can use
the C APIs and the C++ declarations supported by its importer. For C++ access,
enable Swift C++ interoperability and C++17; direct compiler invocations use
`-cxx-interoperability-mode=default -Xcc -std=c++17` alongside the static macro.

`icu::AlphabeticIndex` remains the upstream C++ class from
`<unicode/alphaindex.h>`. Its behavior is verified directly in C++; this project
does not make it into a new Swift API or provide a bridge for it. Applications
that need an additional bridge supply that code independently of the artifact.
Custom data filters must preserve the relevant collation and locale exemplar
data for the indexing locales your app uses.

## Verification

Every build validates architecture/platform/deployment metadata, real static data,
C/C++ and Swift+Foundation consumer links for every slice, and dead stripping
within an ICU object file. It also runs native C/C++/Swift consumers and tests
properties, normalization, collation, regex, word breaks, legacy conversion, and
number/date formatting against the packaged headers and native ICU build.
`AlphabeticIndex` is link-tested through its upstream C++ API for every architecture.
Runtime tests cover English record ordering, accents, Swedish Å/Ä/Ö, Russian
with English labels, Japanese, Korean, Chinese pinyin, underflow/overflow,
and immutable lookup. Swift C++ tests use upstream `UnicodeString` and `Locale`
declarations. Each platform variant's public headers are checked against the
upstream hashes recorded in the manifest, and implementation sources are rejected
from the artifact. The packaged macOS and Mac Catalyst slices also run on
the build machine's native architecture.

The artifact contains `BuildManifest.json` with provenance and
`Verification.json` with the checks and measured consumer sizes. The ICU source
tree must be clean before and after the build. Device binaries are link-tested;
the script does not install software on a physical device.

To also execute consumers in an already booted Simulator, provide its UDID:

```sh
./scripts/build-icu.py --simulator-udid YOUR_BOOTED_SIMULATOR_UDID
```

Repeat `--simulator-udid` to test multiple platform families in one invocation.
The script does not boot or shut down the Simulator. To recheck an existing
artifact independently:

```sh
./scripts/verify-icu.py /path/to/ICU.xcframework \
  --work-dir /tmp/icu-verification \
  --simulator-udid YOUR_BOOTED_SIMULATOR_UDID
```

The optional `--host-build` verifier argument accepts the native `host/` directory
from the build cache to repeat macOS runtime checks. The Simulator must be running
on an architecture included in the artifact.

## Automated releases

[Check ICU releases](.github/workflows/check-icu-releases.yml) runs only when
started manually through **Actions → Check ICU releases → Run workflow**.
It checks stable GitHub releases in
`unicode-org/icu`, ignores drafts/prereleases and avoids downgrades. When an update
is available it commits only the `icu` submodule revision to the default branch,
creates a matching repository tag (for example, `release-79.1`), and creates a
draft release. Bot commits use subjects such as `⬆️ Update ICU to 79.1`, validated
to be under 50 Unicode characters with no trailing period. It also bootstraps a
release for the currently pinned ICU version when this repository has not
published it yet. Every manual run continues to the build and publishing jobs,
even when the latest upstream release is already complete. When the submodule
revision is unchanged, no commit is created and the existing tag is preserved.

[Publish XCFramework](.github/workflows/release.yml) is called directly by the
manual release-check workflow and also runs for manually published repository releases.
It uses the build scripts from the workflow run's commit and the ICU revision
pinned by the release tag. It builds and verifies one XCFramework containing all
supported Apple app platforms, and uploads these GitHub release assets:

- `ICU.xcframework.zip`, with exactly one `ICU.xcframework` at the archive root.
- `ICU.xcframework.zip.sha256`, with the ZIP's SHA-256 checksum.

Only after both assets are uploaded does it refresh the release title to
`ICU <version>` and update the release description with
the upstream release-notes link and a `.binaryTarget(name:url:checksum:)` snippet
using that release's exact download URL and checksum. Draft releases are then
published. Existing maintainer-written description text is preserved.

Start **Publish XCFramework** manually with an existing release tag to rebuild
that version. Both manual entry points always build and replace the ZIP and
checksum assets, then regenerate the title and managed description. The download
URL stays the same, but the checksum can change; Swift package consumers must
use the newly generated snippet after replacement. Choose the branch containing
your build-script improvements when starting a manual run.

Release-event runs reuse an existing ZIP and checksum when retrying a partial
publish. Reusable workflow callers can request replacement with `rebuild: true`.

The workflows use `GITHUB_TOKEN` with `contents: write`; no PAT is required. The
release-check workflow calls the reusable publishing workflow because releases
created with `GITHUB_TOKEN` do not trigger another release-event workflow. The
publisher runs as a dependent job in the same workflow run, so its build/publish
status is reflected in that run's result.
Repository rules must allow the bot to push submodule commits and create tags.
Enable Actions and put the workflows on the repository's default branch.

The publisher uses GitHub's `xcode-27` macOS runner, which includes the required
Apple SDKs. Set the `MACOS_RUNNER` repository variable to use a different suitable
runner. Builds are verified before publishing, and diagnostic logs are kept as
workflow artifacts for seven days.

Release automation has dependency-free tests that also run in
[CI](.github/workflows/ci.yml):

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```
