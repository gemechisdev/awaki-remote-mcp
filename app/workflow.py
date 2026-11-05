def make_workflow() -> str:
    # User-supplied build command is passed through an environment variable,
    # never interpolated directly into a shell script.
    return r"""name: remote-build
on:
  workflow_dispatch:
    inputs:
      language:
        description: Build environment
        required: true
        type: choice
        options: [android, kotlin, rust, go, python, node, cpp, flutter, generic]
      build_command:
        description: Shell command to execute in the checked-out project
        required: true
        type: string
      artifact_paths:
        description: Newline-separated artifact paths/globs
        required: true
        type: string
      timeout_minutes:
        description: Maximum job duration
        required: true
        type: string
        default: "30"

permissions:
  contents: read
  actions: read

concurrency:
  group: remote-build-${{ github.run_id }}
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: ${{ fromJSON(inputs.timeout_minutes) }}
    steps:
      - name: Checkout temporary source
        uses: actions/checkout@v4
        with:
          persist-credentials: false

      - name: Set up Java for Android/Kotlin
        if: inputs.language == 'android' || inputs.language == 'kotlin' || inputs.language == 'flutter'
        uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: "17"
          cache: gradle

      - name: Set up Rust
        if: inputs.language == 'rust'
        uses: dtolnay/rust-toolchain@stable

      - name: Set up Go
        if: inputs.language == 'go'
        uses: actions/setup-go@v5
        with:
          go-version: stable
          cache: true

      - name: Set up Python
        if: inputs.language == 'python'
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Set up Node.js
        if: inputs.language == 'node'
        uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: npm

      - name: Set up Flutter
        if: inputs.language == 'flutter'
        uses: subosito/flutter-action@v2
        with:
          channel: stable
          cache: true

      - name: Install C/C++ toolchain
        if: inputs.language == 'cpp'
        run: |
          sudo apt-get update
          sudo apt-get install -y build-essential cmake ninja-build

      - name: Run build command
        id: build
        env:
          BUILD_COMMAND: ${{ inputs.build_command }}
        shell: bash
        run: |
          set +e
          bash -euo pipefail -c "$BUILD_COMMAND" 2>&1 | tee build.log
          result=${PIPESTATUS[0]}
          set -e
          exit "$result"

      - name: Upload build outputs
        if: success()
        uses: actions/upload-artifact@v4
        with:
          name: build-output
          path: ${{ inputs.artifact_paths }}
          if-no-files-found: error
          retention-days: 1

      - name: Upload build log
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: build-log
          path: build.log
          if-no-files-found: ignore
          retention-days: 1
"""
