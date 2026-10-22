#!/usr/bin/env bash
# Lets ImageMagick read caption text from a file, then PROVES it works.
#
# moviepy 1.0.3's TextClip hands ImageMagick its text as "label:@/tmp/x.txt".
# Ubuntu's default policy.xml forbids every "@" path, so captions fail with
#   "attempt to perform an operation not allowed by the security policy @/tmp/..."
#
# This used to be a one-line sed on /etc/ImageMagick-6/policy.xml with
# "|| true" after it. On 2026-10-22 GitHub moved ubuntu-latest to 26.04, which
# ships ImageMagick 7 at /etc/ImageMagick-7/policy.xml. The sed hit a missing
# file, "|| true" swallowed it, and both upload slots died later in the render.
#
# So now: patch every ImageMagick policy file that exists, whatever the major
# version, then render a real caption from an @file the same way moviepy does.
# If that render fails, this step fails here, in seconds, with a clear message.
set -euo pipefail

shopt -s nullglob
policies=(/etc/ImageMagick*/policy.xml)
if [ ${#policies[@]} -eq 0 ]; then
  echo "::error::No /etc/ImageMagick*/policy.xml found - is imagemagick installed?"
  exit 1
fi

for policy in "${policies[@]}"; do
  echo "Patching $policy"
  # Any rights value on the "@*" path rule becomes read|write. Matches both
  # quote styles and any spacing between the attributes.
  sudo sed -i -E \
    "s#(<policy[^>]*domain=\"path\"[^>]*)rights=\"[^\"]*\"([^>]*pattern=\"@\\*\")#\\1rights=\"read|write\"\\2#" \
    "$policy"
  grep -n 'pattern="@\*"' "$policy" || echo "  (no @* rule in $policy)"
done

# moviepy calls "convert" (auto-detected); ImageMagick 7 also has "magick".
bin=""
for candidate in convert magick; do
  if command -v "$candidate" >/dev/null 2>&1; then bin="$candidate"; break; fi
done
if [ -z "$bin" ]; then
  echo "::error::Neither convert nor magick is on PATH"
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
printf 'caption smoke test' > "$tmp/text.txt"
# Same font and stroke options agent/assemble.py passes through moviepy.
font="$(cd "$(dirname "$0")/.." && pwd)/assets/DejaVuSans-Bold.ttf"
if ! "$bin" -background transparent -font "$font" -fill white \
      -stroke black -strokewidth 2 -pointsize 40 \
      "label:@$tmp/text.txt" "PNG32:$tmp/out.png"; then
  echo "::error::ImageMagick ($bin) still cannot render caption text from an @file. Captions would fail mid-render."
  exit 1
fi
echo "ImageMagick caption smoke test passed ($bin, $("$bin" -version | head -1))"
