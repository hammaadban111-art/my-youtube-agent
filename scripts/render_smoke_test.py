"""Proves the render toolchain works on this machine, in about ten seconds.

    python scripts/render_smoke_test.py          # captions + a 1s encode
    python scripts/render_smoke_test.py --tts    # ...and one edge-tts sentence

Why it exists: on 2026-10-22 GitHub moved ubuntu-latest to Ubuntu 26.04
(ImageMagick 7). The caption setup quietly stopped working and both upload
slots failed eleven minutes in, after the story had already been claimed. This
script makes the same calls the real render makes - a moviepy TextClip with the
real font, method and stroke, then an actual mp4 encode - so a broken runner
fails here, before any story is touched.

daily.yml and reupload.yml run it right after installing dependencies.
canary.yml runs it (with --tts) every morning before the first slot, so a
runner or edge-tts change turns a cheap canary red instead of an upload.
"""
import argparse
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def check_captions_and_encode(workdir: str) -> None:
    from moviepy.editor import ColorClip, CompositeVideoClip, TextClip
    from agent import assemble

    # Same arguments as agent/assemble.py _segment_clip.
    caption = TextClip(
        "Smoke test caption", fontsize=70, color="white",
        font=assemble.FONT_PATH, method="label", stroke_color="black",
        stroke_width=assemble.CAPTION_STROKE_WIDTH,
    )
    if caption.w <= 0 or caption.h <= 0:
        raise RuntimeError(f"caption rendered empty ({caption.w}x{caption.h})")
    print(f"[smoke] caption rendered {caption.w}x{caption.h}")

    background = ColorClip((360, 640), color=(20, 20, 20)).set_duration(1)
    video = CompositeVideoClip(
        [background, caption.set_position(("center", "center")).set_duration(1)])
    out = os.path.join(workdir, "smoke.mp4")
    video.write_videofile(out, fps=24, codec="libx264", audio=False,
                          logger=None)
    size = os.path.getsize(out)
    if size < 1000:
        raise RuntimeError(f"encoded smoke video is only {size} bytes")
    print(f"[smoke] encoded 1s mp4 ({size} bytes)")


def check_tts(workdir: str) -> None:
    import edge_tts
    from agent import config

    out = os.path.join(workdir, "smoke.mp3")

    async def speak():
        await edge_tts.Communicate("This is a smoke test.", config.VOICE).save(out)

    asyncio.run(speak())
    size = os.path.getsize(out)
    if size < 1000:
        raise RuntimeError(f"edge-tts returned only {size} bytes")
    print(f"[smoke] edge-tts spoke with {config.VOICE} ({size} bytes)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tts", action="store_true",
                        help="also synthesize one sentence with edge-tts")
    args = parser.parse_args(argv)

    failures = []
    with tempfile.TemporaryDirectory() as workdir:
        checks = [("captions/encode", check_captions_and_encode)]
        if args.tts:
            checks.append(("edge-tts", check_tts))
        for name, check in checks:
            try:
                check(workdir)
            except Exception as e:  # noqa: BLE001 - report every broken piece
                failures.append(name)
                print(f"::error title=Render smoke test: {name} broken::"
                      f"{type(e).__name__}: {str(e)[:500]}")
    if failures:
        print(f"[smoke] FAILED: {', '.join(failures)}")
        return 1
    print("[smoke] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
