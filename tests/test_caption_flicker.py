import random
import string
from agent.assemble import (
    _caption_chunks, _measure_text_width, CAPTION_MIN_SECONDS, CAPTION_MAX_WIDTH,
    CAPTION_MIN_FONTSIZE, CAPTION_MAX_MERGED_WORDS
)


def test_caption_flicker_fuzzing():
    """Fuzz like test_caption_timing.py: every chunk lasts >= CAPTION_MIN_SECONDS,
    unless it is the only chunk covering a sentence shorter than that floor or
    unmergeable due to width/word limits."""
    random.seed(123)
    for _ in range(100):
        num_sentences = random.randint(1, 4)
        sentences = []
        cumulative_start = 0.0
        for _ in range(num_sentences):
            num_words = random.randint(1, 10)
            words = [
                "".join(random.choices(string.ascii_lowercase, k=random.randint(2, 6)))
                for _ in range(num_words)
            ]
            text = " ".join(words)
            duration = random.uniform(0.4, 3.0)
            sentences.append({
                "text": text,
                "start": cumulative_start,
                "duration": duration,
            })
            cumulative_start += duration + 0.1

        chunks = _caption_chunks(sentences)
        for chunk in chunks:
            assert len(chunk["text"].split()) <= 4, f"Chunk exceeds 4 words: {chunk['text']!r}"
            assert chunk["duration"] > 0, "Chunk duration must be positive"
            assert chunk["fontsize"] >= CAPTION_MIN_FONTSIZE, (
                f"Fontsize {chunk['fontsize']} below CAPTION_MIN_FONTSIZE {CAPTION_MIN_FONTSIZE}"
            )

            # Check duration floor invariant with explicit legitimate exceptions:
            if chunk["duration"] < CAPTION_MIN_SECONDS - 0.05:
                matching_sent = next(
                    s for s in sentences
                    if s["start"] - 1e-5 <= chunk["start"] <= s["start"] + s["duration"] + 1e-5
                )
                sent_short = matching_sent["duration"] < CAPTION_MIN_SECONDS

                sent_chunks = [
                    ck for ck in chunks
                    if matching_sent["start"] - 1e-5 <= ck["start"] <= matching_sent["start"] + matching_sent["duration"] + 1e-5
                ]
                ck_idx = sent_chunks.index(chunk)
                can_merge = False
                for nbr_idx in [ck_idx - 1, ck_idx + 1]:
                    if 0 <= nbr_idx < len(sent_chunks):
                        nbr = sent_chunks[nbr_idx]
                        merged_text = f"{chunk['text']} {nbr['text']}" if ck_idx < nbr_idx else f"{nbr['text']} {chunk['text']}"
                        merged_words = len(merged_text.split())
                        merged_width = _measure_text_width(merged_text, CAPTION_MIN_FONTSIZE)
                        if merged_words <= CAPTION_MAX_MERGED_WORDS and merged_width <= CAPTION_MAX_WIDTH:
                            can_merge = True

                assert sent_short or not can_merge, (
                    f"Chunk {chunk['text']!r} has duration {chunk['duration']:.3f}s < {CAPTION_MIN_SECONDS}s "
                    f"without a valid exception (sentence duration: {matching_sent['duration']:.3f}s, can_merge: {can_merge})"
                )


def test_real_render_long_words_shrink_to_merge():
    """Realistic long words from the real render ('observatory', 'temperatures', 'freezing')
    in a fast sentence. Previously split into sub-0.6s single-word flashes because 3 long words
    did not fit at fontsize 72 and merge at size 72 was refused. Verified that shrink-to-merge
    merges them into a readable caption >= CAPTION_MIN_SECONDS by reducing fontsize."""
    sentences = [{"text": "observatory temperatures freezing", "start": 0.0, "duration": 0.9}]
    chunks = _caption_chunks(sentences)

    assert len(chunks) == 1
    assert chunks[0]["text"] == "observatory temperatures freezing"
    assert chunks[0]["duration"] >= CAPTION_MIN_SECONDS
    assert chunks[0]["fontsize"] < 72, "Fontsize should step down to fit the merged text"
    assert chunks[0]["fontsize"] >= CAPTION_MIN_FONTSIZE, "Fontsize must not fall below legibility floor"


def test_merged_caption_fontsize_floor():
    """Asserts that when shrink-to-merge reduces fontsize to accommodate merged text,
    the resulting fontsize is strictly >= CAPTION_MIN_FONTSIZE (44)."""
    # Fast sentence with realistic long words requiring fontsize reduction to merge
    sentences = [
        {"text": "Radar hour in valley observatory temperatures", "start": 0.0, "duration": 1.8}
    ]
    chunks = _caption_chunks(sentences)

    for chunk in chunks:
        assert chunk["duration"] >= CAPTION_MIN_SECONDS, (
            f"Chunk {chunk['text']!r} landed below duration floor: {chunk['duration']:.3f}s"
        )
        assert chunk["fontsize"] >= CAPTION_MIN_FONTSIZE, (
            f"Chunk {chunk['text']!r} font size {chunk['fontsize']} below CAPTION_MIN_FONTSIZE floor {CAPTION_MIN_FONTSIZE}"
        )


def test_fast_sentence_short_words_flicker_fix():
    """A targeted non-random case: a fast sentence of short words that produced a
    sub-0.6s caption before merging, but is merged to respect CAPTION_MIN_SECONDS."""
    # 4 words in 0.8 seconds -> split into 3 words + 1 word (0.6s and 0.2s slots)
    # The 0.2s piece triggers a merge with the adjacent piece to create a single 4-word caption.
    sentences = [{"text": "he ran away fast", "start": 0.0, "duration": 0.8}]
    chunks = _caption_chunks(sentences)

    assert len(chunks) == 1
    assert chunks[0]["text"] == "he ran away fast"
    assert chunks[0]["duration"] >= CAPTION_MIN_SECONDS


def test_four_word_cap_holds_after_merging():
    """Proves the 4-word cap from B1 holds after merging: two 3-word pieces (total 6 words)
    must NOT merge into a 6-word chunk even if duration is below CAPTION_MIN_SECONDS."""
    # 6 words in 0.8 seconds -> two 3-word pieces, each allotted ~0.4s (< 0.6s)
    sentences = [{"text": "one two three four five six", "start": 0.0, "duration": 0.8}]
    chunks = _caption_chunks(sentences)

    # Must NOT merge into 1 chunk of 6 words
    assert len(chunks) == 2
    for chunk in chunks:
        words = chunk["text"].split()
        assert len(words) <= 4, f"Merged chunk {chunk['text']!r} exceeded 4-word cap"


def test_merge_refused_when_exceeding_max_width():
    """Proves a merge is refused when the merged text would exceed CAPTION_MAX_WIDTH."""
    # Two wide words that individually fit CAPTION_MAX_WIDTH, but together exceed it.
    w1 = "LONGWORD" * 3
    w2 = "WIDEWORD" * 3
    sentences = [{"text": f"{w1} {w2}", "start": 0.0, "duration": 0.5}]

    chunks = _caption_chunks(sentences)
    assert len(chunks) == 2
    for chunk in chunks:
        width = _measure_text_width(chunk["text"], chunk["fontsize"])
        assert width <= CAPTION_MAX_WIDTH
