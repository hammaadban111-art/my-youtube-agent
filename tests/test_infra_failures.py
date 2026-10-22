"""agent.main.is_infrastructure_failure: only exact outage signatures from real
runs are treated as infrastructure, so story faults still count."""
from agent import main


def _chained(outer, inner):
    try:
        try:
            raise inner
        except Exception:
            raise outer
    except Exception as e:  # noqa: BLE001
        return e


def test_edge_tts_403_is_infrastructure():
    # Message as raised by agent/tts.py on 2026-10-12 (run 38210992447).
    e = RuntimeError(
        "edge-tts failed on every voice tried. Last error: 403, message="
        "'Invalid response status', url='wss://speech.platform.bing.com/"
        "consumer/speech/synthesize/readaloud/edge/v1?TrustedClientToken=x'")
    assert main.is_infrastructure_failure(e)


def test_imagemagick_policy_block_is_infrastructure():
    # Message as raised by moviepy on 2026-10-22 (run 39768171915).
    e = OSError(
        "MoviePy Error: creation of None failed because of the following "
        "error:\n\nconvert: attempt to perform an operation not allowed by "
        "the security policy `@/tmp/tmpma_194t0.txt' @ "
        "error/string.c/FileToString/989.")
    assert main.is_infrastructure_failure(e)


def test_the_cause_chain_is_checked_too():
    e = _chained(RuntimeError("[3/7] voice failed"),
                 ConnectionError("WSServerHandshakeError: 403"))
    assert main.is_infrastructure_failure(e)


def test_story_faults_are_not_infrastructure():
    for e in (
        RuntimeError("Narration is 29.2s but VIDEO_LENGTH_SECONDS is 35.0s."),
        ValueError("packet story has no segments"),
        RuntimeError("edge-tts failed on every voice tried. Last error: "
                     "NoAudioReceived: No audio was received."),
        KeyError("clip_path"),
    ):
        assert not main.is_infrastructure_failure(e), e
