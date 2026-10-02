#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export COPYFILE_DISABLE=1
container=$(docker create --pull always ghcr.io/home-assistant/hassfest)
trap 'docker rm -f -v "$container" > /dev/null' EXIT
git ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' path; do
  if [ -e "$path" ] || [ -L "$path" ]; then
    printf '%s\0' "$path"
  fi
done | tar --null -T - -cf - | docker cp - "$container":/github/workspace
docker start -a "$container"
