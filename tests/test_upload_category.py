"""The packet declares a category per story; the upload has to send it.

Until 2026-09-25 every upload hardcoded categoryId "22" (People & Blogs)
while every packet story said "Education"."""
from agent import upload


def test_education_maps_to_27():
    assert upload.category_id({"metadata": {"category": "Education"}}) == "27"


def test_case_and_whitespace_do_not_matter():
    assert upload.category_id({"metadata": {"category": "  science & TECHNOLOGY "}}) == "28"


def test_unknown_or_missing_category_falls_back_to_people_and_blogs():
    assert upload.category_id({"metadata": {"category": "Mysteries"}}) == "22"
    assert upload.category_id({"metadata": {}}) == "22"
    assert upload.category_id(None) == "22"
