#!/usr/bin/env sh
# Regenerate static/tailwind.css from the templates.
#
# The stylesheet is committed so that deploying REACH stays a pure-Python
# operation — pip install, gunicorn, done. Node is needed only to regenerate it,
# and only when a template gains a utility class that is not already in the
# build. Tailwind v3 is pinned deliberately: it is what cdn.tailwindcss.com
# served while these screens were designed, so a rebuild changes nothing.
set -eu
cd "$(dirname "$0")/.."
npx --yes tailwindcss@3 \
    --config tools/tailwind.config.js \
    --input tools/tailwind-input.css \
    --output static/tailwind.css \
    --minify
echo "Wrote static/tailwind.css"
