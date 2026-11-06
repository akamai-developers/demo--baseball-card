"""Download the three model repos into the Hugging Face cache, at their pins.

    python3 pull_models.py /deploy/models.env

Runs inside vLLM's image, with the hf-cache volume at /hf-cache and HF_HOME
pointing at it. deploy/scripts/pull-models.sh starts it with docker compose run.

snapshot_download rather than the command-line tool, because that tool was
renamed from huggingface-cli to hf and its name depends on the library's
version. The Python entry point has not moved. A part-finished download
resumes, so a rerun is safe, and one that finds every file in place is quick.
"""

from __future__ import annotations

import sys
from pathlib import Path

# FLUX's repo also holds its transformer a second time, as one 7.75 GB file at
# the root, flux-2-klein-4b.safetensors, and three sample pictures. diffusers
# reads the folders model_index.json names, so only those come down: 16.0 GB of
# the repo's 23.7.
FLUX_FILES = [
    "model_index.json",
    "scheduler/*",
    "text_encoder/*",
    "tokenizer/*",
    "transformer/*",
    "vae/*",
]

# The prefix of each pin in models.env, what to call it, and which files.
# None is the whole repo at that commit.
REPOS = (
    ("CUTOUT", "BiRefNet", None),
    ("ART", "FLUX.2 klein", FLUX_FILES),
    ("LLM", "Gemma 4", None),
)


def pins(path: Path) -> dict[str, str]:
    """models.env as a dict: KEY=value lines, comments and blanks skipped."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: pull_models.py <models.env>", file=sys.stderr)
        return 2
    from huggingface_hub import snapshot_download

    values = pins(Path(argv[1]))
    for prefix, label, files in REPOS:
        repo = values.get(f"{prefix}_MODEL")
        revision = values.get(f"{prefix}_REVISION")
        if not repo or not revision:
            print(f"{argv[1]} has no {prefix}_MODEL and {prefix}_REVISION", file=sys.stderr)
            return 1
        print(f"==> {label}: {repo} at {revision}", flush=True)
        path = snapshot_download(repo_id=repo, revision=revision, allow_patterns=files)
        print(f"    {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
