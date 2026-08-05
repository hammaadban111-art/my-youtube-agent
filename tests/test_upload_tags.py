"""Tests for upload.build_tags tag extraction and length/budget capping,
plus quota.UNITS_PER_READING configuration.
"""
from agent import quota, upload


def test_build_tags_realistic_script():
    script = {
        "title": "The Night Something Walked Across England",
        "description": "On a frozen morning in 1855, thousands awoke... #unsolvedmysteries #history #creepy",
        "topic_subject": "Devil's Footprints",
    }
    niche = "unsolved mysteries and bizarre history"
    tags = upload.build_tags(script, niche)

    # All lowercase
    assert all(t == t.lower() for t in tags)

    # Deduped while preserving first-seen order
    assert len(tags) == len(set(tags))

    # Relevant tags present
    assert "devil's footprints" in tags
    assert "unsolved" in tags
    assert "mysteries" in tags
    assert "bizarre" in tags
    assert "history" in tags
    assert "unsolvedmysteries" in tags
    assert "creepy" in tags

    # Verify first-seen order: topic_subject comes first
    assert tags[0] == "devil's footprints"


def test_build_tags_500_char_budget_respected():
    # Feed many long tags (topic_subject, long niche terms, long hashtags)
    # total budget (sum of tag lengths + separators) <= 500
    long_hashtags = " ".join([f"#{'a' * 80}{i}" for i in range(10)])
    script = {
        "topic_subject": "t" * 80,
        "description": f"Some text {long_hashtags}",
    }
    niche = " ".join([f"nicheterm{'b' * 70}{i}" for i in range(5)])

    tags = upload.build_tags(script, niche)

    total_with_separators = sum(len(t) for t in tags) + (len(tags) - 1 if tags else 0)
    assert total_with_separators <= 500
    assert sum(len(t) for t in tags) <= 500


def test_build_tags_each_tag_max_100_chars():
    script = {
        "topic_subject": "x" * 105,  # > 100 chars, should be dropped
        "description": f"#validtag #{'y' * 100} #{'z' * 101}",
    }
    tags = upload.build_tags(script, "niche")

    assert "x" * 105 not in tags
    assert "z" * 101 not in tags
    assert "y" * 100 in tags
    assert "validtag" in tags
    assert all(len(t) <= 100 for t in tags)


def test_build_tags_missing_or_none_topic_subject():
    script_missing = {"description": "Check this out #mystery"}
    script_none = {"topic_subject": None, "description": "Check this out #mystery"}

    tags1 = upload.build_tags(script_missing, "unsolved mysteries")
    tags2 = upload.build_tags(script_none, "unsolved mysteries")

    assert "mystery" in tags1
    assert "unsolved" in tags1
    assert "mystery" in tags2
    assert "unsolved" in tags2


def test_build_tags_hashtags_extracted_strip_hash():
    script = {
        "description": "Fascinating case! #unsolvedmysteries #paranormal #history"
    }
    tags = upload.build_tags(script, "")

    assert "unsolvedmysteries" in tags
    assert "paranormal" in tags
    assert "history" in tags
    assert all(not t.startswith("#") for t in tags)


def test_build_tags_no_usable_material():
    assert upload.build_tags({}, "") == []
    assert upload.build_tags({"topic_subject": None, "description": ""}, "") == []
    assert upload.build_tags({"topic_subject": "  ", "description": "no hashtags here"}, "  ") == []
    assert upload.build_tags(None, None) == []


def test_build_tags_stopwords_filtered_single_word():
    niche = "unsolved mysteries and bizarre history"
    tags = upload.build_tags({}, niche)
    assert "and" not in tags
    assert "unsolved" in tags
    assert "mysteries" in tags
    assert "bizarre" in tags
    assert "history" in tags


def test_build_tags_multiword_stopword_survives():
    script = {"topic_subject": "out of place artifacts"}
    tags = upload.build_tags(script, "")
    assert "out of place artifacts" in tags


def test_build_tags_wikipedia_disambiguator_and_parentheses():
    script = {"topic_subject": "Devil's Kettle (Minnesota)"}
    tags = upload.build_tags(script, "")
    assert "devil's kettle" in tags
    assert "minnesota" in tags
    assert all("(" not in t and ")" not in t for t in tags)


def test_build_tags_apostrophe_preserved():
    script = {"topic_subject": "Devil's Kettle (Minnesota)"}
    tags = upload.build_tags(script, "bizarre history")
    assert "devil's kettle" in tags
    assert "minnesota" in tags


def test_quota_units_per_reading():
    assert quota.UNITS_PER_READING == 3
