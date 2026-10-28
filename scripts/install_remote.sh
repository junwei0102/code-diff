#!/usr/bin/env bash
# Copy the diff-review skill to ~/.claude/skills/diff-review on one or more SSH hosts.
#   usage: install_remote.sh nibi fir
# Re-run it after updating the skill (files are overwritten in place). Uses one SSH
# connection per host, so an open ControlMaster connection means no extra MFA prompt.
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ $# -eq 0 ]; then
  echo "usage: $0 <ssh-host> [<ssh-host> ...]" >&2
  exit 2
fi

remote_script='set -e
dest="$HOME/.claude/skills/diff-review"
mkdir -p "$dest"
tar -C "$dest" -xzf -
chmod +x "$dest"/scripts/*.py "$dest"/scripts/*.sh
if python3 "$dest/scripts/diff_review.py" --help >/dev/null 2>&1; then
  echo "  installed to $dest (python3 $(python3 -c "import platform; print(platform.python_version())"))"
else
  echo "  installed to $dest, but python3 here is missing or older than 3.7: run \`module load python\` first"
fi'

for host in "$@"; do
  echo "→ $host"
  # COPYFILE_DISABLE keeps macOS tar from adding ._* metadata files.
  COPYFILE_DISABLE=1 tar -C "$skill_dir" --exclude '__pycache__' --exclude '*.pyc' --exclude '.DS_Store' -czf - . |
    ssh "$host" "$remote_script"
done
