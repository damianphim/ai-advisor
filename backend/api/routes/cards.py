"""
cards.py — Proactive advisor card generation

POST   /api/cards/generate/{user_id}   — Generate AI proactive cards
GET    /api/cards/{user_id}            — Fetch stored cards (instant)
DELETE /api/cards/{user_id}            — Clear AI-generated cards
POST   /api/cards/ask/{user_id}        — User asks a question → single card
POST   /api/cards/{card_id}/thread     — Follow-up thread on a card
PATCH  /api/cards/{card_id}/save       — Toggle saved state on a card
PATCH  /api/cards/{user_id}/reorder    — Persist drag-and-drop order

CONTEXT FIX (v3):
  Thread endpoint previously called Claude with ONLY the card body — no student
  profile, no completed courses, nothing. It now imports build_system_context
  from chat.py and passes the full student context as the system prompt, with
  the card body injected via card_context and thread history in the messages
  array. Card threads now have the same context quality as main chat.
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import anthropic
import asyncio
import logging
import json
import re
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from postgrest.exceptions import APIError

from api.utils.supabase_client import get_supabase, get_user_by_id
from api.config import settings
from api.exceptions import UserNotFoundException
from api.auth import get_current_user_id, require_self, get_user_db
from api.utils.sanitise import sanitise_user_message, sanitise_context_field
from api.utils.lang import lang_instruction as _lang_instruction
from api.utils.degree_progress import compute_degree_progress_summary
from api.routes.chat import _MILESTONES
from api.routes.courses import build_course_grounding_block

router = APIRouter()
logger = logging.getLogger(__name__)



_anthropic_client: anthropic.Anthropic | None = None
_async_anthropic_client: anthropic.AsyncAnthropic | None = None


def get_anthropic_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _anthropic_client


def get_async_anthropic_client() -> anthropic.AsyncAnthropic:
    global _async_anthropic_client
    if _async_anthropic_client is None:
        _async_anthropic_client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _async_anthropic_client


CARD_CATEGORIES = ["deadlines", "degree", "courses", "grades", "planning", "opportunities", "advice"]
CATEGORIES_PROMPT_LIST = "\n".join(f'  - "{c}"' for c in CARD_CATEGORIES)


# ── Transcript reminder config ──────────────────────────────────────
# (month, day) tuples for when we drop a "re-upload your transcript" card
# into every user's brief. Set ~10 days after the final-exam period ends so
# grades have time to post.
#   - Winter finals: reminder on May 10
#   - Fall finals:   reminder on Jan 10
POST_FINALS_REMINDER_DATES = [(5, 10), (1, 10)]
TRANSCRIPT_REMINDER_LABEL = "TRANSCRIPT UPDATE"


# ── Course-registration reminder config ─────────────────────────────
# Fall and Winter registration open on the SAME date in late May — there is no
# separate Winter window — so both reminder cards tell students to register for
# both terms at once. We fire:
#   - May 20: one-week heads-up so students can plan ("look ahead")
#   - May 28: day-of "registration opens, go register now"
# Update annually if McGill's registration window shifts.
COURSE_REGISTRATION_REMINDER_DATES = [(5, 20), (5, 28)]
COURSE_REGISTRATION_REMINDER_LABEL = "COURSE REGISTRATION"

# Summer course registration opens early March. Lower-tone reminder since
# only some students take summer courses (most use the term for internships
# or break) — we just surface the date so interested students don't miss it.
SUMMER_REGISTRATION_REMINDER_DATES = [(3, 1), (3, 8)]
SUMMER_REGISTRATION_REMINDER_LABEL = "SUMMER REGISTRATION"


class ThreadRequest(BaseModel):
    user_id: str
    message: str = Field(..., min_length=1, max_length=2000)
    card_context: str = Field(..., max_length=4000)
    language: str = "en"
    thread_history: Optional[List[dict]] = Field(default=None, max_length=20)
    # DEPRECATED — no longer read; degree progress is computed server-side
    # (see compute_degree_progress_summary). Field kept only so older cached
    # frontend bundles don't 422 on this key.
    degree_progress: Optional[str] = Field(None, max_length=3000)

class GenerateRequest(BaseModel):
    force: bool = False
    language: str = "en"
    # DEPRECATED — no longer read; see ThreadRequest.degree_progress above.
    degree_progress: Optional[str] = Field(None, max_length=3000)

class AskRequest(BaseModel):
    user_id: str
    question: str = Field(..., min_length=1, max_length=2000)
    language: str = "en"

class RetranslateRequest(BaseModel):
    language: str = "en"

class SaveRequest(BaseModel):
    is_saved: bool

class CardOrder(BaseModel):
    id: str = Field(..., max_length=100)
    position: Optional[int] = Field(None, ge=0, le=100)
    sort_order: Optional[int] = Field(None, ge=0, le=100)

    @property
    def resolved_position(self) -> int:
        """Accept either 'position' or 'sort_order' from frontend."""
        if self.position is not None:
            return self.position
        if self.sort_order is not None:
            return self.sort_order
        return 0

class ReorderRequest(BaseModel):
    order: List[CardOrder] = Field(..., max_length=50)


def fetch_student_context(user_id: str, user_sb=None) -> dict:
    """
    Fetch all student data needed for card generation.
    user_sb: pass a user-scoped Supabase client to enforce RLS on all queries.
             Falls back to service role if not provided (e.g. from cron/admin).
    """
    sb = user_sb if user_sb is not None else get_supabase()
    user = get_user_by_id(user_id)

    favorites = (sb.table("favorites")
        .select("course_code, course_title, subject, catalog")
        .eq("user_id", user_id).order("created_at", desc=True).limit(30)
        .execute().data or [])

    completed = (sb.table("completed_courses")
        .select("course_code, course_title, subject, catalog, term, year, grade, credits, professor")
        .eq("user_id", user_id).order("year", desc=True).limit(50)
        .execute().data or [])

    current = (sb.table("current_courses")
        .select("course_code, course_title, subject, catalog, credits, term, year, professor")
        .eq("user_id", user_id).execute().data or [])

    today = datetime.now(timezone.utc).date().isoformat()
    calendar = (sb.table("calendar_events")
        .select("title, date, time, type, description")
        .eq("user_id", user_id).gte("date", today)
        .order("date", desc=False).limit(20)
        .execute().data or [])

    joined_clubs, created_clubs = [], []
    try:
        r = sb.table("user_clubs").select("clubs(name, category, meeting_schedule)").eq("user_id", user_id).execute()
        joined_clubs = [x.get("clubs", {}).get("name", "Unknown") for x in (r.data or []) if x.get("clubs")]
    except Exception:
        pass
    try:
        r = sb.table("clubs").select("name, category, is_private").eq("created_by", user_id).execute()
        created_clubs = r.data or []
    except Exception:
        pass

    return {"user": user, "favorites": favorites, "completed": completed,
            "current": current, "calendar": calendar,
            "joined_clubs": joined_clubs, "created_clubs": created_clubs}


def fetch_saved_cards(user_id: str, user_sb=None) -> list:
    sb = user_sb if user_sb is not None else get_supabase()
    return (sb.table("advisor_cards")
        .select("title, body, category").eq("user_id", user_id).eq("is_saved", True)
        .execute().data or [])


def fetch_recent_card_titles(user_id: str, user_sb=None, limit: int = 24) -> list[str]:
    """
    Return titles of the most recently AI-generated cards (across the last few
    generation batches). Used to nudge Claude away from re-suggesting the same
    topics on refresh.
    """
    sb = user_sb if user_sb is not None else get_supabase()
    try:
        resp = (sb.table("advisor_cards")
                .select("title")
                .eq("user_id", user_id).eq("source", "ai")
                .order("generated_at", desc=True).limit(limit)
                .execute())
        return [r.get("title", "") for r in (resp.data or []) if r.get("title")]
    except Exception:
        return []


def cards_are_fresh(user_id: str, max_age_hours: int = 168) -> bool:
    """Returns True if AI cards exist and were generated within max_age_hours (default 7 days)."""
    try:
        supabase = get_supabase()
        resp = (supabase.table("advisor_cards")
            .select("generated_at").eq("user_id", user_id).eq("source", "ai")
            .order("generated_at", desc=True).limit(1).execute())
        if not resp.data:
            return False
        generated_at = datetime.fromisoformat(
            resp.data[0]["generated_at"].replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600
        return age_hours < max_age_hours
    except Exception:
        return False


def _count_generations_this_week(user_id: str) -> int:
    """Count how many times AI cards were generated for this user in the last 7 days."""
    try:
        supabase = get_supabase()
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        # Count distinct generated_at timestamps (each generation batch shares one timestamp)
        resp = (supabase.table("advisor_cards")
            .select("generated_at")
            .eq("user_id", user_id).eq("source", "ai")
            .gte("generated_at", week_ago)
            .order("generated_at", desc=True)
            .execute())
        if not resp.data:
            return 0
        # Count unique generation timestamps (each batch shares one)
        unique_times = set(r["generated_at"] for r in resp.data)
        return len(unique_times)
    except Exception:
        return 0


def cards_exist(user_id: str) -> bool:
    """Returns True if any AI cards exist for this user, regardless of age."""
    try:
        supabase = get_supabase()
        resp = (supabase.table("advisor_cards")
            .select("id").eq("user_id", user_id).eq("source", "ai")
            .limit(1).execute())
        return bool(resp.data)
    except Exception:
        return False


def _sanitise_category(card: dict) -> str:
    cat = str(card.get("category") or "").lower().strip()
    return cat if cat in CARD_CATEGORIES else "planning"


def _parse_cards_lenient(raw: str) -> list:
    """
    Best-effort recovery when json.loads() fails on the whole array — e.g.
    Claude drops a comma between two card objects ("Expecting ',' delimiter").
    Extracts each top-level {...} object by brace-matching and parses them
    independently, so one malformed card doesn't sink every card in the
    response. Returns whatever cards parsed successfully (possibly empty).
    """
    cards: list = []
    depth = 0
    start = None
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    cards.append(json.loads(raw[start:i + 1]))
                except json.JSONDecodeError:
                    pass
                start = None
    return cards


def save_cards(user_id: str, cards: list) -> None:
    supabase = get_supabase()
    supabase.table("advisor_cards").delete().eq("user_id", user_id).eq("source", "ai").execute()
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for i, card in enumerate(cards):
        actions = card.get("actions") or []
        rows.append({
            "user_id": user_id, "source": "ai",
            "card_type": card.get("type", "insight"), "icon": card.get("icon", "💡"),
            "label": card.get("label", ""), "title": card.get("title", ""),
            "body": card.get("body", ""), "actions": json.dumps(actions),
            "category": _sanitise_category(card), "priority": card.get("priority", i + 1),
            "sort_order": i, "generated_at": now,
        })
    if not rows:
        return
    try:
        supabase.table("advisor_cards").insert(rows).execute()
    except APIError as exc:
        # 23503 = foreign_key_violation. Streamed card generation can run for
        # tens of seconds; if the account gets deleted mid-stream, user_id no
        # longer exists in users by the time we persist here. The cards were
        # already streamed to whoever was watching — there's no one left to
        # show a persistence error to, so log and drop rather than surfacing
        # a scary "Card generation failed" for what's actually just a race
        # with account deletion.
        if getattr(exc, "code", None) == "23503":
            logger.warning(f"save_cards: user {user_id} no longer exists, dropping generated cards")
        else:
            raise


def insert_user_card(user_id: str, card_data: dict, question: str, language: str) -> dict:
    supabase = get_supabase()
    actions = card_data.get("actions") or []
    now = datetime.now(timezone.utc).isoformat()
    existing = (supabase.table("advisor_cards").select("sort_order")
        .eq("user_id", user_id).order("sort_order", desc=True).limit(1).execute().data or [])
    next_sort = (existing[0]["sort_order"] + 1) if existing else 0
    row = {
        "user_id": user_id, "source": "user",
        "card_type": card_data.get("type", "insight"), "icon": card_data.get("icon", "❓"),
        "label": card_data.get("label", "YOUR QUESTION"), "title": card_data.get("title", question[:80]),
        "body": card_data.get("body", ""), "actions": json.dumps(actions),
        "category": _sanitise_category(card_data), "priority": 0,
        "sort_order": next_sort, "generated_at": now, "prompted_language": language,
        # What the student actually typed. `title` is the model's paraphrase and
        # only falls back to the question, so without this the original wording
        # was lost the moment the card was written — the column existed and
        # nothing had ever populated it. Bounded because title is varchar-ish
        # and this is free text straight from the user.
        "user_question": (question or "")[:2000],
    }
    result = supabase.table("advisor_cards").insert(row).execute()
    inserted = result.data[0] if result.data else row
    if isinstance(inserted.get("actions"), str):
        inserted["actions"] = json.loads(inserted["actions"])
    return inserted


_ACTIONS_PROMPT = (
    '"actions"  : array of 2–3 questions the STUDENT would want to click to ask '
    'the advisor to learn more about this card '
    '(e.g. "Which courses satisfy this requirement?", "When is the deadline for this?", '
    '"How do I register for this?") — '
    'write them from the student\'s perspective as if they are asking the advisor, '
    'NOT questions for the student to answer. '
    'For opportunity cards that recommend professors, also include action objects with '
    '"type" set to "email_professor" (label e.g. "Draft an email to Prof. X") or '
    '"visit_lab_page" (label e.g. "Visit their research lab page"). '
    'These action objects use the format {"type": "<action_type>", "label": "<display text>"}. '
    'You may mix plain string actions and typed action objects in the same array'
)


# Cacheable system prompt for card generation — stable across all users and
# every refresh, so Anthropic's prompt caching can serve it at ~90% discount
# on hits. Built once at module load.
_CARDS_SYSTEM_PROMPT = f"""You are a proactive AI academic advisor for McGill University.

You generate concise, high-signal briefing cards for a single student given their
profile, courses, calendar, and clubs. Each card surfaces a deadline, gap, opportunity,
or recommendation tailored to their situation. Do not invent facts; if something is
uncertain, frame it as a suggestion to verify.

CARD SCHEMA — every card must include:
  "type"     : one of "urgent" | "warning" | "insight" | "progress"
  "icon"     : single relevant emoji
  "label"    : short ALL-CAPS label (≤ 4 words)
  "title"    : concise headline (≤ 12 words)
  "body"     : 1–3 sentence explanation with specific, actionable detail
  {_ACTIONS_PROMPT}
  "category" : one of:
{CATEGORIES_PROMPT_LIST}
  "priority" : integer 1–8 (1 = most important)

TERM AWARENESS
The student's courses are separated into "COURSES THIS TERM" (the current
semester) and "REGISTERED FOR UPCOMING TERMS" (future semesters, grouped by
term). A student is often registered across MULTIPLE separate terms at once —
e.g. some courses in Fall 2026 and others in Winter 2027. These are DISTINCT
sequential semesters, not one block:
  - Never call upcoming-term courses "this term", "this semester", or "currently
    taking" — say "registered for <Term> <Year>".
  - Never merge courses from different terms into a single count or workload; a
    Fall course and a Winter course are not taken at the same time. If the
    student has 2 Fall and 3 Winter courses, that is NOT "5 courses this term".
  - Reason about workload, balance, and prerequisites per term (e.g. "your Fall
    2026 load" vs "your Winter 2027 load"), never lumped across the whole year.

PROFESSOR RECOMMENDATIONS FOR OPPORTUNITY CARDS
For "opportunities" cards, when relevant, recommend specific McGill professors the
student could reach out to based on their major, completed courses, and interests.
Include:
  - Professor name and department
  - Their research area that aligns with the student's profile
  - A suggested approach for reaching out (e.g. "Attend their office hours for COMP 251"
    or "Email about their ML research lab openings")
  - Only recommend professors when you have high confidence they are real McGill faculty
  - Add a disclaimer at the end of the card body: "Verify professor details on McGill's department website."
At least 1 of the 8 cards should be an "opportunities" card with a professor
recommendation if the student's profile has a declared major.

PROACTIVE MILESTONES → "advice" CATEGORY
The MILESTONES reference below lists situations with a stated trigger
condition and a confirmed (or "verify at X") procedure. Check the student's
DEGREE PROGRESS and profile data against each trigger. If one clearly
matches — e.g. a program's progress is at/near 100%, or their GPA falls in
a probation/unsatisfactory band — include ONE card with "category": "advice"
surfacing that specific milestone, using the procedure from MILESTONES
almost verbatim (don't paraphrase deadlines or numbers into something
vaguer). If nothing matches, don't force an "advice" card — it's fine for a
generation to have zero. Never invent a milestone that isn't in the
reference list below.

{_MILESTONES}
"""


def _deduplicate_completed(completed: list) -> list:
    """
    Filter completed courses so that withdrawn/failed attempts are excluded
    when the same course was later completed with a passing grade.

    This prevents the AI from suggesting "retake COMP 273" when the student
    already withdrew and then retook it successfully.
    """
    NON_PASSING = {"W", "F", "U", "WF", "WL", "J", "KF"}

    # Build a set of course codes that have at least one passing grade
    passed_codes = set()
    for c in completed:
        grade = (c.get("grade") or "").strip().upper()
        if grade and grade not in NON_PASSING:
            passed_codes.add(c.get("course_code", ""))

    # Filter: keep all entries UNLESS it's a non-passing grade for a course
    # that was later passed
    result = []
    for c in completed:
        code = c.get("course_code", "")
        grade = (c.get("grade") or "").strip().upper()
        if grade in NON_PASSING and code in passed_codes:
            # Skip this entry — the student retook and passed
            continue
        result.append(c)
    return result


def build_rich_context(ctx: dict, saved_cards: list = None, recent_titles: list[str] | None = None) -> str:
    user = ctx["user"]
    completed, current, favorites, calendar = (ctx["completed"], ctx["current"], ctx["favorites"], ctx["calendar"])
    # Deduplicate: remove withdrawn/failed entries when course was later passed
    completed = _deduplicate_completed(completed)
    total_credits = sum(c.get("credits") or 3 for c in completed)
    # Computed here (not trusted from the client) — see compute_degree_progress_summary's
    # docstring for why: it used to depend on the frontend having rendered the
    # Degree Planning tab this session, which produced wrong/missing numbers
    # whenever cards generated before that (the common case, from Home).
    try:
        degree_progress = compute_degree_progress_summary(user, completed, user.get("id", ""))
    except Exception:
        degree_progress = ""
    adv = user.get("advanced_standing") or []
    adv_credits = sum((a.get("credits") or 0) for a in adv)
    adv_summary = ", ".join(f"{a['course_code']} ({a.get('credits') or 0} cr)" for a in adv) or "None"

    def fmt_completed():
        # "Prof: X" is the student's own recorded instructor for that course —
        # real data, not a rating — shown only when known.
        return "\n".join(
            f"  - {c['course_code']} ({sanitise_context_field(c.get('course_title',''))}) | "
            f"Grade: {c.get('grade') or 'N/A'} | Term: {c.get('term','?')} {c.get('year','')}"
            + (f" | Prof: {sanitise_context_field(c['professor'])}" if c.get('professor') else "")
            for c in completed) or "  None recorded"

    def fmt_list(items, code_key="course_code", title_key="course_title", show_professor=False):
        def line(i):
            base = f"  - {i[code_key]} ({sanitise_context_field(i.get(title_key,''))})"
            if show_professor and i.get("professor"):
                base += f" — Prof. {sanitise_context_field(i['professor'])}"
            return base
        return "\n".join(line(i) for i in items) or "  None recorded"

    # Term-aware enrollment (mirrors chat.py): upcoming-term registrations
    # must not be described as courses the student is taking right now.
    from ..utils.terms import get_active_term, split_current_courses
    _active_term, _active_year = get_active_term()
    _taking_now, _upcoming_terms = split_current_courses(current)
    _upcoming_str = "\n".join(
        f"  {term} {year}:\n" + "\n".join(
            f"    - {c['course_code']} ({sanitise_context_field(c.get('course_title',''))})"
            + (f" — Prof. {sanitise_context_field(c['professor'])}" if c.get('professor') else "")
            for c in cs
        )
        for (term, year), cs in _upcoming_terms
    ) or "  None"

    calendar_str = "\n".join(
        f"  - {e['date']}: {sanitise_context_field(e['title'])} [{e.get('type','personal')}]"
        + (f" — {sanitise_context_field(e['description'])}" if e.get('description') else "")
        for e in calendar) or "  No upcoming events"

    majors_str = user.get("major", "Undeclared")
    for m in (user.get("other_majors") or []):
        majors_str += f", {m}"
    minors_str = user.get("minor") or "None"
    for m in (user.get("other_minors") or []):
        minors_str += f", {m}"

    saved_section = ""
    if saved_cards:
        saved_lines = "\n".join(
            f"  - [{c.get('category','planning')}] {sanitise_context_field(c['title'])}: {sanitise_context_field(c['body'][:120])}"
            for c in saved_cards)
        saved_section = f"\nSAVED CARDS (already pinned — DO NOT regenerate cards covering these topics):\n{saved_lines}\n"

    # Recently shown card titles — nudge the model toward different angles
    recent_section = ""
    if recent_titles:
        # Deduplicate (in case same title appears multiple times) and cap
        seen = set()
        unique = []
        for t in recent_titles:
            key = (t or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(sanitise_context_field(t))
            if len(unique) >= 24:
                break
        if unique:
            recent_section = (
                "\nRECENTLY SHOWN CARDS (the student has already seen these — DO NOT "
                "repeat the same topic, angle, or recommendation; surface NEW insights):\n"
                + "\n".join(f"  - {t}" for t in unique)
                + "\n"
            )

    # PRIVACY: no name/email/username in the prompt — Claude only sees
    # anonymous academic data (faculty, program, courses, credits).
    safe_faculty       = sanitise_context_field(str(user.get('faculty') or 'Not specified'))
    safe_majors        = sanitise_context_field(majors_str)
    safe_minors        = sanitise_context_field(minors_str)
    safe_concentration = sanitise_context_field(str(user.get('concentration') or 'None'))

    progress_section = ""
    if degree_progress:
        safe_progress = sanitise_context_field(degree_progress, max_length=3000)
        progress_section = (
            "\nDEGREE PROGRESS (computed from the student's own completed-course "
            "data against program requirements — not an official McGill record). "
            "Use these numbers directly and exactly as given; do not recompute, "
            "round differently, or blend them with your own estimate from the "
            "COMPLETED COURSES list below:\n"
            f"{safe_progress}\n"
        )

    # The stable header + instructions + professor guide are now in
    # _CARDS_SYSTEM_PROMPT (cacheable). This returns ONLY the per-user
    # data + return-format instruction for the user message.
    return f"""{saved_section}{recent_section}Today: {datetime.now(timezone.utc).date().isoformat()}

STUDENT PROFILE
  Faculty      : {safe_faculty}
  Major(s)     : {safe_majors}
  Minor(s)     : {safe_minors}
  Concentration: {safe_concentration}
  Year         : U{user.get('year') or '?'}
  Credits done : {total_credits} (+ {adv_credits} advanced standing: {adv_summary})
{progress_section}
COMPLETED COURSES
{fmt_completed()}

COURSES THIS TERM ({_active_term} {_active_year})
{fmt_list(_taking_now, show_professor=True)}

REGISTERED FOR UPCOMING TERMS (not yet started — say "registered for", never "currently taking" or "this term")
{_upcoming_str}

SAVED/FAVOURITED COURSES
{fmt_list(favorites)}

UPCOMING CALENDAR EVENTS
{calendar_str}

STUDENT CLUBS
  Joined: {', '.join(ctx.get('joined_clubs', [])) or 'None'}
  Created: {', '.join(c.get('name','') for c in ctx.get('created_clubs', [])) or 'None'}

Generate exactly 8 cards based on the schema in the system prompt.
Return ONLY the JSON array — no markdown, no commentary."""


def _build_single_card_prompt(question: str, ctx: dict, language: str, course_grounding: str | None = None) -> str:
    grounding_section = f"\n{course_grounding}\n" if course_grounding else ""
    return f"""You are a proactive AI academic advisor for McGill University.
A student has asked: "{question}"

Based on their profile below, generate a single helpful advisor card that directly answers their question.

{build_rich_context(ctx)}
{grounding_section}
Return a single JSON object (not an array) with these fields:
  "type"     : one of "urgent" | "warning" | "insight" | "progress"
  "icon"     : single emoji relevant to the answer
  "label"    : short ALL-CAPS label (≤ 4 words)
  "title"    : concise headline answering the question (≤ 12 words)
  "body"     : 2–4 sentence answer with specific, actionable detail
  {_ACTIONS_PROMPT}
  "category" : one of: {", ".join(f'"{c}"' for c in CARD_CATEGORIES)}

Return ONLY the JSON object — no markdown, no commentary.{_lang_instruction(language)}"""


def _get_stored_cards_language(user_id: str) -> str | None:
    """Read the cards language stored in Supabase auth user metadata."""
    try:
        supabase = get_supabase()
        resp = supabase.auth.admin.get_user_by_id(user_id)
        meta = (resp.user.user_metadata or {}) if resp.user else {}
        lang = meta.get("cards_language")
        # Only return a recognised language code — empty string or anything else → None
        return lang if lang in ("en", "fr", "zh") else None
    except Exception:
        return None


def _set_stored_cards_language(user_id: str, language: str) -> None:
    """Persist the cards language in Supabase auth user metadata."""
    try:
        supabase = get_supabase()
        resp = supabase.auth.admin.get_user_by_id(user_id)
        existing_meta = (resp.user.user_metadata or {}) if resp.user else {}
        existing_meta["cards_language"] = language
        supabase.auth.admin.update_user_by_id(user_id, {"user_metadata": existing_meta})
    except Exception as e:
        logger.warning(f"Could not persist cards language for {user_id}: {e}")


def _fetch_cards_response(user_id: str, confirmed_language: str | None = None, user_sb=None) -> dict:
    """
    confirmed_language: pass the language when cards were just generated/retranslated.
    When None, reads the previously persisted language from user metadata.
    """
    get_user_by_id(user_id)
    sb = user_sb if user_sb is not None else get_supabase()
    resp = (sb.table("advisor_cards").select("*")
        .eq("user_id", user_id).order("sort_order", desc=False).execute())
    cards = resp.data or []
    for card in cards:
        if isinstance(card.get("actions"), str):
            card["actions"] = json.loads(card["actions"])
    ai_cards = [c for c in cards if c.get("source") == "ai"]
    generated_at = ai_cards[0].get("generated_at") if ai_cards else None
    # Use confirmed language if just set; otherwise read from stored metadata
    cards_language = confirmed_language if confirmed_language else _get_stored_cards_language(user_id)
    return {
        "cards": cards, "count": len(cards), "generated_at": generated_at,
        "fresh": cards_are_fresh(user_id),
        "cards_language": cards_language,
    }


@router.get("/{user_id}", response_model=dict)
async def get_cards(user_id: str, req: Request, current_user_id: str = Depends(get_current_user_id), user_sb=Depends(get_user_db)):
    require_self(current_user_id, user_id)
    try:
        return _fetch_cards_response(user_id, user_sb=user_sb)
    except UserNotFoundException:
        raise HTTPException(status_code=404, detail="User not found")
    except Exception as e:
        logger.exception(f"Failed to get cards for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve advisor cards")


@router.post("/generate/{user_id}", response_model=dict)
async def generate_cards(user_id: str, request: GenerateRequest, req: Request, current_user_id: str = Depends(get_current_user_id), user_sb=Depends(get_user_db)):
    require_self(current_user_id, user_id)
    """
    Generate AI cards.
    - If force=False and cards are fresh (< 7 days old), returns existing cards immediately.
    - If force=True, regenerates (max 2 times per week).

    SEC FIX #5 / #7: gated on verified email + daily cost cap.
    """
    from ..utils.verified_user import is_email_verified
    from ..utils.llm_budget import check_and_record_llm_usage
    if not is_email_verified(current_user_id):
        raise HTTPException(status_code=403, detail={"code": "email_not_verified", "message": "Verify your email to generate cards."})
    if request.force:
        check_and_record_llm_usage(current_user_id, kind="cards")
    try:
        get_user_by_id(user_id)
        if not request.force and cards_are_fresh(user_id):
            logger.info(f"Cards already fresh for {user_id}, skipping generation")
            # confirmed_language=None: we don't know what language these cached cards are in.
            # The frontend will check and retranslate if needed.
            return _fetch_cards_response(user_id, confirmed_language=None, user_sb=user_sb)

        # Rate limit: max 2 forced regenerations per week
        if request.force:
            gen_count = _count_generations_this_week(user_id)
            if gen_count >= 2:
                logger.info(f"Rate limit: {user_id} already generated {gen_count} times this week")
                return _fetch_cards_response(user_id, user_sb=user_sb)

        ctx = fetch_student_context(user_id, user_sb=user_sb)
        saved = fetch_saved_cards(user_id, user_sb=user_sb)
        # Only pass recent titles when the user is forcing a refresh — on a
        # first generation there's nothing to diversify against.
        recent_titles = fetch_recent_card_titles(user_id, user_sb=user_sb) if request.force else []
        prompt = build_rich_context(ctx, saved_cards=saved, recent_titles=recent_titles) + _lang_instruction(request.language)

        client = get_anthropic_client()
        message = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=4096,
            system=[{
                "type":          "text",
                "text":          _CARDS_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        cards = json.loads(raw)
        if not isinstance(cards, list):
            raise ValueError("AI did not return a JSON array")
        for card in cards:
            card["category"] = _sanitise_category(card)
            card.setdefault("type", "insight")
            card.setdefault("icon", "💡")
            card.setdefault("actions", [])

        save_cards(user_id, cards)
        _set_stored_cards_language(user_id, request.language)
        logger.info(f"Generated {len(cards)} cards for {user_id} in {request.language}")
        return _fetch_cards_response(user_id, confirmed_language=request.language, user_sb=user_sb)

    except UserNotFoundException:
        raise HTTPException(status_code=404, detail="User not found")
    except json.JSONDecodeError as e:
        logger.error(f"Card JSON parse error: {e}")
        raise HTTPException(status_code=500, detail="Failed to parse AI response")
    except Exception as e:
        logger.exception(f"Card generation failed for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate advisor cards")


async def _fetch_student_context_parallel(user_id: str, user_sb=None) -> dict:
    """Same as fetch_student_context but runs all DB queries in parallel."""
    sb = user_sb if user_sb is not None else get_supabase()
    # user lookup is needed before parallel queries but is a single fast call
    user = await asyncio.to_thread(get_user_by_id, user_id)

    today = datetime.now(timezone.utc).date().isoformat()

    def q_favorites():
        return (sb.table("favorites")
            .select("course_code, course_title, subject, catalog")
            .eq("user_id", user_id).order("created_at", desc=True).limit(30)
            .execute().data or [])

    def q_completed():
        return (sb.table("completed_courses")
            .select("course_code, course_title, subject, catalog, term, year, grade, credits, professor")
            .eq("user_id", user_id).order("year", desc=True).limit(50)
            .execute().data or [])

    def q_current():
        return (sb.table("current_courses")
            .select("course_code, course_title, subject, catalog, credits, term, year, professor")
            .eq("user_id", user_id).execute().data or [])

    def q_calendar():
        return (sb.table("calendar_events")
            .select("title, date, time, type, description")
            .eq("user_id", user_id).gte("date", today)
            .order("date", desc=False).limit(20)
            .execute().data or [])

    def q_clubs():
        joined, created = [], []
        try:
            r = sb.table("user_clubs").select("clubs(name, category, meeting_schedule)").eq("user_id", user_id).execute()
            joined = [x.get("clubs", {}).get("name", "Unknown") for x in (r.data or []) if x.get("clubs")]
        except Exception:
            pass
        try:
            r = sb.table("clubs").select("name, category, is_private").eq("created_by", user_id).execute()
            created = r.data or []
        except Exception:
            pass
        return joined, created

    favorites, completed, current, calendar, (joined_clubs, created_clubs) = await asyncio.gather(
        asyncio.to_thread(q_favorites),
        asyncio.to_thread(q_completed),
        asyncio.to_thread(q_current),
        asyncio.to_thread(q_calendar),
        asyncio.to_thread(q_clubs),
    )

    return {"user": user, "favorites": favorites, "completed": completed,
            "current": current, "calendar": calendar,
            "joined_clubs": joined_clubs, "created_clubs": created_clubs}


def _build_ndjson_context(ctx: dict, saved_cards: list = None, recent_titles: list[str] | None = None) -> str:
    """Like build_rich_context but asks for NDJSON output (one card per line) for streaming."""
    base = build_rich_context(ctx, saved_cards=saved_cards, recent_titles=recent_titles)
    # Replace the final return instruction with NDJSON variant
    return base.replace(
        "Return ONLY the JSON array — no markdown, no commentary.",
        "Return exactly 8 cards as newline-delimited JSON (NDJSON): one complete JSON object per line, "
        "no surrounding array brackets, no markdown fences. Each line must be a valid standalone JSON object."
    )


@router.post("/stream/{user_id}")
async def stream_cards(
    user_id: str, request: GenerateRequest, req: Request,
    current_user_id: str = Depends(get_current_user_id),
    user_sb=Depends(get_user_db),
):
    """
    SSE streaming card generation. Emits one `data:` event per card as Claude
    produces them, then a final `done` event. Language handling is identical to
    the non-streaming /generate endpoint.

    SEC FIX #5 / #7: verified email + daily LLM budget.
    """
    require_self(current_user_id, user_id)
    from ..utils.verified_user import is_email_verified
    from ..utils.llm_budget import check_and_record_llm_usage
    if not is_email_verified(current_user_id):
        raise HTTPException(status_code=403, detail={"code": "email_not_verified", "message": "Verify your email to generate cards."})
    check_and_record_llm_usage(current_user_id, kind="cards")

    # Auth check up front (outside the generator so HTTP errors still work)
    try:
        get_user_by_id(user_id)
    except UserNotFoundException:
        raise HTTPException(status_code=404, detail="User not found")

    # If cards are fresh and not forced, stream existing cards instantly
    if not request.force and cards_are_fresh(user_id):
        existing = _fetch_cards_response(user_id, confirmed_language=None, user_sb=user_sb)

        async def _instant_stream():
            for i, card in enumerate(existing.get("cards", [])):
                yield f"data: {json.dumps({'type': 'card', 'card': card, 'index': i})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'count': len(existing.get('cards', [])), 'language': existing.get('cards_language'), 'fresh': True})}\n\n"

        return StreamingResponse(_instant_stream(), media_type="text/event-stream",
                                  headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Rate-limit check
    if request.force:
        gen_count = await asyncio.to_thread(_count_generations_this_week, user_id)
        if gen_count >= 2:
            existing = _fetch_cards_response(user_id, user_sb=user_sb)

            async def _rate_limited_stream():
                for i, card in enumerate(existing.get("cards", [])):
                    yield f"data: {json.dumps({'type': 'card', 'card': card, 'index': i})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'count': len(existing.get('cards', [])), 'language': existing.get('cards_language'), 'rate_limited': True})}\n\n"

            return StreamingResponse(_rate_limited_stream(), media_type="text/event-stream",
                                      headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    language = request.language

    async def _generate_stream():
        try:
            # Parallel DB fetch — also include recent card titles on forced refresh
            # so Claude doesn't repeat the same topics.
            tasks = [
                _fetch_student_context_parallel(user_id, user_sb),
                asyncio.to_thread(fetch_saved_cards, user_id, user_sb),
            ]
            if request.force:
                tasks.append(asyncio.to_thread(fetch_recent_card_titles, user_id, user_sb))
            results = await asyncio.gather(*tasks)
            ctx, saved = results[0], results[1]
            recent_titles = results[2] if len(results) > 2 else []

            prompt = _build_ndjson_context(ctx, saved_cards=saved, recent_titles=recent_titles) + _lang_instruction(language)
            async_client = get_async_anthropic_client()

            collected_cards: list = []
            line_buffer = ""

            # Anthropic prompt caching — static card playbook lives in the
            # system block and is served at ~90% discount on subsequent calls
            # within the 5-min cache window.
            async with async_client.messages.stream(
                model=settings.CLAUDE_MODEL,
                max_tokens=4096,
                system=[{
                    "type":          "text",
                    "text":          _CARDS_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    line_buffer += text
                    while "\n" in line_buffer:
                        line, line_buffer = line_buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        # Strip stray array brackets Claude may emit
                        if line in ("[", "]") or line.startswith("```"):
                            continue
                        line = line.rstrip(",")
                        try:
                            card = json.loads(line)
                            card["category"] = _sanitise_category(card)
                            card.setdefault("type", "insight")
                            card.setdefault("icon", "💡")
                            card.setdefault("actions", [])
                            collected_cards.append(card)
                            yield f"data: {json.dumps({'type': 'card', 'card': card, 'index': len(collected_cards) - 1})}\n\n"
                        except json.JSONDecodeError:
                            pass

            # Handle any trailing content in the buffer
            if line_buffer.strip() and line_buffer.strip() not in ("[", "]"):
                try:
                    card = json.loads(line_buffer.strip().rstrip(","))
                    card["category"] = _sanitise_category(card)
                    card.setdefault("type", "insight")
                    card.setdefault("icon", "💡")
                    card.setdefault("actions", [])
                    collected_cards.append(card)
                    yield f"data: {json.dumps({'type': 'card', 'card': card, 'index': len(collected_cards) - 1})}\n\n"
                except json.JSONDecodeError:
                    pass

            if collected_cards:
                await asyncio.to_thread(save_cards, user_id, collected_cards)
                await asyncio.to_thread(_set_stored_cards_language, user_id, language)
                logger.info(f"Streamed {len(collected_cards)} cards for {user_id} in {language}")

            yield f"data: {json.dumps({'type': 'done', 'count': len(collected_cards), 'language': language})}\n\n"

        except Exception as e:
            logger.exception(f"Card stream failed for {user_id}: {e}")
            yield f"data: {json.dumps({'type': 'error', 'detail': 'Card generation failed'})}\n\n"

    return StreamingResponse(
        _generate_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/retranslate/{user_id}", response_model=dict)
async def retranslate_cards(user_id: str, request: RetranslateRequest, req: Request, current_user_id: str = Depends(get_current_user_id), user_sb=Depends(get_user_db)):
    require_self(current_user_id, user_id)
    try:
        get_user_by_id(user_id)
        # Clear the stored language BEFORE generation so that if the AI call
        # fails or produces the wrong language, the metadata is null rather
        # than falsely claiming the new language — forcing a fresh retranslation
        # on next load instead of silently showing wrong-language cards forever.
        _set_stored_cards_language(user_id, "")
        ctx = fetch_student_context(user_id, user_sb=user_sb)
        # Don't include saved_cards — they may be in a different language
        # which would influence the model to respond in that language instead.
        prompt = build_rich_context(ctx, saved_cards=None) + _lang_instruction(request.language)

        client = get_anthropic_client()

        # Haiku occasionally emits slightly malformed JSON (e.g. a missing
        # comma). Tolerate trailing commas / prose wrappers, and retry the
        # call once before giving up — this turns a hard 500 into a rare miss.
        cards = None
        for attempt in range(2):
            message = client.messages.create(
                model=settings.CLAUDE_MODEL,
                max_tokens=4096,
                system=[{
                    "type":          "text",
                    "text":          _CARDS_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            # Keep only the JSON array and strip trailing commas before ] or }.
            if "[" in raw and "]" in raw:
                raw = raw[raw.find("["):raw.rfind("]") + 1]
            raw = re.sub(r",(\s*[\]}])", r"\1", raw)
            try:
                cards = json.loads(raw)
                break
            except json.JSONDecodeError as e:
                if attempt == 0:
                    logger.warning(f"Retranslate JSON invalid, retrying once: {e}")
                    continue
                # Last attempt: fall back to recovering individual cards
                # rather than losing the whole batch over e.g. one missing
                # comma between two objects.
                cards = _parse_cards_lenient(raw)
                if not cards:
                    raise
                logger.warning(
                    f"Retranslate JSON invalid on final attempt, recovered "
                    f"{len(cards)} card(s) via lenient per-object parsing: {e}"
                )
        if not isinstance(cards, list):
            raise ValueError("AI did not return a JSON array")
        for card in cards:
            card["category"] = _sanitise_category(card)
            card.setdefault("type", "insight")
            card.setdefault("icon", "💡")
            card.setdefault("actions", [])
        save_cards(user_id, cards)
        _set_stored_cards_language(user_id, request.language)
        logger.info(f"Retranslated {len(cards)} cards for {user_id} in {request.language}")
        return _fetch_cards_response(user_id, confirmed_language=request.language, user_sb=user_sb)

    except UserNotFoundException:
        raise HTTPException(status_code=404, detail="User not found")
    except json.JSONDecodeError as e:
        logger.error(f"Retranslate JSON parse error: {e}")
        raise HTTPException(status_code=500, detail="Failed to parse AI response")
    except Exception as e:
        logger.exception(f"Retranslate failed for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retranslate cards")


@router.delete("/{user_id}", status_code=204)
async def delete_cards(user_id: str, req: Request, current_user_id: str = Depends(get_current_user_id), user_sb=Depends(get_user_db)):
    require_self(current_user_id, user_id)
    try:
        get_user_by_id(user_id)
        user_sb.table("advisor_cards").delete().eq("user_id", user_id).eq("source", "ai").execute()
        return None
    except UserNotFoundException:
        raise HTTPException(status_code=404, detail="User not found")
    except Exception as e:
        logger.exception(f"Failed to delete cards for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete cards")


@router.delete("/{user_id}/{card_id}", status_code=204)
async def delete_card(user_id: str, card_id: str, req: Request, current_user_id: str = Depends(get_current_user_id), user_sb=Depends(get_user_db)):
    require_self(current_user_id, user_id)
    try:
        get_user_by_id(user_id)
        user_sb.table("advisor_cards").delete().eq("id", card_id).eq("user_id", user_id).execute()
        return None
    except UserNotFoundException:
        raise HTTPException(status_code=404, detail="User not found")
    except Exception as e:
        logger.exception(f"Failed to delete card {card_id} for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete card")


@router.post("/ask/{user_id}", response_model=dict)
async def ask_card(user_id: str, request: AskRequest, req: Request, current_user_id: str = Depends(get_current_user_id), user_sb=Depends(get_user_db)):
    require_self(current_user_id, user_id)
    from ..utils.verified_user import is_email_verified
    from ..utils.llm_budget import check_and_record_llm_usage
    if not is_email_verified(current_user_id):
        raise HTTPException(status_code=403, detail={"code": "email_not_verified", "message": "Verify your email to ask the advisor."})
    check_and_record_llm_usage(current_user_id, kind="cards")
    sanitise_user_message(request.question)
    try:
        get_user_by_id(user_id)
        ctx = fetch_student_context(user_id, user_sb=user_sb)
        course_grounding = build_course_grounding_block(request.question)
        prompt = _build_single_card_prompt(request.question, ctx, request.language, course_grounding)

        client = get_anthropic_client()
        message = client.messages.create(model=settings.CLAUDE_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}])

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        card_data = json.loads(raw)
        card = insert_user_card(user_id, card_data, request.question, request.language)
        return {"card": card}

    except UserNotFoundException:
        raise HTTPException(status_code=404, detail="User not found")
    except json.JSONDecodeError as e:
        logger.error(f"Ask card JSON parse error: {e}")
        raise HTTPException(status_code=500, detail="Failed to parse AI response")
    except Exception as e:
        logger.exception(f"Ask card failed for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate card from question")


@router.post("/{card_id}/thread", response_model=dict)
async def thread_message(
    card_id: str,
    request: ThreadRequest,
    req: Request,
    current_user_id: str = Depends(get_current_user_id),
    user_sb=Depends(get_user_db),
):
    """
    Follow-up thread on a card.
    Now uses build_system_context() from chat.py — same full student context
    as main chat. Card body is injected via card_context. Thread history is
    passed in the messages array for conversation continuity.
    """
    require_self(current_user_id, request.user_id)
    from ..utils.verified_user import is_email_verified
    from ..utils.llm_budget import check_and_record_llm_usage
    if not is_email_verified(current_user_id):
        raise HTTPException(status_code=403, detail={"code": "email_not_verified", "message": "Verify your email to use the advisor."})
    check_and_record_llm_usage(current_user_id, kind="cards")
    sanitise_user_message(request.message)

    try:
        card_row = (user_sb.table("advisor_cards")
            .select("user_id, source, prompted_language").eq("id", card_id).execute())
        if not card_row.data:
            raise HTTPException(status_code=404, detail="Card not found")
        if card_row.data[0]["user_id"] != current_user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        reply_language = request.language
        if card_row.data[0].get("source") == "user":
            reply_language = card_row.data[0].get("prompted_language") or request.language

        # Import here to avoid circular imports at module level
        from api.routes.chat import build_system_context

        user = get_user_by_id(request.user_id)
        system_context = build_system_context(
            user,
            current_tab=None,
            language=reply_language,
            card_context=sanitise_context_field(request.card_context, max_length=800),
        )

        # Real catalogue data for any course code mentioned in THIS message —
        # see build_course_grounding_block's docstring / chat.py's send_message
        # for why this exists. No cache_control is used on this endpoint's
        # system param, so it's safe to just concatenate.
        course_grounding = build_course_grounding_block(request.message)
        if course_grounding:
            system_context = f"{system_context}\n\n{course_grounding}"

        # Build messages array: prior thread turns + current message
        messages = []
        if request.thread_history:
            for msg in request.thread_history[-16:]:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": request.message})

        client = get_anthropic_client()
        logger.info(
            f"Card thread {card_id}: {len(messages)} messages, "
            f"lang={reply_language}, user={request.user_id}"
        )
        response = client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=512,
            system=system_context,
            messages=messages,
        )
        return {"response": response.content[0].text.strip()}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Thread message failed for card {card_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to process thread message")


@router.patch("/{card_id}/save", response_model=dict)
async def save_card(card_id: str, request: SaveRequest, req: Request, current_user_id: str = Depends(get_current_user_id), user_sb=Depends(get_user_db)):
    try:
        ownership = user_sb.table("advisor_cards").select("user_id").eq("id", card_id).execute()
        if not ownership.data:
            raise HTTPException(status_code=404, detail="Card not found")
        if ownership.data[0]["user_id"] != current_user_id:
            raise HTTPException(status_code=403, detail="Access denied")
        result = user_sb.table("advisor_cards").update({"is_saved": request.is_saved}).eq("id", card_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Card not found")
        card = result.data[0]
        if isinstance(card.get("actions"), str):
            card["actions"] = json.loads(card["actions"])
        return card
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to save card {card_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update card")


@router.patch("/{user_id}/reorder", response_model=dict)
async def reorder_cards(user_id: str, request: ReorderRequest, req: Request, current_user_id: str = Depends(get_current_user_id), user_sb=Depends(get_user_db)):
    require_self(current_user_id, user_id)
    try:
        get_user_by_id(user_id)
        # The reorder_advisor_cards RPC (defined in Supabase) updates the
        # `sort_order` column — send both key names so it's correct
        # regardless of which one the RPC's JSON extraction actually reads.
        # (Previously only "position" was sent, which — if the RPC reads
        # "sort_order" as its column name suggests — meant every reorder
        # request wrote NULL into a NOT NULL column.)
        payload = [
            {"id": item.id, "position": item.resolved_position, "sort_order": item.resolved_position}
            for item in request.order
        ]
        user_sb.rpc("reorder_advisor_cards", {"payload": payload}).execute()
        logger.info(f"Successfully reordered {len(request.order)} cards for user {user_id}")
        return {"reordered": len(request.order)}
    except UserNotFoundException:
        raise HTTPException(status_code=404, detail="User not found")
    except Exception as e:
        logger.exception(f"Failed to reorder cards for {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to reorder cards")

# ════════════════════════════════════════════════════════════════════
#  Post-finals transcript reminder
# ════════════════════════════════════════════════════════════════════

def is_post_finals_day(today: "date | None" = None) -> bool:
    """Returns True if `today` is one of the configured post-finals dates."""
    from datetime import date as _date
    t = today or _date.today()
    return (t.month, t.day) in POST_FINALS_REMINDER_DATES


def _has_recent_transcript_activity(user_id: str, within_days: int = 14) -> bool:
    """Skip reminder if user has imported any course rows in the last N days
       (suggests they already re-uploaded after finals)."""
    try:
        supabase = get_supabase()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
        resp = (supabase.table("completed_courses")
                .select("id")
                .eq("user_id", user_id)
                .gte("created_at", cutoff)
                .limit(1).execute())
        return bool(resp.data)
    except Exception:
        return False


def _has_active_transcript_reminder(user_id: str) -> bool:
    """True if a transcript-reminder card was already inserted today (idempotency)."""
    try:
        supabase = get_supabase()
        today_iso = datetime.now(timezone.utc).date().isoformat()
        resp = (supabase.table("advisor_cards")
                .select("id")
                .eq("user_id", user_id)
                .eq("label", TRANSCRIPT_REMINDER_LABEL)
                .gte("generated_at", today_iso)
                .limit(1).execute())
        return bool(resp.data)
    except Exception:
        return False


def _insert_transcript_reminder_card(user_id: str) -> bool:
    """
    Insert a high-priority transcript-reminder card for one user.
    Returns True if inserted, False if skipped (already exists / recent upload).
    """
    if _has_active_transcript_reminder(user_id):
        return False
    if _has_recent_transcript_activity(user_id, within_days=14):
        return False
    try:
        supabase = get_supabase()
        now = datetime.now(timezone.utc).isoformat()
        # Push existing cards down by 1 so this reminder appears at the top
        try:
            existing = (supabase.table("advisor_cards").select("id, sort_order")
                        .eq("user_id", user_id).execute().data or [])
            for c in existing:
                supabase.table("advisor_cards") \
                    .update({"sort_order": (c.get("sort_order") or 0) + 1}) \
                    .eq("id", c["id"]).execute()
        except Exception:
            pass

        row = {
            "user_id": user_id,
            "source": "system",
            "card_type": "urgent",
            "icon": "📄",
            "label": TRANSCRIPT_REMINDER_LABEL,
            "title": "Re-upload your transcript",
            "body": (
                "Final grades for this term should now be posted on Minerva. "
                "Re-upload your unofficial transcript to keep your courses, GPA, "
                "and degree progress current. (Minerva → Unofficial Transcript → "
                "⌘/Ctrl + P → Save as PDF)"
            ),
            "actions": json.dumps([
                {"type": "open_transcript_upload", "label": "Upload transcript"},
                "How do I get my unofficial transcript PDF?",
                "What gets updated when I re-upload?",
            ]),
            "category": "planning",
            "priority": 1,
            "sort_order": 0,
            "generated_at": now,
        }
        supabase.table("advisor_cards").insert(row).execute()
        return True
    except Exception as e:
        logger.warning(f"Failed to insert transcript reminder for {user_id}: {e}")
        return False


def run_transcript_reminder_cron() -> dict:
    """
    Daily cron: if today is a post-finals date, drop a reminder card into every
    user's brief (idempotent — won't duplicate within the same day, and skips
    users who already re-uploaded recently).
    """
    if not is_post_finals_day():
        return {"sent": 0, "skipped": "not_post_finals_day"}
    try:
        supabase = get_supabase()
        users_resp = supabase.table("users").select("id").execute()
        sent = 0
        for u in (users_resp.data or []):
            if u.get("id") and _insert_transcript_reminder_card(u["id"]):
                sent += 1
        logger.info(f"Transcript reminder cron: inserted {sent} cards")
        return {"sent": sent}
    except Exception as e:
        logger.exception(f"Transcript reminder cron failed: {e}")
        return {"sent": 0, "error": str(e)}


# ════════════════════════════════════════════════════════════════════
#  Course-registration opens reminder
# ════════════════════════════════════════════════════════════════════

def is_course_registration_day(today: "date | None" = None) -> bool:
    """True if today is one of the configured course-registration reminder dates."""
    from datetime import date as _date
    t = today or _date.today()
    return (t.month, t.day) in COURSE_REGISTRATION_REMINDER_DATES


def _has_active_course_registration_card(user_id: str) -> bool:
    """True if a course-registration card was already inserted today (idempotency)."""
    try:
        supabase = get_supabase()
        today_iso = datetime.now(timezone.utc).date().isoformat()
        resp = (supabase.table("advisor_cards")
                .select("id")
                .eq("user_id", user_id)
                .eq("label", COURSE_REGISTRATION_REMINDER_LABEL)
                .gte("generated_at", today_iso)
                .limit(1).execute())
        return bool(resp.data)
    except Exception:
        return False


def _insert_course_registration_card(user_id: str, days_before_open: int) -> bool:
    """
    Insert a course-registration card for one user. days_before_open distinguishes
    the heads-up card (>0) from the day-of card (0).
    """
    if _has_active_course_registration_card(user_id):
        return False
    try:
        supabase = get_supabase()
        now = datetime.now(timezone.utc).isoformat()

        # Push existing cards down by 1 so this one appears at the top
        try:
            existing = (supabase.table("advisor_cards").select("id, sort_order")
                        .eq("user_id", user_id).execute().data or [])
            for c in existing:
                supabase.table("advisor_cards") \
                    .update({"sort_order": (c.get("sort_order") or 0) + 1}) \
                    .eq("id", c["id"]).execute()
        except Exception:
            pass

        # Wording shifts based on whether registration is opening today or upcoming.
        # Both cards lead with the same point: Fall and Winter open TOGETHER, so
        # this is one sitting for the whole year. Students who plan only Fall
        # come back in November to find the Winter sections they wanted full.
        if days_before_open > 0:
            title = f"Registration opens in {days_before_open} days — plan Fall AND Winter"
            body = (
                f"McGill course registration opens in about {days_before_open} days, and Fall and "
                "Winter open at the SAME TIME — you register for both terms in one sitting, not "
                "twice. Check Minerva for the exact day and time YOUR registration opens: McGill "
                "staggers start times per student, so yours may differ from a friend's. Plan a full "
                "year before then — the courses you need for Fall, the ones for Winter, and a "
                "backup for each. If you only plan Fall, the Winter sections you wanted are usually "
                "gone by the time you come back to them."
            )
            card_type = "warning"
        else:
            title = "Registration is open — register for Fall AND Winter"
            body = (
                "McGill course registration is opening. Fall and Winter open together, so register "
                "for BOTH terms in one session — there is no second registration date for Winter, "
                "and popular sections fill within hours. Registration times are staggered per "
                "student, so check Minerva for your own start time and be ready at it rather than "
                "assuming it is this morning. Watch for time conflicts within each term, confirm "
                "prerequisites carry across from Fall to Winter, and have a backup ready for "
                "anything that fills up."
            )
            card_type = "urgent"

        row = {
            "user_id": user_id,
            "source": "system",
            "card_type": card_type,
            "icon": "📝",
            "label": COURSE_REGISTRATION_REMINDER_LABEL,
            "title": title,
            "body": body,
            "actions": json.dumps([
                # Opens an inline date/time form in the card. The student's own
                # start time is the one thing this card cannot know — McGill
                # staggers them — so they read it off Minerva and enter it, and
                # we put it on their calendar with a reminder the day before.
                {"type": "set_registration_time", "label": "Add my registration time to my calendar"},
                {"type": "open_degree_planning", "label": "Open Degree Planning"},
                "Which courses should I take in Fall and which in Winter?",
                "Where in Minerva do I find my registration start time?",
                "Do any of my Winter choices need a Fall prerequisite first?",
                "What backups should I have ready if my first picks fill up?",
            ]),
            "category": "deadlines",
            "priority": 1,
            "sort_order": 0,
            "generated_at": now,
        }
        supabase.table("advisor_cards").insert(row).execute()
        return True
    except Exception as e:
        logger.warning(f"Failed to insert course-registration card for {user_id}: {e}")
        return False


def run_course_registration_reminder_cron() -> dict:
    """
    Daily cron: if today is one of the configured registration-reminder dates,
    drop a card into every user's brief (idempotent — one card per user per day).
    """
    if not is_course_registration_day():
        return {"sent": 0, "skipped": "not_registration_day"}
    try:
        from datetime import date as _date
        today = _date.today()
        # Distance to the closest "registration opens" date in our list. If today
        # IS the opens day, days_before_open = 0; if it's the heads-up day, > 0.
        # We treat the LATEST date in the list as "the actual opens day" since
        # earlier entries are heads-up dates.
        sorted_dates = sorted(COURSE_REGISTRATION_REMINDER_DATES, key=lambda x: (x[0], x[1]))
        opens_month, opens_day = sorted_dates[-1]
        try:
            opens_date = today.replace(month=opens_month, day=opens_day)
            days_before_open = max(0, (opens_date - today).days)
        except ValueError:
            days_before_open = 0

        supabase = get_supabase()
        users_resp = supabase.table("users").select("id").execute()
        sent = 0
        for u in (users_resp.data or []):
            if u.get("id") and _insert_course_registration_card(u["id"], days_before_open):
                sent += 1
        logger.info(f"Course-registration reminder cron: inserted {sent} cards (days_before_open={days_before_open})")
        return {"sent": sent, "days_before_open": days_before_open}
    except Exception as e:
        logger.exception(f"Course-registration reminder cron failed: {e}")
        return {"sent": 0, "error": str(e)}


# ════════════════════════════════════════════════════════════════════
#  Summer-course-registration opens reminder
# ════════════════════════════════════════════════════════════════════

def is_summer_registration_day(today: "date | None" = None) -> bool:
    """True if today is one of the configured summer-registration reminder dates."""
    from datetime import date as _date
    t = today or _date.today()
    return (t.month, t.day) in SUMMER_REGISTRATION_REMINDER_DATES


def _has_active_summer_registration_card(user_id: str) -> bool:
    """True if a summer-registration card was already inserted today (idempotency)."""
    try:
        supabase = get_supabase()
        today_iso = datetime.now(timezone.utc).date().isoformat()
        resp = (supabase.table("advisor_cards")
                .select("id")
                .eq("user_id", user_id)
                .eq("label", SUMMER_REGISTRATION_REMINDER_LABEL)
                .gte("generated_at", today_iso)
                .limit(1).execute())
        return bool(resp.data)
    except Exception:
        return False


def _insert_summer_registration_card(user_id: str, days_before_open: int) -> bool:
    """
    Insert a summer-registration card for one user. Tone is informational
    rather than urgent — only a subset of students take summer courses.
    """
    if _has_active_summer_registration_card(user_id):
        return False
    try:
        supabase = get_supabase()
        now = datetime.now(timezone.utc).isoformat()

        # Push existing cards down by 1 so this card lands near the top
        try:
            existing = (supabase.table("advisor_cards").select("id, sort_order")
                        .eq("user_id", user_id).execute().data or [])
            for c in existing:
                supabase.table("advisor_cards") \
                    .update({"sort_order": (c.get("sort_order") or 0) + 1}) \
                    .eq("id", c["id"]).execute()
        except Exception:
            pass

        if days_before_open > 0:
            title = f"Summer course registration opens in {days_before_open} days"
            body = (
                f"Planning to take a summer course? McGill summer registration opens in about "
                f"{days_before_open} days. Decide whether you want to lighten next year's load, "
                "catch up on a prereq, or knock out an elective — and have your picks ready."
            )
            card_type = "insight"
        else:
            title = "Summer course registration is open"
            body = (
                "McGill summer course registration is now open on Minerva. If you're planning to "
                "take a summer course — to catch up on a prereq, lighten next year's load, or "
                "explore something new — register soon since summer sections often have limited "
                "seats."
            )
            card_type = "warning"

        row = {
            "user_id": user_id,
            "source": "system",
            "card_type": card_type,
            "icon": "☀️",
            "label": SUMMER_REGISTRATION_REMINDER_LABEL,
            "title": title,
            "body": body,
            "actions": json.dumps([
                {"type": "open_degree_planning", "label": "Open Degree Planning"},
                "Should I take a summer course this year?",
                "Which prereqs could I catch up on over the summer?",
                "Are there any electives I should knock out in summer term?",
            ]),
            "category": "planning",
            "priority": 2,
            "sort_order": 0,
            "generated_at": now,
        }
        supabase.table("advisor_cards").insert(row).execute()
        return True
    except Exception as e:
        logger.warning(f"Failed to insert summer-registration card for {user_id}: {e}")
        return False


def run_summer_registration_reminder_cron() -> dict:
    """
    Daily cron: if today is a configured summer-registration reminder date,
    drop a card into every user's brief (idempotent — one card per user per day).
    """
    if not is_summer_registration_day():
        return {"sent": 0, "skipped": "not_summer_registration_day"}
    try:
        from datetime import date as _date
        today = _date.today()
        # Latest configured date = "registration opens"; earlier ones are heads-ups
        sorted_dates = sorted(SUMMER_REGISTRATION_REMINDER_DATES, key=lambda x: (x[0], x[1]))
        opens_month, opens_day = sorted_dates[-1]
        try:
            opens_date = today.replace(month=opens_month, day=opens_day)
            days_before_open = max(0, (opens_date - today).days)
        except ValueError:
            days_before_open = 0

        supabase = get_supabase()
        users_resp = supabase.table("users").select("id").execute()
        sent = 0
        for u in (users_resp.data or []):
            if u.get("id") and _insert_summer_registration_card(u["id"], days_before_open):
                sent += 1
        logger.info(f"Summer-registration reminder cron: inserted {sent} cards (days_before_open={days_before_open})")
        return {"sent": sent, "days_before_open": days_before_open}
    except Exception as e:
        logger.exception(f"Summer-registration reminder cron failed: {e}")
        return {"sent": 0, "error": str(e)}
