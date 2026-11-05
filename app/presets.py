"""Per-language defaults so the agent only has to say *what* to build."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    language: str
    command: str | None
    artifacts: str | None
    description: str


_RUST_CMD = (
    "cargo build --release && mkdir -p dist && "
    "find target/release -maxdepth 1 -type f -executable "
    r"! -name '*.so' ! -name '*.d' ! -name '*.rlib' -exec cp {} dist/ \;"
)

PRESETS: dict[str, Preset] = {
    "android": Preset(
        "android",
        "chmod +x ./gradlew && ./gradlew --no-daemon --console=plain assembleDebug",
        "**/build/outputs/apk/**/*.apk",
        "Android (Gradle) with JDK 17 + preinstalled Android SDK. Debug APK by default. "
        "For a bundle use `bundleRelease` and artifact_paths `**/build/outputs/bundle/**/*.aab`.",
    ),
    "kotlin": Preset(
        "kotlin",
        "chmod +x ./gradlew && ./gradlew --no-daemon --console=plain build",
        "**/build/libs/*.jar",
        "Kotlin/JVM (Gradle) with JDK 17.",
    ),
    "java": Preset(
        "java",
        "if [ -f ./gradlew ]; then chmod +x ./gradlew && ./gradlew --no-daemon --console=plain build; "
        "else mvn -B -ntp package; fi",
        "**/build/libs/*.jar\n**/target/*.jar",
        "Java (Gradle wrapper or Maven) with JDK 17.",
    ),
    "rust": Preset(
        "rust",
        _RUST_CMD,
        "dist/*",
        "Rust stable with cargo cache. Release executables are copied to dist/.",
    ),
    "go": Preset(
        "go",
        "mkdir -p dist && go build -trimpath -o dist/ ./...",
        "dist/*",
        "Go (latest stable by default) with module/build cache.",
    ),
    "python": Preset(
        "python",
        "python -m pip install --upgrade pip build && python -m build",
        "dist/*",
        "Python 3.12 with pip cache. Builds sdist + wheel by default.",
    ),
    "node": Preset(
        "node",
        "if [ -f package-lock.json ]; then npm ci; else npm install; fi && npm run build",
        "dist/**/*\nbuild/**/*",
        "Node.js 22 with npm cache.",
    ),
    "cpp": Preset(
        "cpp",
        "cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release && cmake --build build --parallel",
        None,
        "C/C++ with CMake + Ninja. artifact_paths is required (e.g. `build/myapp`).",
    ),
    "flutter": Preset(
        "flutter",
        "flutter pub get && flutter build apk --release",
        "build/app/outputs/flutter-apk/*.apk",
        "Flutter stable + JDK 17.",
    ),
    "generic": Preset(
        "generic",
        None,
        None,
        "Plain Ubuntu runner (git, curl, Python, Node, Java, Go, Rust, Docker preinstalled). "
        "build_command and artifact_paths are required.",
    ),
}

TOOLCHAIN_KEYS = {"java_version", "node_version", "python_version", "go_version"}
TOOLCHAIN_DEFAULTS = {
    "java_version": "17",
    "node_version": "22",
    "python_version": "3.12",
    "go_version": "stable",
}
