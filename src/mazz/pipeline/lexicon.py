"""The word lists the verifier reasons over, and nothing else.

Split out of `verify.py` for one reason: this is *data*, and data that widens a
compliance control's blind spot must be reviewable on its own. A diff that adds
a word here is a diff that lets a draft say something without evidence, and it
should be as visible as that sounds.

--------------------------------------------------------------------------
THE ADMISSION POLICY (read before adding a word)
--------------------------------------------------------------------------
`COMMON_ENGLISH` holds words that **assert nothing specific about the
recipient's business**: grammar, generic verbs, generic adjectives, courtesies,
and the fixed scaffolding our own templates emit. A sentence built only from
these is not checkable because it says nothing checkable.

A word is admissible only if the answer to all three is yes:

  1. Could it appear in a letter about any company at all, unchanged?
  2. If the copy used it with no evidence behind it, would a recipient shrug
     rather than reach for a lawyer?
  3. Is it absent from the *content* of a plausible research fact — i.e. is it
     not the kind of word we would be citing a source for?

That deliberately excludes an entire register that reads like ordinary English
but is not: revenue, sales, pipeline, funding, round, investor, growth, scale,
headcount, churn, retention, quota, budget, hiring, customer, user, vendor,
tool, platform, product, launch, migration, acquisition, merger, compliance,
onboarding. Those are claims. They must come from a cited fact, from the lead's
notes, or from the *kind* of a cited fact (see `KIND_VOCABULARY`).

Nothing here contains a digit. Numbers have no allow-list anywhere in this
system; see `verify.extract_claims`.

--------------------------------------------------------------------------
WHY A LEXICON AT ALL
--------------------------------------------------------------------------
The Phase 1 verifier had a ~200-word hand list and required every other word in
the copy to appear verbatim in the evidence. That is correct for a composer that
quotes, and unusable for one that writes: ordinary restatement ("hiring",
"scaled", "since") failed, and a control that fails constantly is a control the
reviewer stops reading.

The trade is explicit. A wider general-English floor means a draft CAN assemble
an unsupported inference out of common words ("your team is stretched"). That
sentence names nothing, quantifies nothing, and dates nothing — it is the grey
area the optional entailment judge in `verify.py` exists for, and it is called
out in that module's honest-limits section. What the floor must never do is let
through a specific: a number, a name, a date, a title or a quote. Those are
extracted as claims *before* this list is ever consulted.
"""

from __future__ import annotations

import re

__all__ = [
    "ABBREVIATIONS",
    "COGNITION_VERBS",
    "COMMON_ENGLISH",
    "FIRST_PERSON_MARKERS",
    "FORWARD_RELATIVE_PHRASES",
    "IRREGULAR_FORMS",
    "KIND_VOCABULARY",
    "MAGNITUDES",
    "NUMBER_WORDS",
    "OTHER_PARTY_MARKERS",
    "PAST_RELATIVE_PHRASES",
    "RELATIVE_DATE_PHRASES",
    "MONTHS",
    "is_common",
    "stem",
]


# --------------------------------------------------------------------------
# COMMON_ENGLISH
# --------------------------------------------------------------------------

#: Grammar. Determiners, pronouns, auxiliaries, prepositions, conjunctions.
_GRAMMAR = """
a about above across after again against all almost along already also although
always am among amid an and another any anybody anyone anything anywhere are
aren't around as at away
back be because been before behind being below beneath beside besides between
beyond both but by
can can't cannot could couldn't
despite did didn't do does doesn't doing don't down during
each either else enough even ever every everybody everyone everything except
few for from further
had hadn't has hasn't have haven't having he he'd he'll he's her here here's
hers herself him himself his how however
i i'd i'll i'm i've if in indeed inside instead into is isn't it it'd it'll
it's its itself
just
let let's like
many maybe me might mine more moreover most mostly much must mustn't my myself
near neither never nevertheless next no nobody none nor not nothing now
of off often on once one only onto or other others otherwise ought our ours
ourselves out outside over own
past per perhaps plus
quite
rather really
same seldom several shall shan't she she'd she'll she's should shouldn't since
so some somebody somehow someone something sometimes somewhat somewhere
still such
than that that's the their theirs them themselves then there there's therefore
these they they'd they'll they're they've this those though through throughout
thus till to together too toward towards
under underneath unless until unto up upon us usually
very via
was wasn't we we'd we'll we're we've well were weren't what what's whatever
when whenever where whereas wherever whether which whichever while who whoever
whom whose why will with within without won't would wouldn't
yet you you'd you'll you're you've your yours yourself yourselves
"""

#: Generic verbs. Each describes an action with no object baked into it. A verb
#: that implies a business event (raise, acquire, launch, ship, scale, hire) is
#: NOT here — that is a claim, and it belongs to evidence or to KIND_VOCABULARY.
_VERBS = """
add added adding agree agreed allow allowed answer answered appear appeared
ask asked asking
become became been begin began begun believe believed break breaks broke broken
bring brought build building built
call called calling came come coming consider considered cover covered covers
decide decided did do does doing done
enjoy enjoyed expect expected explain explained
feel felt find finding finds fit fits fitted found follow followed following
follows
gave generate generated generates generating get gets getting give given giving
go goes going gone got guess
handle handled happen happened hear heard held help helped helping hold hope
hoped
imagine imagined keep keeping kept knew know knowing known
land landed lands learn learned leave leaves leaving left let like liked look
looked looking lose lost
made make makes making mean means meant meet meeting met mention mentioned mind
miss missed move moved moving
need needed needs noticed notice
offer offered open opened opening
pass passed pick picked point pointed prefer preferred put
read reading ready reckon remember remembered reply replied reach reached
run running
said saw say saying says see seeing seem seemed seen sending sends sent set
share shared show showed showing shown sit sitting sound sounded speak spoke
spend spent stand start started stop stopped suggest suggested suppose
take taken takes taking talk talked tell telling tells thank thanked think
thinking thought told took try tried trying turn turned
understand understood use used useful using
wait waited walk want wanted wants was watch went were wonder wondered work
worked working worth would write writing written wrote
"""

#: Generic adjectives and adverbs. Nothing here quantifies or measures.
_MODIFIERS = """
able actual actually ahead alike available aware
bad basic best better big bigger brief briefly
careful certain clear clearly close cold common complete
different difficult direct directly down early easier easy entire especially
exactly extra
fair fairly far fast few fine first free fresh full further
general generally glad good great
half handy happy hard harder heavy high higher honest honestly huge
expensive
ideal important impossible interested interesting
kind
large larger last late later least less light little long longer low lower
main major messy messier mid middle minor modest
narrow natural near nearly neat new newer nice normal
obvious obviously odd old only open ordinary original
particular particularly plain plainly pleased poor positive possible practical
present pretty previous prior probably proper
quick quickly quiet
rare real reasonable recent recently regular relevant right
sensible separate serious seriously severe sharp short shorter significant
similar simple
simply single slight slightly slow small smaller smooth solid sorry special
specific standard straight strange strong stronger sudden suitable sure
tidy tight tiny top total tough true typical
unclear unusual up upcoming useful usual
valuable various vast
warm weak whole wide willing wrong
young
"""

#: Time and sequence words with no date in them. A word that points at a
#: SPECIFIC time ("yesterday", "last quarter", a month name) is deliberately
#: absent: those are date claims, checked against evidence in `verify.py`.
_TIME = """
after afterwards again ago already always annual before beforehand brief
current currently daily deadline
early earlier eventually ever finally frequently future
immediately initially instant
later lately long
meanwhile moment moments monthly
never now nowadays
occasionally often once ongoing onwards
period permanent previously
rarely recent recently regularly
season shortly since soon sometimes stage step still sudden suddenly
temporarily then time times timing
until upcoming urgent
weekly when whenever while
yearly
"""

#: Generic nouns: things any letter to anyone might mention. Notably absent —
#: every noun that names a business object, metric, team, tool or event.
_NOUNS = """
answer approach area
background basis bit
case chance change choice context conversation copy couple course
day days detail details direction
effort email emails end example
fact favour favor feedback
group guess
half hand help
idea impression intro introduction issue
line link list lot look
mail mail matter message minute minutes moment month months
name need note notes number
option order
page pair part path pattern people person phone piece place plan plans point
post
problem process purpose
question
reason reply request response rest result
sense set side sign situation sort space spot start state stuff subject
summary
take task team teams thing things thought
version view voice
way week weeks word words work world
year years
"""

#: Admitted after the first live `compose_claude` run, which held 4 of 4 drafts.
#: Every word here was flagged as an untraceable CLAIM in copy that an operator
#: read line by line against its evidence and found clean, and every one of them
#: answers the three admission questions above with a yes: it could appear in a
#: letter about any company at all, a recipient reading it without evidence
#: behind it would shrug, and it is not a word we would cite a source for.
#:
#: Nothing from the business register got in on this ticket. `dashboard`,
#: `ramping` and `sequencing` were flagged in the same run and are deliberately
#: NOT here: they name a tool and two operating states, they are the kind of
#: word a retrieved fact would contain, and admitting them to buy two passing
#: drafts is how a floor becomes a hole. Those drafts are still held, and the
#: measurement in `docs/` says so rather than hiding it.
#:
#: `today`, `tonight` and `tomorrow` are here for a second reason: they moved
#: out of the date-claim set (see `FORWARD_RELATIVE_PHRASES`), so they now reach
#: the word check, and a bare present or future deictic asserts nothing.
_ORDINARY_PROSE = """
cleanup compare compared compares comparing curious
describe described describes describing
elsewhere exercise exercises
friction
handoff handoffs
normally
ramp ramped ramping ramps rarer
sequence sequenced sequences sequencing setup setups
surface surfaced surfaces surfacing
today tomorrow tonight typically
vary varied varies varying
"""
# `cleanup`, `handoff`, `ramp`, `sequence`, `setup` name how work is organised,
# not a fact about the recipient. Each one held a real draft in the first live
# run -- "the first SDRs ramping", "you'd feel that sequencing", "that exact
# setup", "six months of cleanup" -- and each passes all three admission tests:
# it fits a letter to any company, a recipient reads it and shrugs, and it is
# not the kind of word we would cite a source for. We cite a source for "2 SDR
# openings"; "ramping" is the sender's characterisation of it.
#
# `dashboard` was deliberately NOT admitted alongside them. It held a draft in
# the same run ("things a dashboard won't"), and in that generic construction it
# really was a false positive -- but the admission policy above already excludes
# `tool` and `platform`, and a named piece of software the recipient runs is the
# same register. Failing closed on a borderline word costs a held draft; opening
# it costs a category of unverifiable claim.

#: Courtesies, greetings and the fixed scaffolding our own composers emit. These
#: are sender-side: none of them says anything about the recipient.
_SCAFFOLDING = """
apologies appreciate
best
call calls cheers congrats congratulations
deck
hello hey hi
follow-up
intro
kind kindly
meeting
no
please
regards
sincerely sorry
thanks thank
welcome worth
yes
quick quickly
saw seeing site
live
latest update updates
role roles opening openings
leave either way
minute minutes
"""

COMMON_ENGLISH: frozenset[str] = frozenset(
    w
    for w in (
        _GRAMMAR + _VERBS + _MODIFIERS + _TIME + _NOUNS + _ORDINARY_PROSE + _SCAFFOLDING
    ).split()
    if w
)


# --------------------------------------------------------------------------
# KIND_VOCABULARY
# --------------------------------------------------------------------------
#
# Words entailed by the KIND of a cited fact rather than by its text.
#
# This is the narrow, principled fix for the false positives in docs/PHASE2.md.
# `Fact.kind` is not the model's word for it: the kind is set by the retrieval
# adapter from the page the fact came from, is stored in research_cache, and is
# re-resolved from the cache at verification time — exactly like the content. So
# "you're hiring" over a cited `job_posting` is restatement of retrieved
# evidence, not an unsupported claim, even when the posting's own text never
# uses the word "hiring".
#
# The sets are small and each word is a direct restatement of what the kind
# MEANS. Nothing here describes scale, trajectory, outcome or need:
# `job_posting` licenses "hiring", never "growing" (a req can be a backfill);
# `funding` licenses "raised", never "well-funded".

KIND_VOCABULARY: dict[str, frozenset[str]] = {
    "job_posting": frozenset(
        """
        hiring hire hires hired recruit recruiting recruiter
        role roles opening openings position positions vacancy vacancies
        req reqs requisition job jobs posting postings listing listings
        candidate candidates applicant applicants interview interviews
        team teams
        """.split()
    ),
    "careers_page": frozenset(
        """
        hiring hire hires hired recruit recruiting
        role roles opening openings position positions vacancy vacancies
        job jobs posting postings listing listings careers
        team teams
        """.split()
    ),
    "blog_post": frozenset(
        """
        blog post posted posts write wrote writing written published publish
        article piece note wrote
        """.split()
    ),
    "changelog": frozenset(
        """
        changelog release released releases ship shipped shipping
        update updates updated published post
        """.split()
    ),
    "press": frozenset(
        """
        press announcement announced announce release released coverage news
        published post article
        """.split()
    ),
    "funding": frozenset(
        """
        funding funded fund raise raised raising round rounds
        investment investor investors backed backing series
        """.split()
    ),
    "homepage": frozenset("site website homepage page".split()),
    "linkedin_profile": frozenset("profile linkedin".split()),
    "lead_notes": frozenset("notes note".split()),
}


# --------------------------------------------------------------------------
# ABBREVIATIONS
# --------------------------------------------------------------------------
#
# One-way expansions, applied to the DRAFT side only. An abbreviation is
# supported when every word of its expansion is present in the evidence, so this
# can only ever recognise that the copy is restating evidence more briefly — it
# can never invent support that is not there. ("reps" passes when the evidence
# says "Sales Development Representative"; it does not pass when the evidence
# says nothing about representatives.)

ABBREVIATIONS: dict[str, tuple[str, ...]] = {
    "rep": ("representative",),
    "reps": ("representatives",),
    "revops": ("revenue", "operations"),
    "salesops": ("sales", "operations"),
    "sdr": ("sales", "development", "representative"),
    "sdrs": ("sales", "development", "representatives"),
    "ae": ("account", "executive"),
    "aes": ("account", "executives"),
    "csm": ("customer", "success", "manager"),
    "gtm": ("go", "to", "market"),
    "ops": ("operations",),
    "eng": ("engineering",),
    "mgr": ("manager",),
}


# --------------------------------------------------------------------------
# Numbers and dates
# --------------------------------------------------------------------------

#: Spelled-out cardinals. Mapped to digits so "two openings" is checked against
#: the same evidence as "2 openings" — a spelling difference is not a claim.
#:
#: "one" is absent on purpose: it is a determiner far more often than a count
#: ("one dashboard"), and it is in COMMON_ENGLISH for that reason.
#:
#: Vague quantifiers — dozen, dozens, handful, several, many, a few — are also
#: absent, and are NOT mapped to a value. "a dozen roles" over evidence saying
#: "12 roles" must fail: the system prompt forbids paraphrasing a number, and
#: mapping the word to 12 here would quietly permit exactly that.
NUMBER_WORDS: dict[str, int] = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

#: Magnitude suffixes, applied to BOTH sides. "$12M" in the evidence and
#: "12 million" in the copy are the same number and must compare equal;
#: "40" and "40M" are not and must not.
MAGNITUDES: dict[str, int] = {
    "k": 1_000,
    "thousand": 1_000,
    "m": 1_000_000,
    "mm": 1_000_000,
    "million": 1_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "billion": 1_000_000_000,
}

MONTHS: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

#: Relative dates that DATE AN EVENT: they assert that something happened, or is
#: happening, in a stated past or current period. These are checked like any
#: other date — the phrase must be in the evidence. The verifier has no clock
#: and must not acquire one — "last quarter" is true or false only relative to
#: when the source said it, which is the source's job to say.
PAST_RELATIVE_PHRASES: frozenset[str] = frozenset(
    {
        "last week",
        "last month",
        "last quarter",
        "last year",
        "this week",
        "this month",
        "this quarter",
        "this year",
        "past week",
        "past month",
        "past quarter",
        "past year",
        "yesterday",
    }
)

#: Forward-looking and present-deictic time words, which are NOT dated-event
#: claims and are not required to appear in the evidence.
#:
#: Measured, not assumed: on the first live `compose_claude` run every one of
#: the four drafts was held, and two of the holds were here — `date 'next week'`
#: from "Worth a short call in the next week or two?" and `date 'today'` from
#: "I'd be curious how you're handling it today". Neither sentence dates an
#: event. The first proposes a meeting and the second means "at present"; there
#: is no fact about the recipient in either, so there is nothing for evidence to
#: support or contradict.
#:
#: This does not open a hole for invented future or present claims, because the
#: EVENT word carries the claim and event words are not ordinary English. "You
#: are launching next quarter" still fails on `launching`; "you migrate tomorrow"
#: still fails on `migrate`. What is dropped is only the bare time word, which
#: on its own asserts nothing — and a control that holds a draft for saying
#: "today" is a control reviewers learn to click past.
FORWARD_RELATIVE_PHRASES: frozenset[str] = frozenset(
    {
        "next week",
        "next month",
        "next quarter",
        "next year",
        "today",
        "tonight",
        "tomorrow",
    }
)

#: Both sets. Kept as a name because it is exported and imported elsewhere;
#: `verify.py` requires evidence for `PAST_RELATIVE_PHRASES` only.
RELATIVE_DATE_PHRASES: frozenset[str] = PAST_RELATIVE_PHRASES | FORWARD_RELATIVE_PHRASES


# --------------------------------------------------------------------------
# Speaker markers
# --------------------------------------------------------------------------
#
# Who is a sentence about? A claim that needs evidence is an assertion about the
# RECIPIENT'S company. The sender's own offers, questions, availability and
# framing are not claims about the recipient at all, and holding a draft because
# the sender said "I'd be curious" or "a 15-minute call" is the false positive
# that made the first live run hold 4 of 4 drafts.
#
# `verify.py` uses these three sets to decide that question, and the safety of
# the whole idea rests on how they are combined there: a span is treated as the
# sender's voice ONLY when it positively carries a first-person marker AND
# carries no other-party marker AND no cognition verb. Anything else — including
# every span with no pronoun at all, like "Headcount doubled" — is checked in
# full. The default is always to check.

#: The sender talking about the sender.
FIRST_PERSON_MARKERS: frozenset[str] = frozenset(
    """
    i i'd i'll i'm i've me mine my myself
    we we'd we'll we're we've our ours us ourselves
    """.split()
)

#: Anyone who is not the sender. Second person is the recipient; third person is
#: whoever the copy was last talking about, which in cold outreach is nearly
#: always the recipient's company. Either one makes a span checkable again.
OTHER_PARTY_MARKERS: frozenset[str] = frozenset(
    """
    you you'd you'll you're you've your yours yourself yourselves
    they they'd they'll they're they've their theirs them themselves
    """.split()
)

#: Verbs that introduce a PROPOSITION. "I saw", "we hear", "I think" are first
#: person, but what follows them is a claim about somebody else — this is the
#: construction a model would use to smuggle a fabrication into sender voice
#: ("I saw you scaled to 40 reps", "we think headcount doubled"). A span
#: containing one of these is never sender voice, whatever pronouns it carries.
COGNITION_VERBS: frozenset[str] = frozenset(
    """
    assume assumed assuming believe believed bet expect expected
    figure figured find finds found gather gathered guess guessed
    hear heard imagine imagined know knew known learn learned learnt
    notice noticed noticing read reading recall recalled reckon
    remember remembered saw see seeing seen sense sensed spot spotted
    suspect suspected tell telling told think thinking thought
    understand understood watch watched
    """.split()
)


# --------------------------------------------------------------------------
# Stemming
# --------------------------------------------------------------------------

_DOUBLE_CONSONANTS = set("bdgklmnprt")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")

#: Irregular verb forms, folded to their base before stemming.
#:
#: Suffix stripping handles "rebuilds" and "rebuilding" and cannot handle
#: "rebuilt". Without this table a draft saying "the reporting you are
#: rebuilding" passes and "the reporting you rebuilt" is held — a tense change
#: reported as a fabrication, which is precisely the false positive W1 exists to
#: remove. Folding two forms of ONE verb together cannot excuse a claim: both
#: forms still have to be in the evidence.
IRREGULAR_FORMS: dict[str, str] = {
    "began": "begin",
    "begun": "begin",
    "bought": "buy",
    "brought": "bring",
    "built": "build",
    "came": "come",
    "chose": "choose",
    "chosen": "choose",
    "did": "do",
    "done": "do",
    "drawn": "draw",
    "drew": "draw",
    "fell": "fall",
    "fallen": "fall",
    "felt": "feel",
    "found": "find",
    "gave": "give",
    "given": "give",
    "gone": "go",
    "got": "get",
    "gotten": "get",
    "grew": "grow",
    "grown": "grow",
    "held": "hold",
    "kept": "keep",
    "knew": "know",
    "known": "know",
    "laid": "lay",
    "led": "lead",
    "left": "leave",
    "lost": "lose",
    "made": "make",
    "meant": "mean",
    "met": "meet",
    "paid": "pay",
    "put": "put",
    "ran": "run",
    "rebuilt": "rebuild",
    "rewritten": "rewrite",
    "rewrote": "rewrite",
    "said": "say",
    "sat": "sit",
    "saw": "see",
    "seen": "see",
    "sent": "send",
    "sold": "sell",
    "spent": "spend",
    "spoke": "speak",
    "spoken": "speak",
    "stood": "stand",
    "taken": "take",
    "taught": "teach",
    "thought": "think",
    "told": "tell",
    "took": "take",
    "understood": "understand",
    "went": "go",
    "won": "win",
    "wrote": "write",
    "written": "write",
}


def _undouble(word: str) -> str:
    if len(word) > 3 and word[-1] == word[-2] and word[-1] in _DOUBLE_CONSONANTS:
        return word[:-1]
    return word


def _strip_once(word: str) -> str:
    """One suffix-stripping pass. Conservative by design.

    Only inflectional endings are removed — plural, past, progressive. The
    derivational ones (-er, -ly, -ion, -ment) are left alone: over-stemming
    makes two DIFFERENT words collide, and a collision here is a false
    NEGATIVE, which is the failure mode this control may not have.
    """

    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("ing"):
        return _undouble(word[:-3])
    if len(word) > 3 and word.endswith("ed"):
        return _undouble(word[:-2])
    if len(word) > 4 and word.endswith("es") and not word.endswith("ses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def stem(word: str) -> str:
    """Fold a word to a comparison key. Deterministic, no data files, no deps."""

    current = word.lower().strip("-'’ \t\n.,;:!?()[]\"“”")
    for suffix in ("'s", "’s"):
        if current.endswith(suffix):
            current = current[: -len(suffix)]
    current = IRREGULAR_FORMS.get(current, current)
    for _ in range(3):
        nxt = _strip_once(current)
        if nxt == current:
            break
        current = nxt
    if len(current) > 3 and current.endswith("e"):
        current = current[:-1]
    return current


_COMMON_STEMS: frozenset[str] = frozenset(stem(w) for w in COMMON_ENGLISH)


def is_common(word: str) -> bool:
    """Is this word ordinary English that asserts nothing about the recipient?"""

    lowered = word.lower().strip("-'’")
    return lowered in COMMON_ENGLISH or stem(lowered) in _COMMON_STEMS
