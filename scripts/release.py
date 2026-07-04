"""Cut a local apworld release from a single version input.

Releases are built and published LOCALLY (the CI toolchain can't fetch
devkitPro — see .github/workflows/release.yml). This wraps the whole dance so
the version is typed exactly ONCE and the committed source, the git tag, and the
shipped artifact can never disagree:

    python scripts/release.py 0.19.0           # build, commit, tag (no publish)
    python scripts/release.py 0.19.0 --publish # + gh release create

PREREQ: the ``vendor/open-dread-rando`` submodule must be initialized — the
apworld bundles its source, so the build silently produces no zip without it:

    git submodule update --init vendor/open-dread-rando

Steps (in order):

  1. Guard: clean working tree, on a branch, tag ``vX.Y.Z`` doesn't already
     exist. Bail early otherwise — a release should start from a known state.
  2. Stamp ``world_version`` + ``__version__`` to X.Y.Z (scripts/set_version.py).
  3. ``git commit`` the bump as ``chore(release): vX.Y.Z``.
  4. ``git tag vX.Y.Z`` on that commit — so the tag's tree carries the version
     it names (no post-tag drift).
  5. Fetch the Randovania logic cache + build ``dist/dread.apworld`` (the build
     re-derives the version from disk and re-asserts the two fields agree).
  6. ``--publish``: ``gh release create vX.Y.Z dist/dread.apworld
     --generate-notes``. Without it, print the exact commands to run by hand.

Nothing here is pushed — you review the commit/tag first. On any failure after
the commit/tag, the script tells you how to unwind (``git tag -d`` /
``git reset``) rather than leaving a half-cut release.

LANDING THE BUMP ON ``main`` — via a PR, NOT a direct push. ``main`` is
branch-protected (requires the ``test`` status check), so a direct
``git push origin HEAD:main`` is rejected. The release commit rides a PR:

    git push origin vX.Y.Z              # push the tag (release page reads it)
    git push -u origin <branch>         # push the release-commit branch
    gh pr create --base main --title "chore(release): vX.Y.Z" --body ...
    gh pr merge <n> --merge --auto      # MERGE COMMIT (not squash) + auto-merge

Merge as a MERGE COMMIT or REBASE, never SQUASH: squash orphans the tagged
commit so ``vX.Y.Z`` would point off ``main``'s history. The GitHub release
(``--publish`` / ``gh release create``) publishes straight from the tag and is
independent of the PR merge — the download is live immediately; the PR just
lands the source bump on ``main``.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cache_randovania_logic  # noqa: E402
import install_apworld  # noqa: E402
import set_version  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist" / "dread.apworld"


def _git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(REPO),
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed:\n{proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _preflight(tag: str) -> None:
    """Refuse to start unless the repo is in a clean, expected state."""
    if _git("status", "--porcelain"):
        raise SystemExit(
            "working tree is dirty — commit or stash first so the release "
            "commit contains only the version bump."
        )
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise SystemExit("detached HEAD — check out a branch before releasing.")
    existing = _git("tag", "--list", tag)
    if existing:
        raise SystemExit(
            f"tag {tag} already exists — bump the version or delete the tag "
            f"first (git tag -d {tag})."
        )


def _publish(tag: str) -> None:
    print(f"publishing GitHub release {tag}...")
    proc = subprocess.run(
        ["gh", "release", "create", tag, str(DIST),
         "--generate-notes"],
        cwd=str(REPO),
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"gh release create failed (exit {proc.returncode}). The commit "
            f"and tag {tag} are in place; re-run:\n"
            f"    gh release create {tag} {DIST} --generate-notes"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "version",
        help="Release version, e.g. 0.19.0 (a leading 'v' is accepted).",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Also run 'gh release create' to publish the GitHub release. "
             "Without this the artifact is built + committed + tagged and the "
             "publish command is printed for you to run.",
    )
    parser.add_argument(
        "--no-build-sysmodule",
        action="store_true",
        help="Passed through to install_apworld — reuse the staged sysmodule "
             "instead of rebuilding it (needs no devkitPro).",
    )
    args = parser.parse_args(argv)

    try:
        version = set_version.normalize_version(args.version)
    except ValueError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 2
    tag = f"v{version}"

    _preflight(tag)

    # 2. Stamp the two version fields from the single input.
    set_version.set_version(version)
    print(f"stamped world_version + __version__ = {version}")

    # 3-4. Commit the bump and tag it, so tag tree == shipped version.
    _git("commit", "-am", f"chore(release): {tag}")
    _git("tag", tag)
    print(f"committed + tagged {tag}")

    # 5. Populate the logic cache, then build the artifact. The build
    #    re-reads the on-disk version and re-asserts the fields agree.
    print("fetching Randovania logic cache...")
    cache_randovania_logic.cache_all()

    build_argv = ["--mode", "apworld", "--output", str(DIST)]
    if args.no_build_sysmodule:
        build_argv.append("--no-build-sysmodule")
    rc = install_apworld.main(build_argv)
    if rc != 0:
        raise SystemExit(
            f"build failed (exit {rc}). Unwind the release with:\n"
            f"    git tag -d {tag} && git reset --soft HEAD~1"
        )

    print(f"\nbuilt {DIST} for {tag}")
    if args.publish:
        _publish(tag)
    else:
        print(
            f"not published (no --publish). When ready:\n"
            f"    gh release create {tag} {DIST} --generate-notes"
        )
    # The bump still has to land on main. main is branch-protected, so this
    # goes through a PR (auto-merged as a MERGE COMMIT, not squash, so the tag
    # stays on main's history). Printed for both --publish and not.
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    print(
        f"\nland the bump on main (main is protected — no direct push):\n"
        f"    git push origin {tag}\n"
        f"    git push -u origin {branch}\n"
        f'    gh pr create --base main --title "chore(release): {tag}" --body ...\n'
        f"    gh pr merge --merge --auto   # merge commit, NOT squash"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
