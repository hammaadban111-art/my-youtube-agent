"""Speak every upcoming story in a packet the day it lands, and fail loudly if
one is the wrong length.

WHY. On 2026-11-23 the 06:07Z slot died on tts.NarrationLengthError: the
Coelacanth script ran 45.7s at the house rate and still 40.3s at the +25%
clamp, against a 35s video. The guard retired it correctly, but the slot was
lost, and the packet holding that story had been committed two days earlier.
Nothing between the commit and the slot ever spoke the script.

Word count cannot do this job (see the adaptive-rate notes in agent/tts.py:
two scripts of similar length ran 170 and 251 words per minute), so this runs
the real synthesis, adaptive-rate repair included, exactly as an upload would.

Microsoft's voice drifts a little from day to day, so a story that only just
fits today can fail on its day. Those are reported as warnings, not failures:
AT_RISK_MARGIN of the speed window either side.

A failure here is a red story-packet run naming the story, days before its
slot, while it can still be rewritten. An edge-tts outage is NOT a length
problem and only warns: the daily canary already watches for that.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

from . import config, gha, packet, tts

AT_RISK_MARGIN = 0.03


def _upcoming(pk: dict, now: datetime) -> list[dict]:
    """Unpublished stories whose slot has not passed yet."""
    out = []
    for story in packet.stories(pk):
        slot = packet._slot_dt(story)
        if slot is None or slot < now:
            continue
        if packet.status_of(story) not in packet.SELECTABLE:
            continue
        out.append(story)
    return out


def measure(story: dict, synth=None) -> dict:
    """{'story_id', 'subject', 'verdict', 'detail'} for one story.

    verdict is 'ok', 'at-risk', 'too-long-or-short' or 'unchecked'."""
    synth = synth or tts.synthesize_all
    row = {"story_id": story.get("story_id", ""),
           "subject": story.get("topic_subject", "")}
    script = packet.to_script(story)
    target = float(config.VIDEO_LENGTH_SECONDS)
    old_workdir = config.WORKDIR
    with tempfile.TemporaryDirectory() as tmp:
        config.WORKDIR = tmp
        try:
            segments = synth(script)
        except tts.NarrationLengthError as e:
            return {**row, "verdict": "too-long-or-short", "detail": str(e)}
        except Exception as e:  # noqa: BLE001 - an outage, not a length verdict
            return {**row, "verdict": "unchecked",
                    "detail": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            config.WORKDIR = old_workdir
    spoken = sum(float(s.get("source_duration", s.get("duration", 0)))
                 for s in segments)
    speed = spoken / target
    detail = (f"{spoken:.1f}s at rate {tts.active_rate()} for a {target:.0f}s "
              f"video (speed {speed:.3f})")
    low = tts.MIN_FINAL_PLAYBACK_SPEED + AT_RISK_MARGIN
    high = tts.MAX_FINAL_PLAYBACK_SPEED - AT_RISK_MARGIN
    verdict = "ok" if low <= speed <= high else "at-risk"
    return {**row, "verdict": verdict, "detail": detail}


def check_packet(pk: dict, now: datetime = None, synth=None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    return [measure(s, synth=synth) for s in _upcoming(pk, now)]


def _summary(rows: list[dict]) -> str:
    lines = ["### Narration length check", "",
             "| Story | Result | Detail |", "|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['subject']} | {r['verdict']} | {r['detail'][:160]} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    rows = check_packet(packet.load_packet())
    for r in rows:
        print(f"[length] {r['verdict']:>17}  {r['story_id']}: {r['detail'][:200]}")
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(_summary(rows))
    for r in rows:
        if r["verdict"] == "at-risk":
            gha.warning(f"Narration close to the limit: {r['subject']}",
                        f"{r['story_id']}: {r['detail']}")
        elif r["verdict"] == "unchecked":
            gha.warning(f"Could not voice {r['subject']}",
                        f"{r['story_id']}: {r['detail']}")
    bad = [r for r in rows if r["verdict"] == "too-long-or-short"]
    for r in bad:
        gha.error(f"Story will fail on its day: {r['subject']}",
                  f"{r['story_id']}: rewrite its narration. {r['detail']}")
    print(f"[length] {len(rows)} upcoming stories checked, {len(bad)} wrong length")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
