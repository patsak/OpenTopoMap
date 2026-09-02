"""The pinned Java tooling: which mkgmap and splitter a build is allowed to use.

One manifest for every way the tools get installed. The Dockerfile runs
`python -m garminsvc.fetchdeps` rather than repeating the URLs in a `RUN`, so a
version bump is a single edit here and the image cannot disagree with a local
checkout about the revision the styles were tested against.

Each distribution is installed into a directory of its own, jar plus the `lib/`
beside it: the jar declares its dependencies through a manifest `Class-Path:
lib/...`, so a jar on its own runs until it first reaches for fastutil and then
dies in the middle of a build. Only the jar, that `lib/` and the licence are
unpacked — the doc/ and examples/ trees stay in the archive, which is why the
install directory holds nothing that has to be excluded again later.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from garminsvc import fetch
from garminsvc.fetch import LogFn, ProgressFn

DOWNLOAD_BASE = "https://www.mkgmap.org.uk/download"

# Written into the install directory so a pin bump reinstalls instead of leaving
# the previous revision in place: the jar's own name carries no version.
STAMP_NAME = ".otm-version"


@dataclass(frozen=True)
class Artifact:
    """A pinned distribution zip and the part of it worth keeping."""

    name: str
    version: str
    sha256: str
    jar: str
    # Globs against the path inside the archive, top-level directory stripped.
    # Patterns that match nothing are fine: splitter ships no licence file.
    members: tuple[str, ...] = ("*.jar", "lib/*.jar", "LICENCE", "README")
    base_url: str = DOWNLOAD_BASE

    @property
    def archive_name(self) -> str:
        return f"{self.version}.zip"

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.archive_name}"


MKGMAP = Artifact(
    name="mkgmap",
    version="mkgmap-r4924",
    sha256="b2170799b61a95d4fc258e8e4fb4e21396809e0390789178f94c77109f8e0d84",
    jar="mkgmap.jar",
)
SPLITTER = Artifact(
    name="splitter",
    version="splitter-r654",
    sha256="87b0ca0e827bef556341f66ec8dd7f48eea2a479d1ff3659caa5d6f0e9a9b68c",
    jar="splitter.jar",
)

ARTIFACTS: dict[str, Artifact] = {a.name: a for a in (MKGMAP, SPLITTER)}


def install_dir(artifact: Artifact, tools_dir: Path) -> Path:
    return tools_dir / artifact.name


def jar_path(artifact: Artifact, tools_dir: Path) -> Path:
    """Exact location of the jar — never a search.

    Hunting for `*.jar` under the tools directory is what let a stale revision,
    or a copy unpacked without its `lib/`, be picked up as the tool to run.
    """
    return install_dir(artifact, tools_dir) / artifact.jar


def installed_version(artifact: Artifact, tools_dir: Path) -> str | None:
    """Version recorded in the install directory, or None if nothing is there."""
    stamp = install_dir(artifact, tools_dir) / STAMP_NAME
    try:
        return stamp.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def is_installed(artifact: Artifact, tools_dir: Path) -> bool:
    if installed_version(artifact, tools_dir) != artifact.version:
        return False
    target = install_dir(artifact, tools_dir)
    return jar_path(artifact, tools_dir).is_file() and any((target / "lib").glob("*.jar"))


def install(
    artifact: Artifact,
    tools_dir: Path,
    log: LogFn | None = None,
    progress: ProgressFn | None = None,
) -> Path:
    """Download, verify and unpack ``artifact``; return the path of its jar.

    A matching install is left alone. Anything else — a different revision, a
    half-finished unpack — is replaced wholesale rather than merged into.
    """
    log = log or print
    target = install_dir(artifact, tools_dir)
    if is_installed(artifact, tools_dir):
        log(f"{artifact.name}: {artifact.version} already installed at {target}")
        return jar_path(artifact, tools_dir)

    tools_dir.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f"{artifact.name}.part")
    with tempfile.TemporaryDirectory(prefix="otm-tools-", dir=tools_dir) as tmp:
        archive = Path(tmp) / artifact.archive_name
        fetch.download(
            artifact.url,
            archive,
            log,
            progress=progress,
            label=f"Downloading {artifact.archive_name}",
            sha256=artifact.sha256,
        )
        if staging.exists():
            shutil.rmtree(staging)
        fetch.extract(archive, staging, members=artifact.members, strip_top=True)

    if not (staging / artifact.jar).is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"{artifact.name}: {artifact.jar} missing in {artifact.archive_name}")
    if not any((staging / "lib").glob("*.jar")):
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"{artifact.name}: no lib/*.jar in {artifact.archive_name}")

    (staging / STAMP_NAME).write_text(f"{artifact.version}\n", encoding="utf-8")
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    log(f"{artifact.name}: installed {artifact.version} at {target}")
    return jar_path(artifact, tools_dir)


def install_all(
    tools_dir: Path,
    log: LogFn | None = None,
    progress: ProgressFn | None = None,
    names: list[str] | None = None,
) -> dict[str, Path]:
    chosen = list(ARTIFACTS) if names is None else names
    jars: dict[str, Path] = {}
    for name in chosen:
        artifact = ARTIFACTS.get(name)
        if artifact is None:
            raise RuntimeError(f"unknown artifact {name!r}; known: {', '.join(sorted(ARTIFACTS))}")
        jars[name] = install(artifact, tools_dir, log=log, progress=progress)
    return jars


def missing(tools_dir: Path) -> list[str]:
    """Artifacts that are absent or pinned to a different version, for reporting."""
    out: list[str] = []
    for artifact in ARTIFACTS.values():
        if is_installed(artifact, tools_dir):
            continue
        target = install_dir(artifact, tools_dir)
        found = installed_version(artifact, tools_dir)
        if found and found != artifact.version:
            out.append(f"{artifact.name}: want {artifact.version}, found {found} in {target}")
        else:
            out.append(f"{artifact.version} not installed in {target}")
    return out
