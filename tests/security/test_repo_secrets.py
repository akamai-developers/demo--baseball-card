"""Nothing secret is in the repo, and nothing secret can be added to it by accident.

Every file git would commit, tracked or not yet added, is read for anything
shaped like a key, a token or a private key, and so is every commit in the
history: a secret committed once and deleted later is still in every clone.
The files that hold real secrets on a working box are held to .gitignore.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT, committable, dotenv_value, refute_key, text_of

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO_ROOT / ".git").exists(),
    reason="not a git checkout",
)

# What a secret looks like here. CARD_API_KEY is openssl rand -hex 32, and a
# Linode token is 64 hex characters too.
SHAPES = {
    "a private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "64 hex characters": re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{64}(?![0-9a-fA-F])"),
    "a Hugging Face token": re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    "a GitHub token": re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|\bgithub_pat_\w{30,}"),
    "an AWS key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "a key in an assignment": re.compile(
        r"\b(?:CARD_API_KEY|VLLM_API_KEY|LINODE_TOKEN|HF_TOKEN)\s*[=:]\s*[\"']?[A-Za-z0-9_\-]{16,}"
    ),
    "a bearer token": re.compile(r"Bearer [A-Za-z0-9._\-]{24,}"),
}

# A sha256 in a lock file is 64 hex characters and no secret. Terraform's lock
# writes the provider's zip hashes as zh: and 64 hex characters.
HASH = re.compile(r"(?:sha256[:=]\s*|\bzh:)[0-9a-f]{64}")


def found_in(text: str) -> list[str]:
    text = HASH.sub("sha256:<hash>", text)
    return [shape for shape, pattern in SHAPES.items() if pattern.search(text)]


def test_the_patterns_catch_what_they_are_for() -> None:
    """Made-up values, built here so this file holds none of them."""
    hexkey = "0123456789abcdef" * 4
    for sample in (
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
        f"token {hexkey}",
        "hf_" + "A1b2C3d4E5" * 4,
        "ghp_" + "A1b2C3d4E5" * 4,
        "CARD_API_KEY=" + "abcdef0123456789" * 2,
        "Authorization: Bearer " + "x1y2z3" * 5,
    ):
        assert found_in(sample), sample
    for sample in (
        f"sha256:{hexkey}",
        f'    "zh:{hexkey}",',
        "CARD_API_KEY=",
        "Authorization: Bearer $CARD_API_KEY",
        'VLLM_API_KEY: "${CARD_API_KEY:?CARD_API_KEY must be set}"',
        "revision e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4",
    ):
        assert not found_in(sample), sample


def test_no_committable_file_holds_anything_shaped_like_a_secret() -> None:
    hits = []
    for path in committable():
        text = text_of(path)
        if text is None:
            continue
        hits += [f"{path.relative_to(REPO_ROOT)}: {shape}" for shape in found_in(text)]
    assert hits == []


def test_no_committable_file_holds_this_boxs_key() -> None:
    key = dotenv_value("CARD_API_KEY")
    if not key:
        pytest.skip("no CARD_API_KEY in .env, so there is nothing to look for")
    for path in committable():
        text = text_of(path)
        if text is not None:
            refute_key(text, key, str(path.relative_to(REPO_ROOT)))


# ---- the history ---------------------------------------------------------------


def git(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, input=stdin, capture_output=True, text=True, errors="replace"
    ).stdout


def commits() -> list[str]:
    """Every commit on every branch and tag, oldest first."""
    return git("rev-list", "--all", "--reverse").split()


def shown(commit: str) -> str:
    """A commit's message and its patch. A binary file's patch is one line saying so."""
    return git("show", "-m", "--no-color", "--no-ext-diff", "--format=%B", commit)


def test_the_history_has_commits_to_read() -> None:
    """So the two tests below cannot pass by reading nothing."""
    assert commits(), "git rev-list --all found no commit"
    assert "diff --git a/" in shown(commits()[-1])


def test_no_commit_ever_held_anything_shaped_like_a_secret() -> None:
    hits = [f"{commit[:12]}: {shape}" for commit in commits() for shape in found_in(shown(commit))]
    assert hits == []


def test_no_commit_ever_held_this_boxs_key() -> None:
    key = dotenv_value("CARD_API_KEY")
    if not key:
        pytest.skip("no CARD_API_KEY in .env, so there is nothing to look for")
    for commit in commits():
        refute_key(shown(commit), key, f"commit {commit[:12]}")


def test_no_commit_ever_added_a_file_that_gitignore_keeps_out() -> None:
    """.env, secrets/, Terraform state and the rest: not once, in any commit."""
    added = git("log", "--all", "--format=", "--name-only", "--diff-filter=A")
    paths = sorted({line for line in added.splitlines() if line})
    assert paths, "git log found no file ever added"
    assert git("check-ignore", "--no-index", "--stdin", stdin="\n".join(paths)).split() == []


# ---- what stays out of git --------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.tmp",
        ".env.tmp.Ab12Cd",
        "secrets/ssh/id_ed25519",
        "deploy/terraform/terraform.tfstate",
        "deploy/terraform/terraform.tfstate.backup",
        "deploy/terraform/deploy.auto.tfvars",
        "deploy/terraform/.terraform/providers/x",
        "out/card-front.png",
        "build-logs/run_1.log",
        "BUILD_PROGRESS.md",
        "PROMPT.md",
        "build.sh",
        "CLAUDE.md",
        ".claude/settings.json",
    ],
)
def test_the_files_that_hold_secrets_or_state_are_ignored(path: str) -> None:
    ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT)
    assert ignored.returncode == 0, f"{path} is not in .gitignore"


def test_the_example_settings_are_not_ignored() -> None:
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=REPO_ROOT)
    assert ignored.returncode == 1
