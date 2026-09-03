"""
SYMBOLOS-BACKEND-1B (Sentry): "Syllabus processing failed for
COMP_250_outline (1).pdf: 'list' object has no attribute 'get'".

_persist_syllabus_result did extracted.get("instructor", {}).get("name") —
the extraction prompt asks Claude for a single "instructor" object, but a
syllabus naming multiple co-instructors sometimes gets Claude to return a
list of them instead, and .get() on a list crashes. Never trust an LLM's
output to match the requested schema exactly.
"""
from __future__ import annotations

from types import SimpleNamespace

from api.routes.syllabus import _normalize_instructor, _persist_syllabus_result


class _FakeQuery:
    """Enough of the postgrest query builder for _persist_syllabus_result
    to run to completion with no matches anywhere — this test is about the
    instructor-shape bug, not the RMP-matching or calendar-insert logic."""
    def __init__(self):
        pass
    def select(self, *_a, **_k): return self
    def ilike(self, *_a, **_k): return self
    def like(self, *_a, **_k): return self
    def eq(self, *_a, **_k): return self
    def limit(self, *_a, **_k): return self
    def delete(self): return self
    def update(self, *_a, **_k): return self
    def insert(self, *_a, **_k): return self
    @property
    def not_(self): return self
    def is_(self, *_a, **_k): return self
    def execute(self): return SimpleNamespace(data=[])


class _FakeSupabase:
    def from_(self, _name): return _FakeQuery()
    def table(self, _name): return _FakeQuery()


class TestNormalizeInstructor:
    def test_dict_passes_through(self):
        assert _normalize_instructor({"name": "David Becerra"}) == {"name": "David Becerra"}

    def test_list_of_dicts_uses_first(self):
        assert _normalize_instructor([{"name": "A"}, {"name": "B"}]) == {"name": "A"}

    def test_empty_list_becomes_empty_dict(self):
        assert _normalize_instructor([]) == {}

    def test_none_becomes_empty_dict(self):
        assert _normalize_instructor(None) == {}

    def test_missing_key_becomes_empty_dict(self):
        assert _normalize_instructor("not a dict or list") == {}


class TestPersistSyllabusResultSurvivesListShapedInstructor:
    def test_list_shaped_instructor_does_not_crash(self):
        """The exact real-world shape from the Sentry event: Claude
        returned "instructor" as a list because the syllabus named
        multiple co-instructors."""
        extracted = {
            "course_code": "COMP250",
            "course_title": "Introduction to Computer Science",
            "term": "Fall", "year": 2026,
            "instructor": [
                {"name": "Jane Instructor", "email": "jane@mcgill.ca"},
                {"name": "Second Prof", "email": "second@mcgill.ca"},
            ],
            "schedule": [], "assessments": [],
        }
        result = _persist_syllabus_result("user-1", "COMP_250_outline (1).pdf", extracted, _FakeSupabase())

        assert result["success"] is True
        assert result["instructor_name"] == "Jane Instructor"
        assert result["instructor_email"] == "jane@mcgill.ca"

    def test_missing_instructor_does_not_crash(self):
        extracted = {"course_code": "COMP250", "schedule": [], "assessments": []}
        result = _persist_syllabus_result("user-1", "no_instructor.pdf", extracted, _FakeSupabase())
        assert result["success"] is True
        assert result["instructor_name"] is None
        assert result["instructor_email"] is None

    def test_normal_dict_instructor_still_works(self):
        extracted = {
            "course_code": "COMP 251", "instructor": {"name": "David Becerra", "email": "d@mcgill.ca"},
            "schedule": [], "assessments": [],
        }
        result = _persist_syllabus_result("user-1", "normal.pdf", extracted, _FakeSupabase())
        assert result["instructor_name"] == "David Becerra"
        assert result["instructor_email"] == "d@mcgill.ca"
