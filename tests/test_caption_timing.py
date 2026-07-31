import random
import string
from agent.assemble import _caption_chunks, _measure_text_width, CAPTION_MAX_WIDTH


def test_randomized_caption_chunks_no_overlap_and_within_bounds():
    """Guards against caption timing drift, zero/negative durations, and text overflow in caption chunking."""
    random.seed(42)
    for _ in range(300):
        num_sentences = random.randint(1, 5)
        sentences = []
        cumulative_start = 0.0
        for _ in range(num_sentences):
            num_words = random.randint(1, 25)
            words = [
                "".join(random.choices(string.ascii_letters, k=random.randint(1, 14)))
                for _ in range(num_words)
            ]
            text = " ".join(words)
            duration = random.uniform(0.5, 4.0)
            sentences.append({
                "text": text,
                "start": cumulative_start,
                "duration": duration,
            })
            cumulative_start += duration + random.uniform(0.0, 0.5)

        chunks = _caption_chunks(sentences)
        sorted_chunks = sorted(chunks, key=lambda c: c["start"])

        # (a) Assert no two chunks overlap in time
        for i in range(len(sorted_chunks) - 1):
            prev_end = sorted_chunks[i]["start"] + sorted_chunks[i]["duration"]
            nxt_start = sorted_chunks[i + 1]["start"]
            assert prev_end <= nxt_start + 1e-9, (
                f"Overlap detected: chunk ending at {prev_end} overlaps chunk starting at {nxt_start}"
            )

        # (b) Assert every chunk duration > 0
        for chunk in chunks:
            assert chunk["duration"] > 0, f"Chunk duration must be positive: {chunk}"

        # (c) Assert every chunk's measured text width is <= CAPTION_MAX_WIDTH
        for chunk in chunks:
            width = _measure_text_width(chunk["text"], chunk["fontsize"])
            assert width <= CAPTION_MAX_WIDTH, (
                f"Chunk text width {width} exceeds CAPTION_MAX_WIDTH {CAPTION_MAX_WIDTH} for text: {chunk['text']!r}"
            )


def test_pathological_long_word_fits_max_width():
    """Guards against caption clipping or overflowing CAPTION_MAX_WIDTH when handling an unsplittable long word."""
    long_word = "A" * 70
    sentences = [{"text": long_word, "start": 0.0, "duration": 3.0}]
    chunks = _caption_chunks(sentences)

    assert len(chunks) > 0, "Pathological long word should produce at least one chunk"
    sorted_chunks = sorted(chunks, key=lambda c: c["start"])

    for i in range(len(sorted_chunks) - 1):
        prev_end = sorted_chunks[i]["start"] + sorted_chunks[i]["duration"]
        nxt_start = sorted_chunks[i + 1]["start"]
        assert prev_end <= nxt_start + 1e-9

    for chunk in chunks:
        assert chunk["duration"] > 0
        width = _measure_text_width(chunk["text"], chunk["fontsize"])
        assert width <= CAPTION_MAX_WIDTH, (
            f"Chunk width {width} exceeds CAPTION_MAX_WIDTH {CAPTION_MAX_WIDTH} for text: {chunk['text']!r}"
        )
