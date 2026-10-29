"""Apple application destinations; SDK metadata determines available architectures."""

PLATFORMS = {
    "ios": {"minimum": "15.0", "variants": [
        ("ios", "iphoneos", "iphoneos", ["arm64"]),
        ("ios-simulator", "iphonesimulator", "iphonesimulator", "simulator")]},
    "macos": {"minimum": "12.0", "variants": [
        ("macos", "macosx", "macosx", ["arm64", "x86_64"])]},
    "maccatalyst": {"minimum": "15.0", "variants": [
        ("maccatalyst", "macosx", "iosmac", ["arm64", "x86_64"])]},
    "tvos": {"minimum": "15.0", "variants": [
        ("tvos", "appletvos", "appletvos", ["arm64"]),
        ("tvos-simulator", "appletvsimulator", "appletvsimulator", "simulator")]},
    "watchos": {"minimum": "9.0", "variants": [
        ("watchos", "watchos", "watchos", ["arm64_32", "arm64"]),
        ("watchos-simulator", "watchsimulator", "watchsimulator", "simulator")]},
    "visionos": {"minimum": "1.0", "variants": [
        ("visionos", "xros", "xros", ["arm64"]),
        ("visionos-simulator", "xrsimulator", "xrsimulator", "simulator")]},
}

# XCFramework's names differ from Clang's xros/macabi spellings.
XCFRAMEWORK_PLATFORMS = {
    ("ios", None): "ios", ("ios", "simulator"): "ios-simulator",
    ("ios", "maccatalyst"): "maccatalyst", ("macos", None): "macos",
    ("tvos", None): "tvos", ("tvos", "simulator"): "tvos-simulator",
    ("watchos", None): "watchos", ("watchos", "simulator"): "watchos-simulator",
    ("xros", None): "visionos", ("xros", "simulator"): "visionos-simulator",
}


def build_matrix():
    return {"include": [{"family": family, "variant": variant}
                        for family, settings in PLATFORMS.items()
                        for variant, _, _, _ in settings["variants"]]}


def version(value):
    return tuple(map(int, (value + ".0.0").split(".")[:3]))
