"""The nickname, the tagline and the scouting blurb on the back of the card.

The model gets example backs in its system prompt and copies their register:
every sentence but the last carries a number or a bio fact, the last is the
verdict those numbers add up to, and nothing praises the player. The bio facts
are the height, the weight, the hometown and the position, when the stats file
gives them, and each may carry one sentence, written the way the facts write
it. After the player's facts, one line says how many sentences their facts
allow, two to four. It answers with one JSON object, and a JSON schema goes
with the request so vLLM constrains the decoding.

`check` reads the answer before it goes on a card: the word limits, a nickname
with no word of the player's name in it, a blurb that uses the numbers with a
number or a bio fact in every sentence but the verdict, no sentence that is
only numbers joined by "plus" or "and", no number the facts do not hold (the
player's own height, weight and hometown are taken out first, so the 6 of
"6 ft 1 in" is theirs), no word from `api.writing.BANNED`, `PRAISE` or
`UNSAID`, no side the player does not bat or throw from, and no name for an
angle the number is not in. An answer with problems gets one retry, whose last
message names each problem on its own line. If that fails too, `template_text`
writes the back from the stats file, so a card always renders.

There is no comps line. The "Swings like" table on the back says it, and the
model is not told who the comps are, so it has nothing to say about a real
player.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache

import httpx
from pydantic import ValidationError

from api.clients import ServiceError, post_chat
from api.writing import BANNED, EM_DASH, phrases_in
from card.render import POSITIONS
from common.schemas import METRICS, CardText, Stats

# Words, not characters. The prompt states them and check() holds the model to them.
LIMITS = {"nickname": 3, "tagline": 8, "blurb": 40}

TEXT_SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "string"} for name in LIMITS},
    "required": list(LIMITS),
    "additionalProperties": False,
}

# What a model reaches for when it praises a player it knows nothing about. The
# back that came off the node on 2026-09-22 used four of them. The prompt names
# them, and check() rejects them along with the list every page is held to.
PRAISE = (
    "impressive",
    "elite",
    "dynamic",
    "versatile",
    "well-rounded",
    "solid",
    "consistent",
    "highlight",
    "highlights",
    "demonstrate",
    "demonstrates",
    "shows great",
    "great mechanics",
    "reliable threat",
    "specific approach",
    "power production",
    "ball-to-bat",
    "making him",
    "making her",
    "making them",
)

# What the facts never say, by the five things the prompt names: a school, a
# team, a league, a year and a family. A hometown invites "played high school
# ball in Oakland", and the facts hold no school, so a back that names one made
# it up. check() rejects these as it does PRAISE. A word in the player's own
# facts, "College Park, MD", is theirs to keep.
UNSAID = (
    *("school", "schools", "college", "colleges", "university", "varsity", "scholarship"),
    *("team", "teams", "roster", "league", "leagues"),
    *("year", "years", "season", "seasons"),
    *("family", "families", "parents", "father", "mother", "dad", "mom"),
    *("brother", "brothers", "sister", "sisters", "son", "daughter", "wife", "husband"),
)

# The words for a side. A back that said "lefty" for a player who bats right
# came off the node on 2026-09-23. check() holds every field to the facts.
# Each spelling is listed: on 2026-09-24 Gemma named a right-hander "The Leftie".
LEFT = (
    "lefty",
    "leftie",
    "lefties",
    "left-handed",
    "left handed",
    "lefthanded",
    "left-hander",
    "left hander",
    "lefthander",
    "left-handers",
    "left handers",
    "lefthanders",
    "southpaw",
    "southpaws",
    "from the left side",
)
RIGHT = (
    "righty",
    "rightie",
    "righties",
    "right-handed",
    "right handed",
    "righthanded",
    "right-hander",
    "right hander",
    "righthander",
    "right-handers",
    "right handers",
    "righthanders",
    "from the right side",
)
SIDE_WORDS = {"L": LEFT, "R": RIGHT}

# The names angles go by, and the numbers each name covers, in degrees, the way
# the prompt says them. "Under 10" does not take 10 and "over 50" does not take
# 50, so 10 is a line drive and 50 a fly ball. "10 to 25" and "25 to 50" take
# both ends, so 25 is both and either name is right. Every name a band goes by
# is listed, plurals too, so check() can hold the answer to the number.
Band = tuple[str, float | None, float | None, tuple[str, ...]]
LAUNCH_BANDS: tuple[Band, ...] = (
    (
        "a ground ball",
        None,
        10,
        (
            "ground ball",
            "ground balls",
            "ground-ball",
            "groundball",
            "groundballs",
            "grounder",
            "grounders",
        ),
    ),
    ("a line drive", 10, 25, ("line drive", "line drives", "line-drive", "liner", "liners")),
    ("a fly ball", 25, 50, ("fly ball", "fly balls", "fly-ball", "flyball", "flyballs")),
    ("a pop up", 50, None, ("pop up", "pop ups", "pop-up", "pop-ups", "popup", "popups")),
)
ATTACK_BANDS: tuple[Band, ...] = (
    ("flat", None, 5, ("flat",)),
    ("level to slightly up", 5, 15, ("level", "slightly up")),
    ("a steep uppercut", 15, None, ("steep", "uppercut", "uppercuts")),
)
ANGLES = (
    ("launch angle", "launch_angle_deg", LAUNCH_BANDS),
    ("attack angle", "attack_angle_deg", ATTACK_BANDS),
)

# The backs in the system prompt. They teach the register better than any rule
# does. All three pass check(), and a test says so. Dana's launch angle is 28,
# a fly ball by the bands above: an example that called a line drive a fly ball
# would teach the model to. The third is a card whose height, weight and
# hometown were typed: one number, and a sentence for bio facts written the way
# the facts write them, with the first name and no he or she.
EXAMPLES: tuple[tuple[Stats, CardText], ...] = (
    (
        Stats(
            name="Sam Rivera",
            position="SS",
            bats="R",
            throws="R",
            hometown="Chicago, IL",
            metrics={
                "bat_speed_mph": 68.2,
                "exit_velo_mph": 88.5,
                "launch_angle_deg": 14.0,
                "swing_length_ft": 7.1,
                "attack_angle_deg": 9.0,
                "time_to_contact_ms": 155,
            },
        ),
        CardText(
            nickname="Short Fuse",
            tagline="Quick to the ball, quicker to first",
            blurb=(
                "Sam gets the bat to the ball in 155 ms, a short swing at 7.1 ft. "
                "The 88.5 mph exit velocity will not clear many fences. "
                "The 14 degree launch angle sends it over the infield. Doubles, not homers."
            ),
        ),
    ),
    (
        Stats(
            name="Dana Okafor",
            position="C",
            bats="L",
            metrics={
                "bat_speed_mph": 74.9,
                "exit_velo_mph": 96.3,
                "launch_angle_deg": 28.0,
                "attack_angle_deg": 15.0,
            },
        ),
        CardText(
            nickname="The Crane",
            tagline="Swings hard and aims for the second deck",
            blurb=(
                "Dana swings at 74.9 mph and the ball leaves at 96.3. "
                "A 28 degree launch angle and a 15 degree attack angle mean fly balls, "
                "and at that speed many land in the seats. Strikeouts come with the package."
            ),
        ),
    ),
    (
        Stats(
            name="Leo Grant",
            position="CF",
            bats="L",
            height_in=69,
            weight_lb=182,
            hometown="Fresno, CA",
            metrics={"exit_velo_mph": 93.4},
        ),
        CardText(
            nickname="The Sledge",
            tagline="Barrel first, questions later",
            blurb=(
                "The ball leaves Leo's bat at 93.4 mph. Leo stands 5 ft 9 in and weighs "
                "182 lb. Fresno, CA is home. Hits it hard."
            ),
        ),
    ),
)

# A model that ignores response_format usually still fences its JSON.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass
class TextResult:
    """What came back, and whether it came from the model or from the template."""

    text: CardText
    meta: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def from_model(self) -> bool:
        return self.error is None


# -- the prompt --------------------------------------------------------------


def _written(key: str, value: float) -> str:
    """A metric's value the way the facts write it, with the card's decimals."""
    return f"{value:.{METRICS[key].decimals}f}"


def _metric_line(key: str, value: float) -> str:
    metric = METRICS[key]
    return f"{metric.label}: {_written(key, value)} {metric.unit}"


def _metric_lines(stats: Stats) -> list[str]:
    return [_metric_line(key, value) for key, value in stats.metrics.items()]


def height_phrase(inches: int) -> str:
    """A height the way the facts write it, one way every time: 73 inches is
    6 ft 1 in, and 72 is 6 ft 0 in."""
    return f"{inches // 12} ft {inches % 12} in"


def weight_phrase(pounds: int) -> str:
    """A weight the way the facts write it: 190 lb."""
    return f"{pounds} lb"


def facts(stats: Stats) -> str:
    """Everything the model is told about the player, one fact a line, in the
    order of the bio row on the back, then the numbers."""
    lines = [f"Name: {stats.name}"]
    for label, value in (
        ("Position", stats.position),
        ("Bats", stats.bats),
        ("Throws", stats.throws),
        ("Height", height_phrase(stats.height_in) if stats.height_in else None),
        ("Weight", weight_phrase(stats.weight_lb) if stats.weight_lb else None),
        ("Hometown", stats.hometown),
    ):
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines + _metric_lines(stats))


def bio_facts(stats: Stats) -> list[str]:
    """The bio facts the stats file gave, by their label in the facts. Each may
    carry one sentence of the blurb. Bats and Throws carry none: a side is a
    fact the back may not get wrong, not one it writes a sentence about."""
    given = {
        "Position": stats.position,
        "Height": stats.height_in,
        "Weight": stats.weight_lb,
        "Hometown": stats.hometown,
    }
    return [label for label, value in given.items() if value]


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def blurb_plan(stats: Stats) -> str:
    """How many sentences the blurb may have, from how many facts may carry one.

    Every sentence but the verdict carries a number or a bio fact, so one
    number and no bio makes two sentences at most, and one number with a height
    and a weight makes four. Nothing is made up to fill the space. Told only
    "four short sentences or fewer", Gemma wrote four for one number on the node
    on 2026-09-24, and filled two of them with "He plays left field" and "He is
    a righty". With no numbers at all it wrote "A reliable outfielder." about a
    player it knew nothing about.
    """
    numbers = len(stats.metrics)
    if numbers == 0:
        return (
            "These facts hold no swing numbers, so the blurb is at most 2 sentences, "
            "who the player is and then that no swing numbers came with this card."
        )
    bio = len(bio_facts(stats))
    count = numbers + bio
    carried = min(count, 3)
    held = _counted(numbers, "number") + (f" and {_counted(bio, 'bio fact')}" if bio else "")
    if count == 1:
        carry = "1 sentence that carries it"
    else:
        carry = f"at most {carried} sentences that carry them"
    plan = (
        f"These facts hold {held}, so the blurb is at most {carried + 1} short "
        f"sentences, {carry} and then the verdict."
    )
    # With six numbers Gemma packed all six into three sentences and ran past
    # 40 words in about 1 card in 7, and each of those went to the template.
    if numbers > carried:
        plan += f" Use the numbers that say the most. It need not use all {numbers}."
    # A bio fact may take a sentence a number needed. check() wants the number,
    # or two of them, so the plan says so before the model spends the space.
    if bio:
        must = "the number" if numbers == 1 else "at least 2 of the numbers"
        plan += f" It must use {must}."
    return plan


def _user(stats: Stats) -> str:
    """The user message: the facts, then how long the blurb may be."""
    return f"Facts:\n{facts(stats)}\n\n{blurb_plan(stats)}"


def _example(stats: Stats, text: CardText) -> str:
    card = json.dumps({name: getattr(text, name) for name in LIMITS})
    return f"{_user(stats)}\n\nCard:\n{card}"


SYSTEM = "\n".join(
    [
        "You write the back of a baseball card for an amateur player. Write it the way "
        "the backs of old cards read: short sentences built on the numbers, and no praise.",
        "",
        f"nickname: 1 to {LIMITS['nickname']} words teammates would shout. "
        "Never a word from the player's name. No quote marks.",
        f"tagline: at most {LIMITS['tagline']} words. One line under the nickname "
        "about how this player hits.",
        f"blurb: at most {LIMITS['blurb']} words in short sentences, and before the "
        "verdict at most one sentence for each number or bio fact in the facts, so four "
        "sentences in all at most. It need not use every fact. "
        "Every sentence but the last carries a number or a bio fact from the facts and "
        "could only be about this player. The last sentence is the verdict those numbers "
        "add up to, 5 words or fewer.",
        "",
        "Use only the numbers in the facts. Never round one, never invent one. "
        "Never name a major league player.",
        f"Never write these words: {', '.join(PRAISE)}.",
        "",
        "Height, Weight, Hometown and Position are bio facts. Each may carry one sentence, "
        "written the way the facts write it, and nothing beyond it: no school, team, "
        "league, year, family, or anything else the facts do not say. The facts do not "
        "say whether the player is a man or a woman, so use the first name, never he or "
        "she.",
        "",
        "Bats and Throws say which side the player uses: R is right-handed, L is "
        "left-handed, S is a switch hitter. Never give a side the facts do not.",
        "",
        "Angles have names, and a name is right only when the number is in its band. "
        "Launch angle: under 10 degrees is a ground ball, 10 to 25 a line drive, "
        "25 to 50 a fly ball, over 50 a pop up. Attack angle: under 5 degrees is flat, "
        "5 to 15 level to slightly up, over 15 a steep uppercut.",
        "",
        "Three examples.",
        "",
        *[part for example in EXAMPLES for part in (_example(*example), "")],
        "Answer with one JSON object with the keys nickname, tagline and blurb, and nothing else.",
    ]
)

# The first line of the retry. Each problem check() found follows on its own line.
RETRY = "That card has problems. Fix each one and write the whole card again."


def build_messages(stats: Stats) -> list[dict[str, str]]:
    """The two messages sent to the model. Everything it knows is in here."""
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": _user(stats)},
    ]


def retry_messages(stats: Stats, answer: str, problems: list[str]) -> list[dict[str, str]]:
    """The second try: the first answer, then one line for each thing wrong with it."""
    return [
        *build_messages(stats),
        {"role": "assistant", "content": answer},
        {"role": "user", "content": "\n".join([RETRY, *problems])},
    ]


def chat_payload(model: str, messages: list[dict[str, str]]) -> dict:
    """The request body. response_format makes vLLM decode against the schema."""
    return {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 400,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "card_text", "schema": TEXT_SCHEMA, "strict": True},
        },
    }


# -- the limits --------------------------------------------------------------

# Cutting at a word boundary can still end on "a" or "of". Those come off too.
DANGLING = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "as",
        "is",
        "are",
        "was",
        "were",
        "that",
        "this",
    }
)


def clamp_words(value: str, limit: int) -> str:
    """First `limit` words, whitespace collapsed, no stray quotes, no dangling article."""
    cleaned = " ".join(value.replace("\n", " ").split()).strip(" \"'“”")
    words = cleaned.split(" ")
    if len(words) <= limit:
        return cleaned
    words = words[:limit]
    while words and words[-1].strip(",;:.-").lower() in DANGLING:
        words.pop()
    return " ".join(words).rstrip(",;:-")


def clamp(text: CardText) -> CardText:
    """Apply every word limit, and tidy the whitespace. The template needs the cut."""
    return CardText(**{name: clamp_words(getattr(text, name), LIMITS[name]) for name in LIMITS})


def parse(content: str) -> CardText:
    """Turn the model's message into a CardText, or raise ValueError.

    Nothing is cut here. check() reads the answer the model wrote, all of it.
    """
    candidate = content.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON was not an object")
    try:
        return CardText.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"JSON did not match the schema: {exc.error_count()} problems") from exc


# -- the checks --------------------------------------------------------------

# A letter in any alphabet: \w without the digits and the underscore.
_LETTER = r"[^\W\d_]"
_NAME_WORD = re.compile(rf"{_LETTER}+(?:'{_LETTER}+)*")
# A number standing on its own: 88.5 in "at 88.5." but not in "188.5" or "88.53".
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?!\w|\.\d)")
# Where a sentence ends: a stop and then a space, so "7.1 ft" is not an end.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_DOUBLE_QUOTES = frozenset('"“”„«»')
_SINGLE_QUOTES = frozenset("'‘’")

# A sentence that is only numbers and a word joining them says nothing the
# table beside it does not. "14.0 degrees plus 9.0 degrees." came off the node
# on a card back on 2026-10-29. A sentence is one of those when it holds two
# numbers and one of these words, and nothing is left once the numbers, the
# words below and the player's own name are taken out. The words below are
# never verbs, so a sentence with a verb always has a word left.
JOINERS = frozenset({"plus", "and", "+", "&"})
LISTED = frozenset(
    {
        # units
        *("mph", "ft", "feet", "foot", "ms", "millisecond", "milliseconds"),
        *("deg", "degree", "degrees", "mile", "miles", "per", "hour", "percent"),
        # the labels on the facts' metric lines
        *("bat", "speed", "exit", "velocity", "velo", "launch", "angle", "angles"),
        *("swing", "length", "attack", "time", "to", "contact"),
        # the bio facts': "6 ft 1 in and 190 lb." is numbers in a row too
        *("lb", "lbs", "pound", "pounds", "inch", "inches", "height", "weight", "tall"),
        # the small words that go with them
        *("a", "an", "the", "of", "at", "in", "on", "with", "for", "from", "by"),
        *("off", "up", "his", "her", "their", "its", "this", "that", "both", "each"),
    }
)
_TOKEN = re.compile(rf"{_LETTER}+(?:'{_LETTER}+)*|[+&]")


def _fold(value: str) -> str:
    """How two words are compared: any case, straight apostrophes, no accents."""
    value = unicodedata.normalize("NFKD", value.replace("’", "'").replace("‘", "'"))
    return "".join(ch for ch in value if not unicodedata.combining(ch)).casefold()


def name_words(name: str) -> list[str]:
    """The words of a name that a nickname may not use, three letters or longer.

    "Sam Rivera" gives sam and rivera. "Rory O'Hara" gives rory, o'hara and
    hara.
    """
    words: list[str] = []
    for word in _NAME_WORD.findall(_fold(name)):
        for part in (word, *word.split("'")):
            if sum(ch.isalpha() for ch in part) >= 3 and part not in words:
                words.append(part)
    return words


def _uses_word(text: str, word: str) -> bool:
    return re.search(rf"(?<!{_LETTER}){re.escape(word)}(?!{_LETTER})", _fold(text)) is not None


def _has_quote_marks(value: str) -> bool:
    """Double quotes anywhere, or a single one in front. "Rippin'" keeps its apostrophe."""
    value = value.strip()
    return any(ch in _DOUBLE_QUOTES for ch in value) or value[:1] in _SINGLE_QUOTES


def _forms(key: str, value: float) -> set[str]:
    """How a metric may be written in the blurb and count: as the facts write
    it, or without its trailing zeros, so 14.0 is also 14."""
    written = _written(key, value)
    return {written, written.rstrip("0").rstrip(".")} if "." in written else {written}


# A bio fact found in a field is taken out and this put in its place before the
# numbers are read, so the 6 and the 1 of "6 ft 1 in" are the player's height
# and not numbers the model made up. A sentence that holds one carries a fact.
MARK = "\x00"
# Stands in for a stop inside one of the player's facts while the blurb is cut
# into sentences.
_HELD_STOP = "․"  # one dot leader: not a stop to the sentence splitter


def bio_phrases(stats: Stats) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """How the blurb may write each bio fact, as (phrases, codes).

    Phrases match in any case, and only whole: the height and the weight as the
    facts write them, and 6 ft for 6 ft 0 in, as 14 is 14.0; the hometown, and
    its town before the comma; the position's name, Shortstop for SS, which the
    front of the card prints. Codes match only as written, since OF and IF are
    words too. Nothing else counts. "190 lbs" and "6 feet" are written another
    way, and the retry asks for the way the facts write them.
    """
    phrases: list[str] = []
    if stats.height_in:
        phrases.append(height_phrase(stats.height_in))
        short = f"{stats.height_in // 12} ft"
        # A 6.0 ft swing is written "6 ft" too. Then "6 ft" is the swing's.
        swings = {
            f"{form} ft"
            for key, value in stats.metrics.items()
            if METRICS[key].unit == "ft"
            for form in _forms(key, value)
        }
        if stats.height_in % 12 == 0 and short not in swings:
            phrases.append(short)
    if stats.weight_lb:
        phrases.append(weight_phrase(stats.weight_lb))
    if stats.hometown:
        phrases.append(stats.hometown)
        town = stats.hometown.split(",")[0].strip()
        if town and town != stats.hometown:
            phrases.append(town)
    codes: tuple[str, ...] = ()
    if stats.position:
        codes = (stats.position,)
        if stats.position in POSITIONS:
            phrases.append(POSITIONS[stats.position])
    return tuple(phrases), codes


@cache
def _phrase(phrase: str, cased: bool = False) -> re.Pattern[str]:
    """A whole phrase, its words parted by any space, and a curly apostrophe
    for a straight one: "6 ft 1 in" in "at 6 ft 1 in." but not in "16 ft 1 in"."""
    words = [re.escape(word).replace("'", "['’]") for word in phrase.split()]
    flags = 0 if cased else re.IGNORECASE
    return re.compile(r"(?<![\w.])" + r"\s+".join(words) + r"(?!\w)", flags)


def _bare(value: str, phrases: Sequence[str], codes: Sequence[str]) -> str:
    """`value` with each bio fact it states taken out, the longest first, so
    "6 ft 0 in" goes whole before "6 ft" can take a part of it."""
    for phrase in sorted(phrases, key=len, reverse=True):
        value = _phrase(phrase).sub(MARK, value)
    for code in codes:
        value = _phrase(code, cased=True).sub(MARK, value)
    return value


def _sentences(blurb: str, phrases: Sequence[str]) -> list[str]:
    """The blurb cut into sentences where a stop and a space end one.

    A stop inside one of the player's facts ends nothing: "St. Louis, MO" is
    one town. A fact that ends on a stop, "Washington, D.C.", ends the sentence
    as well when a capital or the end of the blurb comes after it.
    """
    text = blurb.strip()

    def hold(match: re.Match[str]) -> str:
        found = match.group(0)
        after = match.string[match.end() :]
        following = re.match(r"\s+(\S)", after)
        ends = found.endswith(".") and (
            not after.strip() or (following is not None and following.group(1).isupper())
        )
        kept = found[:-1] if ends else found
        return kept.replace(".", _HELD_STOP) + ("." if ends else "")

    for phrase in phrases:
        if "." in phrase:
            text = _phrase(phrase).sub(hold, text)
    return [part.replace(_HELD_STOP, ".") for part in _SENTENCE_END.split(text) if part]


def _sides(stats: Stats) -> dict[str, list[str]]:
    """Each side the facts rule out, "L" or "R", and the facts that rule it out.

    A right-handed batter is no lefty, a left-handed one no righty, and a switch
    hitter is neither. The same for the throwing arm. Nothing is ruled out by a
    fact the stats file left out.
    """
    batting = {"R": "bats right-handed", "L": "bats left-handed", "S": "is a switch hitter"}
    throwing = {"R": "throws right-handed", "L": "throws left-handed"}
    ruled_out: dict[str, list[str]] = {}
    for side, said in ((stats.bats, batting), (stats.throws, throwing)):
        if side is None:
            continue
        for other in ("L", "R"):
            if side != other:
                ruled_out.setdefault(other, []).append(said[side])
    return ruled_out


def _in_band(value: float, low: float | None, high: float | None) -> bool:
    if low is None:
        return value < high
    if high is None:
        return value > low
    return low <= value <= high


def _band_of(value: float, bands: tuple[Band, ...]) -> str:
    """The name the value goes by. The first band that holds it, so 25 is a line drive."""
    return next(name for name, low, high, _ in bands if _in_band(value, low, high))


def joined_numbers(sentence: str, name: str) -> str | None:
    """The word that joins the numbers when the sentence is only numbers joined
    by it, as "plus" is in "14.0 degrees plus 9.0 degrees.". Else None."""
    if len(_NUMBER.findall(sentence)) < 2:
        return None
    words = _TOKEN.findall(_fold(_NUMBER.sub(" ", sentence)))
    joiners = [word for word in words if word in JOINERS]
    own = set(_NAME_WORD.findall(_fold(name)))
    own |= {part for word in own for part in word.split("'")}
    for word in words:
        word = word.removesuffix("'s")
        if word not in JOINERS and word not in LISTED and word not in own:
            return None
    return joiners[0] if joiners else None


def check(text: CardText, stats: Stats) -> list[str]:
    """What is wrong with a card back, one problem to a line. Empty means use it.

    The lines go back to the model on the retry, so each says what to fix. They
    also reach the ledger and the api's log, so none quotes the player's name,
    numbers or bio.
    """
    problems = []
    for name, limit in LIMITS.items():
        count = len(getattr(text, name).split())
        if count == 0:
            problems.append(f"The {name} is empty.")
        elif count > limit:
            # A model cannot count its own words. It can leave something out. Told
            # "Drop a sentence." on the node, Gemma sent the same 42 words back,
            # because every sentence carried numbers it thought it had to keep.
            cut = " Leave out a number and the words that carry it." if name == "blurb" else ""
            problems.append(f"The {name} is {count} words. Use {limit} or fewer.{cut}")

    if any(_uses_word(text.nickname, word) for word in name_words(stats.name)):
        problems.append("The nickname uses a word from the player's name. Use none of it.")
    if _has_quote_marks(text.nickname):
        problems.append("The nickname has quote marks. Leave them off. The card adds its own.")

    # Each field with the player's bio facts taken out. The rules about numbers
    # read these, so the 6 and the 1 of "6 ft 1 in" are not numbers, and so do
    # the rules about sides and angles, so "Flat Rock, MI" is a town and not a
    # flat swing.
    phrases, codes = bio_phrases(stats)
    bare = {name: _bare(getattr(text, name), phrases, codes) for name in LIMITS}

    needed = min(2, len(stats.metrics))
    numbers = set(_NUMBER.findall(bare["blurb"]))
    used = sum(1 for key, value in stats.metrics.items() if _forms(key, value) & numbers)
    # Every way the facts' numbers may be written. Nothing else is a number the
    # model was given.
    known = set().union(*(_forms(key, value) for key, value in stats.metrics.items()))
    if used < needed:
        problems.append(
            f"The blurb uses {used} of the numbers in the facts. "
            f"Use at least {needed}, written the way the facts write them."
        )

    # The register the prompt asks for: every sentence but the last carries a
    # number or a bio fact from the facts, and the last is the verdict. On the
    # node on 2026-09-24 Gemma wrote "These numbers build a power profile." as a
    # third sentence, and asked to give such a sentence a number it made one up:
    # "These numbers total 165.0." So only a number or a bio fact from the facts
    # counts, and the fix asked for is the cut. Only asked of a player who sent
    # numbers. The line names the sentence by its place, because a problem never
    # quotes the player's words.
    sentences = _sentences(text.blurb, phrases)
    if stats.metrics:
        for place, sentence in enumerate(sentences[:-1], start=1):
            plain = _bare(sentence, phrases, codes)
            if MARK not in plain and not known & set(_NUMBER.findall(plain)):
                problems.append(
                    f"Sentence {place} of the blurb has no number or bio fact from the "
                    "facts. Cut it."
                )

    # "14.0 degrees plus 9.0 degrees." on a card: numbers in a row with no verb.
    # The fix asked for is a verb that says what they do, or the cut.
    for place, sentence in enumerate(sentences, start=1):
        joiner = joined_numbers(sentence, stats.name)
        if joiner:
            problems.append(
                f'Sentence {place} of the blurb is only numbers joined by "{joiner}". '
                "Say what the numbers do, with a verb, or cut it."
            )

    # "Never round one, never invent one." On 2026-09-24 a verdict on the node
    # read "142.7 total power.", and a blurb "A 165.0 total output.": the facts'
    # numbers, added up. A number the facts do not hold is made up, and so is a
    # bare 190 or "6 pitches": the height and the weight count only as the
    # facts write them. The line does not quote the number.
    for name in LIMITS:
        if set(_NUMBER.findall(bare[name])) - known:
            problems.append(
                f"The {name} has a number that is not in the facts. Use only the numbers "
                "in the facts, written the way the facts write them."
            )

    # A word the player's own facts carry, a hometown or a name, is theirs to keep.
    theirs = set(phrases_in(facts(stats), BANNED + PRAISE + UNSAID))
    sides = _sides(stats)
    for name in LIMITS:
        value = getattr(text, name)
        for phrase in phrases_in(value, BANNED + PRAISE):
            if phrase not in theirs:
                problems.append(f'Cut "{phrase}" from the {name}.')
        for phrase in phrases_in(value, UNSAID):
            if phrase not in theirs:
                problems.append(f'Cut "{phrase}" from the {name}. The facts do not say it.')
        if EM_DASH in value:
            problems.append(f"The {name} has a long dash. Use a period or a comma.")
        for side, said in sides.items():
            for phrase in phrases_in(bare[name], SIDE_WORDS[side]):
                problems.append(f'Cut "{phrase}" from the {name}. The player {" and ".join(said)}.')
        for label, key, bands in ANGLES:
            if key not in stats.metrics:
                continue
            number = stats.metrics[key]
            for band, low, high, names in bands:
                if _in_band(number, low, high):
                    continue
                for phrase in phrases_in(bare[name], names):
                    problems.append(
                        f'The {name} says "{phrase}". The {label} in the facts is '
                        f"{_band_of(number, bands)}, not {band}."
                    )
    return problems


# -- the template ------------------------------------------------------------


def template_nickname(metrics: dict[str, float]) -> str:
    """The template's nickname, read off the numbers. The first rule that holds wins.

    "Line Drive" names a band, so a launch angle under 10, which is a ground
    ball, gets a ground ball's name instead.
    """
    if metrics.get("time_to_contact_ms", math.inf) <= 150:
        return "Quick Hands"
    if metrics.get("exit_velo_mph", -math.inf) >= 95:
        return "Big Bat"
    if metrics.get("launch_angle_deg", -math.inf) >= 20:
        return "Sky Ball"
    if metrics.get("launch_angle_deg", math.inf) < 10:
        return "Worm Burner"
    return "Line Drive" if metrics else "The Regular"


def _plain(key: str, value: float) -> str:
    """A number the way a person writes it: 14.0 is 14, 88.5 stays 88.5."""
    return _written(key, value).removesuffix(".0")


def _sentence(words: str) -> str:
    return words[:1].upper() + words[1:] + "."


def _plays(stats: Stats) -> str:
    """Who the player is, from whatever the stats file gave."""
    if stats.position:
        return f"{stats.name} plays {stats.position}."
    return f"{stats.name} plays ball."


def _listed(parts: Sequence[str]) -> str:
    """Parts as a person lists them: "a", "a and b", "a, b and c"."""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def _plain_height(inches: int) -> str:
    """A height the way a person writes it: 6 ft for 72 inches, 6 ft 1 in for 73."""
    return f"{inches // 12} ft" if inches % 12 == 0 else height_phrase(inches)


def bio_sentence(stats: Stats, room: int) -> str | None:
    """One plain sentence of what the stats file says about the player besides
    the numbers, in `room` words or fewer: as much of the height, the weight and
    the hometown as fits, in that order. None when there is none, or no room.
    The position is the template's tagline already."""
    first = stats.name.split()[0]
    parts = []
    if stats.height_in:
        parts.append(f"stands {_plain_height(stats.height_in)}")
    if stats.weight_lb:
        parts.append(f"weighs {weight_phrase(stats.weight_lb)}")
    if stats.hometown:
        parts.append(f"is from {stats.hometown}")
    while parts:
        sentence = f"{first} {_listed(parts)}"
        # "Washington, D.C." brings its own stop.
        sentence += "" if sentence.endswith((".", "!", "?")) else "."
        if len(sentence.split()) <= room:
            return sentence
        parts.pop()
    return None


def template_blurb(stats: Stats) -> str:
    """One plain sentence per measurement, in the order the card lists them,
    then one of the bio when there is one and it fits the word limit. A card
    with no numbers says so."""
    if not stats.metrics:
        return f"{_plays(stats)} No swing numbers came with this card."

    first = stats.name.split()[0]
    number = {key: _plain(key, value) for key, value in stats.metrics.items()}
    sentences = []
    if "bat_speed_mph" in number:
        sentences.append(f"{first} swings at {number['bat_speed_mph']} mph.")
    if "exit_velo_mph" in number:
        sentences.append(f"The ball leaves at {number['exit_velo_mph']} mph.")

    pairs = (("launch angle", "launch_angle_deg"), ("attack angle", "attack_angle_deg"))
    angles = [
        f"{label} {number[key]} degree{'' if number[key] in ('1', '-1') else 's'}"
        for label, key in pairs
        if key in number
    ]
    if angles:
        sentences.append(_sentence(", ".join(angles)))

    length, contact = number.get("swing_length_ft"), number.get("time_to_contact_ms")
    if length and contact:
        sentences.append(f"A {length} ft swing, {contact} ms to contact.")
    elif length:
        sentences.append(f"A {length} ft swing.")
    elif contact:
        sentences.append(f"{contact} ms to contact.")

    # So a one-number card's template is not one line.
    bio = bio_sentence(stats, LIMITS["blurb"] - sum(len(each.split()) for each in sentences))
    if bio:
        sentences.append(bio)
    return " ".join(sentences)


def template_text(stats: Stats) -> CardText:
    """The floor. Built from the stats file, nothing invented, not run through check()."""
    return clamp(
        CardText(
            nickname=template_nickname(stats.metrics),
            tagline=stats.position or "Player",
            blurb=template_blurb(stats),
        )
    )


# -- the call ----------------------------------------------------------------


async def generate(
    client: httpx.AsyncClient,
    base: str,
    key: str,
    model: str,
    stats: Stats,
    *,
    attempts: int = 2,
) -> TextResult:
    """Ask the model, check the answer, retry once, fall back to the template.

    This never raises. A card with template text is better than no card.
    `meta["tries"]` keeps each answer and what was wrong with it, in order.
    """
    messages = build_messages(stats)
    meta: dict = {}
    tries: list[dict] = []
    error = "the llm service is not configured"

    for _ in range(attempts):
        try:
            body, meta = await post_chat(client, base, key, chat_payload(model, messages))
            answer = body["choices"][0]["message"]["content"] or ""
        except (ServiceError, httpx.HTTPError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            tries.append({"answer": None, "problems": [error]})
            continue
        except (KeyError, IndexError, TypeError):
            error = "the llm reply had no message content"
            tries.append({"answer": None, "problems": [error]})
            continue

        try:
            text = parse(answer)
        except ValueError as exc:
            problems = [str(exc)]
            error = problems[0]
        else:
            problems = check(text, stats)
            if not problems:
                tries.append({"answer": answer, "problems": []})
                return TextResult(text=clamp(text), meta={**meta, "tries": tries})
            error = "the text failed its checks. " + " ".join(problems)

        tries.append({"answer": answer, "problems": problems})
        # An answer to correct goes back with its problems. An empty one is asked again.
        if answer.strip():
            messages = retry_messages(stats, answer, problems)

    return TextResult(text=template_text(stats), meta={**meta, "tries": tries}, error=error)


__all__ = [
    "ATTACK_BANDS",
    "EXAMPLES",
    "JOINERS",
    "LAUNCH_BANDS",
    "LEFT",
    "LIMITS",
    "PRAISE",
    "RIGHT",
    "SYSTEM",
    "TEXT_SCHEMA",
    "UNSAID",
    "TextResult",
    "bio_facts",
    "bio_phrases",
    "bio_sentence",
    "blurb_plan",
    "build_messages",
    "chat_payload",
    "check",
    "clamp",
    "clamp_words",
    "facts",
    "generate",
    "height_phrase",
    "joined_numbers",
    "name_words",
    "parse",
    "retry_messages",
    "template_blurb",
    "template_nickname",
    "template_text",
    "weight_phrase",
]
