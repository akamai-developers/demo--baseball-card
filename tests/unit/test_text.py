"""The card text: the prompt, the parser, the checks, the retry and the template.

The model is the one part of this pipeline that can hand back anything at all.
These are the shapes it hands back when it misbehaves, and the one back that
came off the node on 2026-09-22 and read like a model wrote it.
"""

from __future__ import annotations

import hashlib
import json
import re

import httpx
import pytest

from api.clients import ServiceError
from api.text import (
    EXAMPLES,
    LIMITS,
    PRAISE,
    RETRY,
    SYSTEM,
    TEXT_SCHEMA,
    UNSAID,
    bio_facts,
    bio_phrases,
    bio_sentence,
    blurb_plan,
    build_messages,
    chat_payload,
    check,
    clamp,
    clamp_words,
    facts,
    generate,
    height_phrase,
    joined_numbers,
    name_words,
    parse,
    retry_messages,
    template_nickname,
    template_text,
    weight_phrase,
)
from api.writing import BANNED, EM_DASH
from card.render import FIXTURE_TEXT, fixture_stats
from common.schemas import CardText, Stats

KEY = "t" * 64

STATS = Stats(
    name="Sam Rivera",
    position="SS",
    bats="R",
    hometown="Chicago, IL",
    metrics={"bat_speed_mph": 68.2, "exit_velo_mph": 88.5},
)

# Passes check() for STATS.
GOOD = {
    "nickname": "Turnstile",
    "tagline": "Turns on a fastball like a switch",
    "blurb": "The bat moves at 68.2 mph and the ball leaves at 88.5. Doubles, not homers.",
}

# What Gemma 4 wrote on the node on 2026-09-22 under the old prompt, word for word
# but for the player's name.
NODE_PLAYER = Stats(
    name="Ka'Ni Quillfeather",
    position="DH",
    hometown="Baltimore, MD",
    metrics={"swing_length_ft": 7.6, "launch_angle_deg": 16.0, "exit_velo_mph": 92.1},
)
NODE_BACK = CardText(
    nickname="Quillfeather",
    tagline="The Powerhouse from Baltimore",
    blurb=(
        "Ka'Ni shows great mechanics with a 7.6 ft swing length. He maintains a steady "
        "16.0 degree launch angle and a 92.1 mph exit velocity, making him a reliable "
        "threat from the designated hitter spot."
    ),
)


def back(**fields: str) -> CardText:
    """GOOD with some fields swapped out."""
    return CardText(**{**GOOD, **fields})


def transport(*responses: httpx.Response, sent: list | None = None) -> httpx.MockTransport:
    """Answer each call with the next response, repeating the last one."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(json.loads(request.content))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.MockTransport(handler)


def completion(content: str, model: str = "mock/wordlist") -> httpx.Response:
    body = {"model": model, "choices": [{"message": {"role": "assistant", "content": content}}]}
    return httpx.Response(200, json=body)


async def run(*responses: httpx.Response, sent: list | None = None, stats: Stats = STATS):
    async with httpx.AsyncClient(transport=transport(*responses, sent=sent)) as client:
        return await generate(client, "http://llm", KEY, "a-model", stats)


# -- the prompt --------------------------------------------------------------


def test_the_prompt_carries_the_facts_it_was_given() -> None:
    user = build_messages(STATS)[1]["content"]
    assert user.startswith("Facts:\n")
    assert "Sam Rivera" in user
    assert "Chicago, IL" in user
    assert "68.2" in user


def test_the_facts_are_in_the_shape_the_examples_show() -> None:
    """The examples teach a shape. The player's own facts have to arrive in it."""
    assert facts(EXAMPLES[0][0]) == (
        "Name: Sam Rivera\n"
        "Position: SS\n"
        "Bats: R\n"
        "Throws: R\n"
        "Hometown: Chicago, IL\n"
        "Bat Speed: 68.2 mph\n"
        "Exit Velocity: 88.5 mph\n"
        "Launch Angle: 14.0 deg\n"
        "Swing Length: 7.1 ft\n"
        "Attack Angle: 9.0 deg\n"
        "Time to Contact: 155 ms"
    )
    assert facts(EXAMPLES[1][0]) == (
        "Name: Dana Okafor\n"
        "Position: C\n"
        "Bats: L\n"
        "Bat Speed: 74.9 mph\n"
        "Exit Velocity: 96.3 mph\n"
        "Launch Angle: 28.0 deg\n"
        "Attack Angle: 15.0 deg"
    )


def test_the_system_prompt_carries_both_example_backs_word_for_word() -> None:
    """Typed out here, so a slip in EXAMPLES cannot hide. Dana's launch angle is
    28: 22 would be a line drive by the bands, and the back calls it fly balls."""
    for words in (
        '"nickname": "Short Fuse"',
        '"tagline": "Quick to the ball, quicker to first"',
        "Sam gets the bat to the ball in 155 ms, a short swing at 7.1 ft. The 88.5 mph exit "
        "velocity will not clear many fences. The 14 degree launch angle sends it over the "
        "infield. Doubles, not homers.",
        '"nickname": "The Crane"',
        '"tagline": "Swings hard and aims for the second deck"',
        "Dana swings at 74.9 mph and the ball leaves at 96.3. A 28 degree launch angle and a "
        "15 degree attack angle mean fly balls, and at that speed many land in the seats. "
        "Strikeouts come with the package.",
    ):
        assert words in SYSTEM, words
    for stats, _ in EXAMPLES[:2]:
        assert f"Facts:\n{facts(stats)}\n\n{blurb_plan(stats)}\n\nCard:\n" in SYSTEM


def test_every_prompt_carries_the_hitter_with_one_number_and_a_bio() -> None:
    """The third example is a card whose height, weight and hometown were
    typed: one number, a sentence for the bio facts written the way the facts
    write them, the first name and no he or she."""
    stats, text = EXAMPLES[2]
    assert facts(stats) == (
        "Name: Leo Grant\nPosition: CF\nBats: L\nHeight: 5 ft 9 in\nWeight: 182 lb\n"
        "Hometown: Fresno, CA\nExit Velocity: 93.4 mph"
    )
    shown = (
        f"Facts:\n{facts(stats)}\n\n{blurb_plan(stats)}\n\nCard:\n"
        '{"nickname": "The Sledge", "tagline": "Barrel first, questions later", '
        '"blurb": "The ball leaves Leo\'s bat at 93.4 mph. Leo stands 5 ft 9 in and weighs '
        '182 lb. Fresno, CA is home. Hits it hard."}'
    )
    assert shown in SYSTEM
    assert SYSTEM.index(shown) > SYSTEM.index("Dana Okafor")
    assert "Three examples." in SYSTEM
    assert not re.search(r"\b(he|she|him|her|his)\b", text.blurb, re.IGNORECASE)


# What a prompt is, byte for byte: the sha256 of json.dumps(build_messages(stats)),
# the system prompt and the user message together. A change to the prompt
# changes them, on purpose, and says why in the CHANGELOG.
PROMPTS = {
    "sha256:cfd1e0930fbf0a2f7c645704ca056e962e4ec29670ce05a46eaf56ebde04f55d": EXAMPLES[0][0],
    "sha256:9ae091f1e2dbc18d5754900144fb099c4f430d5b89614e6ddcce3a0abcdae3c3": EXAMPLES[1][0],
    "sha256:ef7fb884dba149c49a15a985074a382df83e76ece601a6090cb223c12d45f8ae": Stats(
        name="Sam Rivera", position="SS", metrics={"exit_velo_mph": 88.5}
    ),
    "sha256:3b4da0b2977f441e7141d54863d06b641287807ca62b8d150f1c0b18e1bd1ada": Stats(
        name="Sam Rivera", position="SS"
    ),
    # One number and a typed bio.
    "sha256:3d69d1eca51a94f643951ab78c253a5f3cf273955bf7b975d547258b3986e474": Stats(
        name="Sam Rivera",
        height_in=73,
        weight_lb=190,
        hometown="Oakland, CA",
        metrics={"exit_velo_mph": 101.5},
    ),
}
SYSTEM_SHA256 = "sha256:895cbbd48bdf204ee04cd5a9cd0d98ee7193009a876ba01f3709fdad55afd895"
RETRY_SHA256 = "sha256:b9ccb734d15f9d4414dcaae71f15df62b63f830749c8136fb58e9be6f01d7d0f"


def _sha256(value) -> str:
    raw = value if isinstance(value, str) else json.dumps(value)
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def test_the_prompts_are_held_byte_for_byte() -> None:
    """Six numbers, four, one, none, and one with a bio."""
    assert _sha256(SYSTEM) == SYSTEM_SHA256
    for digest, stats in PROMPTS.items():
        assert _sha256(build_messages(stats)) == digest, stats
    one = PROMPTS["sha256:ef7fb884dba149c49a15a985074a382df83e76ece601a6090cb223c12d45f8ae"]
    assert _sha256(retry_messages(one, "{}", ["The nickname is empty."])) == RETRY_SHA256


def test_a_plan_with_no_numbers_says_swing_numbers() -> None:
    assert blurb_plan(Stats(name="Riley Chen")).startswith("These facts hold no swing numbers")


def test_the_prompt_says_the_rules_and_the_word_limits() -> None:
    assert f"nickname: 1 to {LIMITS['nickname']} words" in SYSTEM
    assert f"tagline: at most {LIMITS['tagline']} words" in SYSTEM
    assert f"blurb: at most {LIMITS['blurb']} words" in SYSTEM
    assert "Never a word from the player's name" in SYSTEM
    assert "Every sentence but the last carries a number or a bio fact" in SYSTEM
    assert "5 words or fewer" in SYSTEM
    assert (
        "before the verdict at most one sentence for each number or bio fact in the facts, "
        "so four sentences in all at most. It need not use every fact." in SYSTEM
    )
    assert "Never round one" in SYSTEM
    for phrase in PRAISE:
        assert phrase in SYSTEM, phrase


def test_the_prompt_says_what_a_bio_fact_may_carry_and_what_nothing_may() -> None:
    """Each bio fact may carry one sentence, as given, and nothing beyond it.
    The facts never say a man or a woman either."""
    system = SYSTEM
    assert (
        "Height, Weight, Hometown and Position are bio facts. Each may carry one sentence, "
        "written the way the facts write it, and nothing beyond it: no school, team, "
        "league, year, family, or anything else the facts do not say." in system
    )
    assert "so use the first name, never he or she." in system


def test_the_facts_say_which_side_the_player_throws_from() -> None:
    """Bats and Throws, both, so the back can be held to each side."""
    lines = facts(Stats(name="Sam", bats="S", throws="L")).splitlines()
    assert lines == ["Name: Sam", "Bats: S", "Throws: L"]


def test_the_facts_carry_the_height_and_the_weight_written_one_way() -> None:
    """In the order of the back's bio row: after the sides, before the hometown."""
    stats = Stats(
        name="Riley Chen",
        position="SP",
        throws="L",
        height_in=73,
        weight_lb=190,
        hometown="Oakland, CA",
        metrics={"exit_velo_mph": 101.5},
    )
    assert facts(stats).splitlines() == [
        "Name: Riley Chen",
        "Position: SP",
        "Throws: L",
        "Height: 6 ft 1 in",
        "Weight: 190 lb",
        "Hometown: Oakland, CA",
        "Exit Velocity: 101.5 mph",
    ]
    assert "Height: 6 ft 0 in" in facts(Stats(name="Sam", height_in=72))
    assert "Height: 5 ft 11 in" in facts(Stats(name="Sam", height_in=71))
    assert facts(Stats(name="Sam", weight_lb=205)) == "Name: Sam\nWeight: 205 lb"
    assert (height_phrase(36), height_phrase(96), weight_phrase(60)) == (
        "3 ft 0 in",
        "8 ft 0 in",
        "60 lb",
    )


def test_the_bio_facts_are_the_height_weight_hometown_and_position_given() -> None:
    """Bats and Throws are facts the back may not get wrong, not ones it writes about."""
    assert bio_facts(Stats(name="Sam", bats="R", throws="R", metrics={"exit_velo_mph": 90})) == []
    assert bio_facts(EXAMPLES[2][0]) == ["Position", "Height", "Weight", "Hometown"]
    assert bio_facts(Stats(name="Sam", hometown="Oakland, CA")) == ["Hometown"]


def test_the_prompt_says_what_the_sides_and_the_angles_are_called() -> None:
    assert "R is right-handed, L is left-handed, S is a switch hitter" in SYSTEM
    assert (
        "Launch angle: under 10 degrees is a ground ball, 10 to 25 a line drive, "
        "25 to 50 a fly ball, over 50 a pop up. Attack angle: under 5 degrees is flat, "
        "5 to 15 level to slightly up, over 15 a steep uppercut." in SYSTEM
    )


def test_the_system_prompt_fences_off_real_hitters() -> None:
    assert "Never name a major league player" in SYSTEM
    assert "closest" not in SYSTEM.lower()


def test_the_payload_attaches_the_schema() -> None:
    payload = chat_payload("a-model", build_messages(STATS))
    schema = payload["response_format"]["json_schema"]["schema"]
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["temperature"] == 0.7
    assert set(schema["properties"]) == set(LIMITS)
    assert schema["additionalProperties"] is False


def test_the_model_writes_three_fields_and_no_comps_line() -> None:
    """The numbers are the spec's. Every other test reads them from LIMITS."""
    assert LIMITS == {"nickname": 3, "tagline": 8, "blurb": 40}
    assert TEXT_SCHEMA["required"] == ["nickname", "tagline", "blurb"]
    assert "comps" not in json.dumps(TEXT_SCHEMA)


def test_the_fixture_back_is_the_first_example() -> None:
    """The README's backs and the fixture render show the register the prompt
    asks for. The fixture player is the first example with a height and a
    weight, which the example's back leaves alone, as a back may."""
    stats, text = EXAMPLES[0]
    assert text == FIXTURE_TEXT
    assert facts(fixture_stats()) == facts(
        stats.model_copy(update={"height_in": 72, "weight_lb": 190})
    )
    assert check(FIXTURE_TEXT, fixture_stats()) == []


# -- the limits --------------------------------------------------------------


def test_clamp_words_keeps_short_text_alone() -> None:
    assert clamp_words("one two three", 8) == "one two three"


def test_clamp_words_collapses_whitespace_and_strips_quotes() -> None:
    assert clamp_words('  "Turn\n  stile"  ', 8) == "Turn stile"


def test_clamp_words_does_not_end_on_a_dangling_article() -> None:
    """Cutting at word eight is how you get a tagline that ends on "a"."""
    assert clamp_words("The Cloud Sluggers SS who never cheats a rep", 8) == (
        "The Cloud Sluggers SS who never cheats"
    )
    assert clamp_words("never cheats a rep", 3) == "never cheats"


def test_clamp_applies_every_limit() -> None:
    long = CardText(**{name: " ".join(["word"] * 80) for name in LIMITS})
    clamped = clamp(long)
    for name, limit in LIMITS.items():
        assert len(getattr(clamped, name).split()) <= limit


# -- the parser --------------------------------------------------------------


def test_parse_accepts_plain_json() -> None:
    assert parse(json.dumps(GOOD)).nickname == "Turnstile"


def test_parse_accepts_a_fenced_block() -> None:
    """Models fence their JSON even when told not to."""
    assert parse(f"```json\n{json.dumps(GOOD)}\n```").tagline.startswith("Turns on")


def test_parse_leaves_the_length_to_check() -> None:
    """Cutting here would hide an overlong answer from the retry that fixes it."""
    noisy = {**GOOD, "nickname": "one two three four five"}
    assert parse(json.dumps(noisy)).nickname == "one two three four five"


def test_parse_drops_a_comps_line() -> None:
    assert parse(json.dumps({**GOOD, "comps_narrative": "Swings like a star."})) == CardText(**GOOD)


@pytest.mark.parametrize("content", ["", "not json at all", "{", "[1, 2, 3]", '"a string"'])
def test_parse_refuses_anything_that_is_not_an_object(content: str) -> None:
    with pytest.raises(ValueError):
        parse(content)


def test_parse_fills_in_a_missing_field_rather_than_failing() -> None:
    """CardText defaults every field. check() is what says an empty blurb is wrong."""
    assert parse(json.dumps({"nickname": "Turnstile"})).blurb == ""


# -- the checks --------------------------------------------------------------


@pytest.mark.parametrize("example", range(len(EXAMPLES)))
def test_both_examples_pass_check(example: int) -> None:
    stats, text = EXAMPLES[example]
    assert check(text, stats) == []


def test_the_back_that_came_off_the_node_fails_on_at_least_three_counts() -> None:
    problems = check(NODE_BACK, NODE_PLAYER)
    assert len(problems) >= 3, problems
    assert "The nickname uses a word from the player's name. Use none of it." in problems
    assert 'Cut "shows great" from the blurb.' in problems
    assert 'Cut "reliable threat" from the blurb.' in problems
    assert 'Cut "making him" from the blurb.' in problems


def test_a_good_back_passes() -> None:
    assert check(back(), STATS) == []


@pytest.mark.parametrize("name", list(LIMITS))
def test_each_word_limit_holds_to_the_word(name: str) -> None:
    limit = LIMITS[name]
    # The blurb has to keep its two numbers to be judged on length alone.
    filler = "68.2 88.5" if name == "blurb" else "word word"
    at_limit = " ".join([filler] + ["word"] * (limit - 2))
    assert check(back(**{name: at_limit}), STATS) == []
    over = f"{at_limit} more"
    cut = " Leave out a number and the words that carry it." if name == "blurb" else ""
    assert check(back(**{name: over}), STATS) == [
        f"The {name} is {limit + 1} words. Use {limit} or fewer.{cut}"
    ]


@pytest.mark.parametrize("name", list(LIMITS))
def test_an_empty_field_is_a_problem(name: str) -> None:
    assert f"The {name} is empty." in check(back(**{name: "  "}), STATS)


@pytest.mark.parametrize(
    ("player", "nickname", "uses_it"),
    [
        ("Ka'Ni Quillfeather", "Quillfeather", True),
        ("Ka'Ni Quillfeather", "QUILLFEATHER EXPRESS", True),
        ("Ka'Ni Quillfeather", "Ka’Ni Show", True),
        ("Ka'Ni Quillfeather", "Quill Speed", False),
        ("Sam Rivera", "Sam's Club", True),
        ("Sam Rivera", "Samurai", False),
        ("Bo Jackson", "Bo Knows", False),
        ("José Ramírez", "Jose Cuervo", True),
        ("Adrián Peña", "Pena Colada", True),
        ("Shaquille O'Neal", "Neal Deal", True),
        ("Mary-Kate Green", "Kate Swing", True),
    ],
)
def test_the_nickname_may_not_use_a_word_of_the_name(
    player: str, nickname: str, uses_it: bool
) -> None:
    """Case-insensitive, whole words, three letters or longer."""
    problem = "The nickname uses a word from the player's name. Use none of it."
    stats = STATS.model_copy(update={"name": player})
    assert (problem in check(back(nickname=nickname), stats)) is uses_it


def test_name_words_are_three_letters_or_longer() -> None:
    assert name_words("Ka'Ni Quillfeather") == ["ka'ni", "quillfeather"]
    assert name_words("Shaquille O'Neal") == ["shaquille", "o'neal", "neal"]
    assert name_words("Bo Li") == []


@pytest.mark.parametrize(
    ("nickname", "quoted"),
    [
        ('"Short Fuse"', True),
        ("“Short Fuse”", True),
        ("'Short Fuse'", True),
        ("‘Short Fuse’", True),
        ("Short Fuse", False),
        ("Rippin' Rick", False),
        ("Swingin'", False),
    ],
)
def test_the_nickname_has_no_quote_marks(nickname: str, quoted: bool) -> None:
    """The card puts its own curly quotes around the nickname."""
    problem = "The nickname has quote marks. Leave them off. The card adds its own."
    assert (problem in check(back(nickname=nickname), STATS)) is quoted


THREE = STATS.model_copy(
    update={"metrics": {"bat_speed_mph": 68.2, "launch_angle_deg": 14.0, "time_to_contact_ms": 155}}
)


@pytest.mark.parametrize(
    ("blurb", "used"),
    [
        ("The bat moves at 68.2 mph and gets there in 155 ms. Quick.", 2),
        ("A 14 degree launch angle and 155 ms to contact. Line drives.", 2),
        ("A 14.0 degree launch angle at 68.2 mph. Line drives.", 2),
        ("The bat moves at 68.2 mph. Quick.", 1),
        ("The bat moves at 168.2 mph, 68.25 on a good day, in 155ms. Quick.", 0),
        ("Bat speed (68.2) and a swing done in 155. Quick.", 2),
        ("No numbers at all. Quick.", 0),
    ],
)
def test_the_blurb_needs_two_of_the_numbers_as_whole_tokens(blurb: str, used: int) -> None:
    """14.0 also counts as 14. 168.2, 68.25 and 155ms do not count at all."""
    problems = check(back(blurb=blurb), THREE)
    short = f"The blurb uses {used} of the numbers in the facts."
    assert any(problem.startswith(short) for problem in problems) is (used < 2)


def test_one_metric_needs_one_number_and_none_needs_none() -> None:
    one = STATS.model_copy(update={"metrics": {"exit_velo_mph": 88.5}})
    assert check(back(blurb="The ball leaves at 88.5 mph. Gap power."), one) == []
    assert check(back(blurb="The ball leaves the bat hard. Gap power."), one) == [
        "The blurb uses 0 of the numbers in the facts. "
        "Use at least 1, written the way the facts write them.",
        NUMBERLESS.format(1),
    ]
    none = STATS.model_copy(update={"metrics": {}})
    assert check(back(blurb="No numbers came with this card."), none) == []


def test_a_negative_number_counts_only_with_its_sign() -> None:
    stats = STATS.model_copy(update={"metrics": {"attack_angle_deg": -2.0}})
    assert check(back(blurb="The bat comes in at -2 degrees. Chops."), stats) == []
    assert check(back(blurb="The bat comes in at 2 degrees. Chops."), stats) != []


# -- every sentence but the last carries a number or a bio fact --------------

NUMBERLESS = "Sentence {} of the blurb has no number or bio fact from the facts. Cut it."

# Word for word, a back Gemma 4 wrote on the node on 2026-09-24 for a player who
# sent two numbers. Its third sentence carries none and is not the verdict.
FILLER_PLAYER = Stats(
    name="Ka'Ni Quillfeather",
    bats="R",
    throws="R",
    metrics={"bat_speed_mph": 72.0, "exit_velo_mph": 93.0},
)
FILLER_BACK = CardText(
    nickname="The Cannon",
    tagline="Heavy wood and high velocity",
    blurb=(
        "Ka'Ni swings at 72.0 mph. The ball leaves the bat at 93.0 mph. "
        "These numbers build a power profile. A high-velocity hitter."
    ),
)


def test_the_back_with_a_numberless_sentence_from_the_node_is_caught() -> None:
    """It passed every other check. This is the only thing wrong with it."""
    assert check(FILLER_BACK, FILLER_PLAYER) == [NUMBERLESS.format(3)]


@pytest.mark.parametrize(
    "made_up",
    [
        # Word for word, what Gemma wrote in place of the sentence above when it
        # was asked to give it a number: the two numbers, added up.
        "These numbers total 165.0.",
        "He plays at 1 position.",
        "He swings 72 times a day.",
    ],
)
def test_a_number_that_is_not_in_the_facts_does_not_count(made_up: str) -> None:
    """72 is a form of 72.0 only as a whole token. "72 times" is still 72,
    so that one counts: the check holds the number, not what is said of it."""
    blurb = f"Ka'Ni swings at 72.0 mph. The ball leaves the bat at 93.0 mph. {made_up} Power."
    problems = check(back(blurb=blurb), FILLER_PLAYER)
    counted = made_up.startswith("He swings 72")
    assert (NUMBERLESS.format(3) in problems) is not counted


def test_the_last_sentence_is_the_verdict_and_needs_no_number() -> None:
    assert check(back(), STATS) == []
    assert GOOD["blurb"].endswith("Doubles, not homers.")


def test_each_numberless_sentence_is_named_by_its_place() -> None:
    blurb = (
        "The bat moves at 68.2 mph. It is quick. The ball leaves at 88.5. It carries. Gap power."
    )
    problems = check(back(blurb=blurb), STATS)
    assert [p for p in problems if p.startswith("Sentence")] == [
        NUMBERLESS.format(2),
        NUMBERLESS.format(4),
    ]


@pytest.mark.parametrize(
    "blurb",
    [
        # A decimal point is not the end of a sentence.
        "Sam swings at 68.2 mph and the ball leaves at 88.5. Doubles.",
        # Neither is a stop with no space after it.
        "The bat moves at 68.2 mph.The ball leaves at 88.5. Doubles.",
        # One sentence is its own verdict.
        "The bat moves at 68.2 mph and the ball leaves at 88.5.",
        "Quick, at 68.2 mph! The ball leaves at 88.5? Doubles.",
    ],
)
def test_a_sentence_ends_at_a_stop_and_a_space(blurb: str) -> None:
    assert check(back(blurb=blurb), STATS) == []


MADE_UP = (
    "The {} has a number that is not in the facts. Use only the numbers in the facts, "
    "written the way the facts write them."
)


@pytest.mark.parametrize(
    "blurb",
    [
        # Word for word, a back Gemma wrote on the node on 2026-09-24. 142.7 is
        # nowhere in the facts.
        "Ka'Ni tracks the ball in 155 ms. The 14 degree launch angle produces "
        "line drives. A 9 degree attack angle keeps the ball low. 142.7 total power.",
        # And one from the probe the same day: the two numbers, added up.
        "Ka'Ni swings the bat at 72.0 mph. The ball leaves the wood at 93.0 mph. "
        "A 165.0 total output.",
    ],
)
def test_a_number_the_facts_do_not_hold_is_made_up(blurb: str) -> None:
    stats = Stats(
        name="Ka'Ni Quillfeather",
        bats="R",
        throws="R",
        metrics={
            "bat_speed_mph": 72.0,
            "exit_velo_mph": 93.0,
            "launch_angle_deg": 14.0,
            "attack_angle_deg": 9.0,
            "time_to_contact_ms": 155,
        },
    )
    assert check(back(blurb=blurb), stats) == [MADE_UP.format("blurb")]


def test_a_made_up_number_is_caught_in_every_field() -> None:
    """A year it was said in is made up as well, and has a rule of its own."""
    assert check(back(tagline="Hits 30 homers a game"), STATS) == [MADE_UP.format("tagline")]
    assert check(back(nickname="Number 9"), STATS) == [MADE_UP.format("nickname")]
    none = STATS.model_copy(update={"metrics": {}})
    blurb = "Sam plays shortstop. He hit 12 doubles on Sunday."
    assert check(back(blurb=blurb), none) == [MADE_UP.format("blurb")]
    assert check(back(tagline="Hits 30 homers a year"), STATS) == [
        MADE_UP.format("tagline"),
        'Cut "year" from the tagline. The facts do not say it.',
    ]


def test_the_facts_numbers_in_any_form_they_may_take_are_not_made_up() -> None:
    """14.0 may be written 14. A number inside a word, 2B or 1st, is not a number."""
    stats = STATS.model_copy(update={"metrics": {"launch_angle_deg": 14.0, "exit_velo_mph": 88.5}})
    blurb = "A 14 degree launch angle and 14.0 again. The ball leaves at 88.5 to 2B and 1st. Gaps."
    assert check(back(blurb=blurb), stats) == []


def test_a_player_with_no_numbers_is_not_asked_for_them() -> None:
    none = STATS.model_copy(update={"metrics": {}})
    blurb = "Sam plays shortstop. No swing numbers came with this card."
    assert check(back(blurb=blurb), none) == []


# -- how long the blurb may be -----------------------------------------------

THREE_NUMBERS = {"bat_speed_mph": 72.0, "exit_velo_mph": 93.0, "launch_angle_deg": 12.0}
SIX_NUMBERS = {
    "bat_speed_mph": 72.0,
    "exit_velo_mph": 93.0,
    "launch_angle_deg": 12.0,
    "swing_length_ft": 7.4,
    "attack_angle_deg": 10.0,
    "time_to_contact_ms": 150,
}


@pytest.mark.parametrize(
    ("metrics", "plan"),
    [
        (
            {},
            "These facts hold no swing numbers, so the blurb is at most 2 sentences, "
            "who the player is and then that no swing numbers came with this card.",
        ),
        (
            {"exit_velo_mph": 93.0},
            "These facts hold 1 number, so the blurb is at most 2 short sentences, "
            "1 sentence that carries it and then the verdict.",
        ),
        (
            {"bat_speed_mph": 72.0, "exit_velo_mph": 93.0},
            "These facts hold 2 numbers, so the blurb is at most 3 short sentences, "
            "at most 2 sentences that carry them and then the verdict.",
        ),
        (
            THREE_NUMBERS,
            "These facts hold 3 numbers, so the blurb is at most 4 short sentences, "
            "at most 3 sentences that carry them and then the verdict.",
        ),
        (
            SIX_NUMBERS,
            "These facts hold 6 numbers, so the blurb is at most 4 short sentences, "
            "at most 3 sentences that carry them and then the verdict. "
            "Use the numbers that say the most. It need not use all 6.",
        ),
    ],
)
def test_the_plan_says_how_many_sentences_the_numbers_allow(metrics: dict, plan: str) -> None:
    """On the node, told only "four short sentences or fewer", Gemma wrote four
    for one number and filled two with "He plays left field" and "He is a
    righty". With six numbers it packed all six into three long sentences, and
    about 1 back in 7 went to the template for its length. With the plan, 50 of
    50 backs came from the model on 2026-09-24. A player with no bio facts, as
    here, gets that plan word for word."""
    stats = Stats(name="Sam Rivera", bats="R", throws="R", metrics=metrics)
    assert blurb_plan(stats) == plan
    user = build_messages(stats)[1]["content"]
    assert user == f"Facts:\n{facts(stats)}\n\n{plan}"
    assert ":" not in plan, "the mock reads any line with a colon as a fact"


BIO = {"height_in": 73, "weight_lb": 190, "hometown": "Oakland, CA", "position": "RF"}


@pytest.mark.parametrize(
    ("bio", "metrics", "plan"),
    [
        (
            {},
            {"exit_velo_mph": 101.5},
            "These facts hold 1 number, so the blurb is at most 2 short sentences, "
            "1 sentence that carries it and then the verdict.",
        ),
        (
            {"height_in": 73, "weight_lb": 190},
            {"exit_velo_mph": 101.5},
            "These facts hold 1 number and 2 bio facts, so the blurb is at most 4 short "
            "sentences, at most 3 sentences that carry them and then the verdict. "
            "It must use the number.",
        ),
        (
            {"position": "RF"},
            {"exit_velo_mph": 101.5},
            "These facts hold 1 number and 1 bio fact, so the blurb is at most 3 short "
            "sentences, at most 2 sentences that carry them and then the verdict. "
            "It must use the number.",
        ),
        (
            BIO,
            {"exit_velo_mph": 101.5},
            "These facts hold 1 number and 4 bio facts, so the blurb is at most 4 short "
            "sentences, at most 3 sentences that carry them and then the verdict. "
            "It must use the number.",
        ),
        (
            BIO,
            {"exit_velo_mph": 93.0, "bat_speed_mph": 72.0},
            "These facts hold 2 numbers and 4 bio facts, so the blurb is at most 4 short "
            "sentences, at most 3 sentences that carry them and then the verdict. "
            "It must use at least 2 of the numbers.",
        ),
        (
            BIO,
            SIX_NUMBERS,
            "These facts hold 6 numbers and 4 bio facts, so the blurb is at most 4 short "
            "sentences, at most 3 sentences that carry them and then the verdict. "
            "Use the numbers that say the most. It need not use all 6. "
            "It must use at least 2 of the numbers.",
        ),
        (
            BIO,
            {},
            "These facts hold no swing numbers, so the blurb is at most 2 sentences, "
            "who the player is and then that no swing numbers came with this card.",
        ),
    ],
    ids=["one-alone", "one-height-weight", "one-position", "one-full-bio", "two", "six", "none"],
)
def test_the_plan_counts_the_bio_facts_with_the_numbers(
    bio: dict, metrics: dict, plan: str
) -> None:
    """One number alone is two sentences, and with a height and a weight it is
    four: each may carry one. Nothing is made up to fill the space, and a card
    with no numbers is two sentences whatever its bio."""
    stats = Stats(name="Sam Rivera", bats="R", **bio, metrics=metrics)
    assert blurb_plan(stats) == plan
    assert build_messages(stats)[1]["content"] == f"Facts:\n{facts(stats)}\n\n{plan}"
    assert ":" not in plan, "the mock reads any line with a colon as a fact"


@pytest.mark.parametrize("example", range(len(EXAMPLES)))
def test_each_example_keeps_to_its_own_plan(example: int) -> None:
    """The examples teach the plan by keeping to it."""
    stats, text = EXAMPLES[example]
    sentences = [part for part in text.blurb.split(". ") if part]
    allowed = min(len(stats.metrics) + len(bio_facts(stats)), 3) + 1
    assert len(sentences) <= allowed
    assert f"at most {allowed} short sentences" in blurb_plan(stats)


@pytest.mark.parametrize("phrase", BANNED + PRAISE)
def test_every_banned_phrase_is_cut_from_every_field(phrase: str) -> None:
    for name in LIMITS:
        value = f"{phrase.upper()} 68.2 88.5" if name == "blurb" else phrase
        assert f'Cut "{phrase}" from the {name}.' in check(back(**{name: value}), STATS), name


def test_shows_alone_is_not_on_the_list() -> None:
    """The mock's habit line says "shows up early"."""
    assert check(back(tagline="Shows up early"), STATS) == []


def test_a_word_the_player_brought_is_theirs() -> None:
    """A hometown called Elite is a fact, not praise."""
    stats = STATS.model_copy(update={"hometown": "Elite, TX"})
    assert check(back(tagline="The pride of Elite, TX"), stats) == []
    assert check(back(tagline="An elite bat"), STATS) == ['Cut "elite" from the tagline.']


def test_an_em_dash_is_a_problem() -> None:
    assert check(back(tagline=f"Quick hands {EM_DASH} short path"), STATS) == [
        "The tagline has a long dash. Use a period or a comma."
    ]


# -- sides ---------------------------------------------------------------------

# What the owner read on 2026-09-23: "lefty" on the back of a right-hander.
LEFT_WORDS = ["lefty", "left-handed", "left-hander", "southpaw", "from the left side"]
RIGHT_WORDS = ["righty", "right-handed", "right-hander", "from the right side"]


def sided(bats: str | None = None, throws: str | None = None) -> Stats:
    return STATS.model_copy(update={"bats": bats, "throws": throws})


def said(words: str) -> CardText:
    """GOOD with `words` put in the middle of the blurb."""
    return back(blurb=f"The bat moves at 68.2 mph, {words}, and the ball leaves at 88.5.")


@pytest.mark.parametrize(
    ("bats", "throws", "wrong", "right"),
    [
        ("R", None, LEFT_WORDS, RIGHT_WORDS),
        ("L", None, RIGHT_WORDS, LEFT_WORDS),
        ("S", None, LEFT_WORDS + RIGHT_WORDS, []),
        (None, "R", LEFT_WORDS, RIGHT_WORDS),
        (None, "L", RIGHT_WORDS, LEFT_WORDS),
    ],
)
def test_the_back_may_not_give_a_side_the_facts_do_not(bats, throws, wrong, right) -> None:
    stats = sided(bats, throws)
    for words in wrong:
        problems = check(said(words), stats)
        assert len(problems) == 1, (words, problems)
        assert problems[0].startswith(f'Cut "{words}" from the blurb. The player ')
    for words in right:
        assert check(said(words), stats) == [], words


def test_a_side_is_caught_in_any_field_and_in_any_case() -> None:
    stats = sided("R", "R")
    assert check(back(nickname="Southpaw"), stats) == [
        'Cut "southpaw" from the nickname. The player bats right-handed and throws right-handed.'
    ]
    assert check(back(tagline="A Lefty with quick hands"), stats) == [
        'Cut "lefty" from the tagline. The player bats right-handed and throws right-handed.'
    ]


@pytest.mark.parametrize(
    ("side", "spellings"),
    [
        (
            "R",
            ["leftie", "lefthanded", "lefthander", "left-handers", "left handers", "lefthanders"],
        ),
        ("L", ["rightie", "righthanded", "righthander", "right-handers", "right handers"]),
    ],
)
def test_every_spelling_of_the_other_side_is_caught(side: str, spellings: list[str]) -> None:
    stats = sided(side, side)
    for words in spellings:
        problems = check(said(words), stats)
        assert len(problems) == 1, (words, problems)
        assert problems[0].startswith(f'Cut "{words}" from the blurb.')


def test_the_leftie_from_the_node_is_caught() -> None:
    """Word for word, the nickname Gemma gave a right-hander on 2026-09-24. He
    plays left field, which may be where it came from. The card cannot say so."""
    stats = Stats(name="Ka'Ni Quillfeather", position="LF", bats="R", throws="R")
    blurb = (
        "Ka'Ni Quillfeather plays left field for the Riverside Owls. "
        "No swing numbers came with this card."
    )
    assert check(back(nickname="The Leftie", blurb=blurb), stats) == [
        'Cut "leftie" from the nickname. The player bats right-handed and throws right-handed.'
    ]


def test_a_switch_hitter_who_throws_left_is_no_righty_either() -> None:
    assert check(said("a righty"), sided("S", "L")) == [
        'Cut "righty" from the blurb. The player is a switch hitter and throws left-handed.'
    ]


def test_with_no_side_in_the_facts_no_side_is_ruled_out() -> None:
    for words in LEFT_WORDS + RIGHT_WORDS:
        assert check(said(words), sided()) == [], words


def test_a_lefty_for_a_right_handed_batter_is_caught() -> None:
    stats = Stats(
        name="Ka'Ni Quillfeather",
        bats="R",
        throws="R",
        metrics={"bat_speed_mph": 68.2, "exit_velo_mph": 88.5},
    )
    lefty = CardText(
        nickname="The Hammer",
        tagline="Quick hands, short path",
        blurb="A lefty who swings at 68.2 mph. The ball leaves at 88.5 mph. Gap to gap.",
    )
    assert check(lefty, stats) == [
        'Cut "lefty" from the blurb. The player bats right-handed and throws right-handed.'
    ]


# -- angles ----------------------------------------------------------------------


def angled(launch: float | None = None, attack: float | None = None) -> Stats:
    metrics = {"bat_speed_mph": 68.2, "exit_velo_mph": 88.5}
    if launch is not None:
        metrics["launch_angle_deg"] = launch
    if attack is not None:
        metrics["attack_angle_deg"] = attack
    return STATS.model_copy(update={"metrics": metrics})


@pytest.mark.parametrize(
    ("launch", "allowed"),
    [
        (-12.0, {"ground balls"}),
        (9.9, {"ground balls"}),
        (10.0, {"line drives"}),
        (14.0, {"line drives"}),
        (25.0, {"line drives", "fly balls"}),
        (50.0, {"fly balls"}),
        (50.5, {"pop ups"}),
    ],
)
def test_a_launch_angle_is_called_by_its_band_only(launch: float, allowed: set) -> None:
    for band in ("ground balls", "line drives", "fly balls", "pop ups"):
        problems = check(said(f"{band} mostly"), angled(launch=launch))
        assert (problems == []) == (band in allowed), (launch, band, problems)


@pytest.mark.parametrize(
    ("attack", "allowed"),
    [
        (4.9, {"a flat swing"}),
        (5.0, {"a level swing", "slightly up"}),
        (15.0, {"a level swing", "slightly up"}),
        (15.1, {"a steep uppercut"}),
    ],
)
def test_an_attack_angle_is_called_by_its_band_only(attack: float, allowed: set) -> None:
    for band in ("a flat swing", "a level swing", "slightly up", "a steep uppercut"):
        problems = check(said(band), angled(attack=attack))
        assert (problems == []) == (band in allowed), (attack, band, problems)


def test_the_smoke_back_from_the_node_is_caught_calling_14_degrees_ground_balls() -> None:
    """What Gemma wrote for the fixture player on 2026-09-23, word for word."""
    node = CardText(
        nickname="Short Fuse",
        tagline="Quick to the ball, quicker to first",
        blurb=(
            "Sam gets the bat to the ball in 155 ms. The 88.5 mph exit velocity follows a "
            "7.1 ft swing. A 14.0 degree launch angle and 9.0 degree attack angle keep it "
            "low. Ground balls and singles."
        ),
    )
    assert check(node, fixture_stats()) == [
        'The blurb says "ground balls". The launch angle in the facts is a line drive, '
        "not a ground ball."
    ]


def test_every_spelling_of_a_band_is_caught() -> None:
    for spelling in ("grounders", "ground-ball", "Groundballs", "liners", "flyball", "pop-ups"):
        assert check(said(spelling), angled(launch=14.0 if "lin" not in spelling else 40.0))


def test_a_band_in_the_nickname_is_held_to_the_number_too() -> None:
    assert check(back(nickname="Line Drive"), angled(launch=30.0)) == [
        'The nickname says "line drive". The launch angle in the facts is a fly ball, '
        "not a line drive."
    ]


def test_without_the_angle_a_band_is_not_checked() -> None:
    assert check(said("ground balls, a steep uppercut"), angled()) == []


# -- numbers joined with no verb -------------------------------------------

# The end of the blurb on a card that came off the node on 2026-10-29: "a
# sentence that adds two numbers for no reason".
KAI = Stats(
    name="Kai Moreno",
    bats="R",
    metrics={"bat_speed_mph": 68.2, "launch_angle_deg": 14.0, "attack_angle_deg": 9.0},
)
KAI_BACK = CardText(
    nickname="Gap Finder",
    tagline="Quick bat, level path",
    blurb="Kai swings at 68.2 mph. 14.0 degrees plus 9.0 degrees. Line drives find the gaps.",
)
JOINED = (
    'Sentence {} of the blurb is only numbers joined by "{}". '
    "Say what the numbers do, with a verb, or cut it."
)


def test_the_back_with_two_numbers_added_up_is_caught() -> None:
    assert check(KAI_BACK, KAI) == [JOINED.format(2, "plus")]


@pytest.mark.parametrize(
    ("sentence", "joiner"),
    [
        ("14.0 degrees plus 9.0 degrees.", "plus"),
        ("14 degrees and 9 degrees.", "and"),
        ("14.0 degrees + 9.0 degrees.", "+"),
        ("14.0 & 9.0.", "&"),
        ("A 14.0 degree launch angle plus a 9.0 degree attack angle.", "plus"),
        ("Launch angle 14.0 and attack angle 9.0.", "and"),
        ("Kai's 68.2 mph and 14 degrees.", "and"),
        ("Kai, 68.2 mph plus a 9 degree attack angle.", "plus"),
        ("68.2 mph off the bat and 14.0 degrees up.", "and"),
    ],
)
def test_numbers_joined_with_no_verb_are_caught(sentence: str, joiner: str) -> None:
    assert joined_numbers(sentence, KAI.name) == joiner


@pytest.mark.parametrize(
    "sentence",
    [
        # A verb, so the numbers say something.
        "Kai swings at 68.2 mph and the ball rises at 14.0 degrees.",
        "A 14 degree launch angle and a 9 degree attack angle mean line drives.",
        "Kai swings at 68.2 mph and 14 degrees.",
        # One number, joined to nothing.
        "A 14.0 degree launch angle plus a level path.",
        # Two numbers and no joining word: the template writes these.
        "Launch angle 14 degrees, attack angle 9 degrees.",
        # A joining word and no numbers: a verdict.
        "Doubles and triples.",
    ],
)
def test_a_sentence_with_a_verb_or_no_joined_numbers_passes(sentence: str) -> None:
    assert joined_numbers(sentence, KAI.name) is None


def test_both_examples_and_the_template_have_no_joined_numbers() -> None:
    for stats, text in EXAMPLES:
        for sentence in text.blurb.split(". "):
            assert joined_numbers(sentence, stats.name) is None, sentence
    template = template_text(fixture_stats())
    assert not [p for p in check(template, fixture_stats()) if "joined by" in p]


def test_joined_numbers_are_caught_in_the_last_sentence_too() -> None:
    blurb = "Kai swings at 68.2 mph. Fly balls, 14.0 and 9.0."
    problems = check(KAI_BACK.model_copy(update={"blurb": blurb}), KAI)
    assert JOINED.format(2, "and") not in problems
    flat = "Kai swings at 68.2 mph. 14.0 and 9.0."
    assert JOINED.format(2, "and") in check(KAI_BACK.model_copy(update={"blurb": flat}), KAI)


async def test_joined_numbers_get_the_retry_then_the_template() -> None:
    """As every other rule: one retry that names the sentence, then the template."""
    first = json.dumps(KAI_BACK.model_dump())
    fixed = {
        **KAI_BACK.model_dump(),
        "blurb": (
            "Kai swings at 68.2 mph. A 14.0 degree launch angle and a 9.0 degree attack "
            "angle mean line drives. Line drives find the gaps."
        ),
    }
    sent: list[dict] = []
    result = await run(completion(first), completion(json.dumps(fixed)), sent=sent, stats=KAI)
    assert result.from_model
    assert result.text.blurb == fixed["blurb"]
    assert sent[1]["messages"][-1]["content"] == "\n".join([RETRY, JOINED.format(2, "plus")])

    twice = await run(completion(first), stats=KAI)
    assert not twice.from_model
    assert twice.text == template_text(KAI)
    assert [attempt["problems"] for attempt in twice.meta["tries"]] == [
        [JOINED.format(2, "plus")],
        [JOINED.format(2, "plus")],
    ]


def test_no_problem_quotes_the_name_or_the_numbers() -> None:
    """The problems reach the ledger and the api's log, not only the model."""
    sided_player = NODE_PLAYER.model_copy(update={"bats": "R", "throws": "R"})
    wrong = back(blurb="A lefty, 16.0 degrees of pop ups and a flat 7.6 ft swing.")
    joined = back(blurb="Ka'Ni swings at 92.1 mph. 16.0 degrees plus 7.6 ft. Line drives.")
    problems = (
        check(NODE_BACK, NODE_PLAYER)
        + check(back(blurb="Nothing."), NODE_PLAYER)
        + check(wrong, sided_player)
        + check(FILLER_BACK, FILLER_PLAYER)
        + check(joined, NODE_PLAYER)
    )
    assert JOINED.format(2, "plus") in problems
    text = " ".join(problems)
    assert "Quillfeather" not in text
    assert "Ka'Ni" not in text
    for value in ("7.6", "16.0", "92.1", "72.0", "93.0"):
        assert value not in text


# -- the bio facts -------------------------------------------------------------

# A player who typed a bio: one number, and each bio fact as the app sends it.
# OF is a position and a word, which is the point.
GUEST = Stats(
    name="Sam Rivera",
    position="OF",
    bats="R",
    height_in=73,
    weight_lb=190,
    hometown="Oakland, CA",
    metrics={"exit_velo_mph": 101.5},
)


def second(sentence: str) -> CardText:
    """A back for GUEST whose second sentence of three is `sentence`."""
    return back(blurb=f"The ball leaves at 101.5 mph. {sentence} Loud contact.")


@pytest.mark.parametrize(
    ("sentence", "problems"),
    [
        # The four facts: the phrases count, a bare number does not.
        ("Sam stands 6 ft 1 in.", []),
        ("Sam weighs 190 lb.", []),
        ("Sam stands 6 tall.", [NUMBERLESS.format(2), MADE_UP.format("blurb")]),
        ("Sam weighs 190.", [NUMBERLESS.format(2), MADE_UP.format("blurb")]),
        # The 6 of the height is not a 6 for anything else.
        ("Sam throws 6 pitches.", [NUMBERLESS.format(2), MADE_UP.format("blurb")]),
        # Rounded is made up: 73 inches is not 6 ft.
        ("Sam stands 6 ft.", [NUMBERLESS.format(2), MADE_UP.format("blurb")]),
        ("Sam stands 16 ft 1 in.", [NUMBERLESS.format(2), MADE_UP.format("blurb")]),
        # Written another way is not the way the facts write it.
        ("Sam weighs 190 pounds.", [NUMBERLESS.format(2), MADE_UP.format("blurb")]),
        ("Sam weighs 190 lbs.", [NUMBERLESS.format(2), MADE_UP.format("blurb")]),
        # Both at once, in any case, across a line break.
        ("Sam is 6 FT 1 IN and\n190 lb.", []),
        # The hometown whole, or its town.
        ("Oakland, CA is home.", []),
        ("Sam grew up in Oakland.", []),
        ("Sam grew up in CA.", [NUMBERLESS.format(2)]),
        # The position as the facts write it, or by the name the front prints.
        ("Sam plays OF.", []),
        ("Sam plays outfield.", []),
        ("Most of the time it carries.", [NUMBERLESS.format(2)]),
        # A side is a fact the back may not get wrong, not one it writes about.
        ("Sam bats right-handed.", [NUMBERLESS.format(2)]),
    ],
)
def test_a_bio_fact_counts_only_whole_and_as_the_facts_write_it(sentence, problems) -> None:
    assert check(second(sentence), GUEST) == problems


def test_a_whole_foot_height_may_be_written_without_its_inches() -> None:
    """As 14.0 may be 14: 72 inches is 6 ft 0 in, or 6 ft."""
    tall = GUEST.model_copy(update={"height_in": 72})
    assert check(second("Sam stands 6 ft 0 in."), tall) == []
    assert check(second("Sam stands 6 ft."), tall) == []
    assert check(second("Sam stands 6 ft 1 in."), tall) == [MADE_UP.format("blurb")]


def test_a_six_foot_swing_is_the_swings_and_not_the_height() -> None:
    """A 6.0 ft swing is written "6 ft", which is a 6 ft 0 in player's height
    too. The mock stack wrote it on 2026-11-05. It counts as the swing."""
    stats = Stats(
        name="Sam Rivera", height_in=72, metrics={"swing_length_ft": 6.0, "exit_velo_mph": 90.0}
    )
    both = "The swing covers 6 ft. The ball leaves at 90 mph. Sam stands 6 ft 0 in. Gaps."
    assert check(back(blurb=both), stats) == []
    # One number used, the swing's: the "6 ft" was not taken for the height.
    swing_only = "The swing covers 6 ft. Sam stands 6 ft 0 in. Gaps."
    assert check(back(blurb=swing_only), stats) == [
        "The blurb uses 1 of the numbers in the facts. "
        "Use at least 2, written the way the facts write them."
    ]


@pytest.mark.parametrize(
    ("hometown", "blurb"),
    [
        ("St. Louis, MO", "The ball leaves at 101.5 mph. Sam calls St. Louis, MO home. Loud."),
        ("St. Louis, MO", "The ball leaves at 101.5 mph. Sam grew up in St. Louis. Loud."),
        ("Washington, D.C.", "The ball leaves at 101.5 mph. Sam is from Washington, D.C. Loud."),
        ("Washington, D.C.", "The ball leaves at 101.5 mph. Washington, D.C. is home. Loud."),
    ],
)
def test_a_stop_inside_the_players_hometown_ends_no_sentence(hometown: str, blurb: str) -> None:
    """St. Louis is one town. Cut at its stop, "Sam calls St." would be a
    sentence with no fact, and "Louis, MO home." another."""
    assert check(back(blurb=blurb), GUEST.model_copy(update={"hometown": hometown})) == []


def test_a_sentence_after_a_hometown_with_a_stop_still_needs_a_fact() -> None:
    stats = GUEST.model_copy(update={"hometown": "Washington, D.C."})
    blurb = "Sam is from Washington, D.C. It carries. The ball leaves at 101.5 mph. Loud."
    assert check(back(blurb=blurb), stats) == [NUMBERLESS.format(2)]


@pytest.mark.parametrize(
    ("hometown", "metrics", "blurb"),
    [
        # Real towns whose names are an attack angle's band and a launch angle's.
        (
            "Flat Rock, MI",
            {"attack_angle_deg": 9.0},
            "A 9 degree attack angle. Flat Rock, MI is home. Gaps.",
        ),
        (
            "Level Green, PA",
            {"attack_angle_deg": 3.0},
            "A 3 degree attack angle. Level Green, PA is home. Gaps.",
        ),
        (
            "29 Palms, CA",
            {"exit_velo_mph": 101.5},
            "The ball leaves at 101.5 mph. 29 Palms, CA is home. Loud.",
        ),
    ],
)
def test_the_players_own_hometown_is_a_town_and_not_a_band_or_a_number(
    hometown, metrics, blurb
) -> None:
    stats = Stats(name="Sam Rivera", hometown=hometown, metrics=metrics)
    assert check(back(blurb=blurb), stats) == []


def test_a_band_outside_the_hometown_is_held_to_the_number_as_before() -> None:
    stats = Stats(name="Sam Rivera", hometown="Flat Rock, MI", metrics={"attack_angle_deg": 9.0})
    blurb = "A 9 degree attack angle. Flat Rock, MI is home. A flat cut."
    assert check(back(blurb=blurb), stats) == [
        'The blurb says "flat". The attack angle in the facts is level to slightly up, not flat.'
    ]


def test_a_bio_list_with_no_verb_is_numbers_joined() -> None:
    """As "14.0 degrees plus 9.0 degrees." is. With a verb it says something."""
    assert check(second("6 ft 1 in and 190 lb."), GUEST) == [JOINED.format(2, "and")]
    assert check(second("Sam stands 6 ft 1 in and weighs 190 lb."), GUEST) == []


def test_a_bio_fact_in_the_tagline_is_no_made_up_number() -> None:
    assert (
        check(
            back(tagline="All 6 ft 1 in of it"),
            GUEST.model_copy(update={"metrics": {"bat_speed_mph": 68.2, "exit_velo_mph": 88.5}}),
        )
        == []
    )


def test_the_phrases_a_bio_fact_may_take() -> None:
    assert bio_phrases(GUEST) == (
        ("6 ft 1 in", "190 lb", "Oakland, CA", "Oakland", "Outfield"),
        ("OF",),
    )
    assert bio_phrases(Stats(name="Sam", height_in=72, position="ZZ")) == (
        ("6 ft 0 in", "6 ft"),
        ("ZZ",),
    )
    assert bio_phrases(Stats(name="Sam")) == ((), ())


@pytest.mark.parametrize("phrase", UNSAID)
def test_every_word_for_what_the_facts_never_say_is_cut_from_every_field(phrase: str) -> None:
    """A school, a team, a league, a year, a family: the facts hold none of them."""
    for name in LIMITS:
        value = f"{phrase.upper()} 68.2 88.5" if name == "blurb" else phrase
        problems = check(back(**{name: value}), STATS)
        assert f'Cut "{phrase}" from the {name}. The facts do not say it.' in problems, name


def test_a_word_for_what_the_facts_never_say_is_theirs_when_their_facts_say_it() -> None:
    stats = GUEST.model_copy(update={"hometown": "College Park, MD"})
    assert check(second("College Park, MD is home."), stats) == []
    assert check(second("Sam played college ball."), GUEST) == [
        NUMBERLESS.format(2),
        'Cut "college" from the blurb. The facts do not say it.',
    ]


def test_a_back_that_makes_up_a_school_from_a_hometown_is_caught() -> None:
    """What a hometown invites. Every number in it is the player's."""
    made_up = back(
        blurb="The ball leaves at 101.5 mph. Sam played high school ball in Oakland. Loud contact."
    )
    assert check(made_up, GUEST) == ['Cut "school" from the blurb. The facts do not say it.']


def test_no_problem_quotes_the_bio() -> None:
    """Like the name and the numbers, the bio reaches the ledger and the log."""
    wrong = back(blurb="Sam weighs 190. Sam stands 6 ft. Sam played school ball in Oakland, CA.")
    text = " ".join(check(wrong, GUEST))
    for value in ("190", "6 ft", "Oakland", "OF", "73"):
        assert value not in text, value


# -- the template ------------------------------------------------------------


@pytest.mark.parametrize(
    ("metrics", "nickname"),
    [
        ({"time_to_contact_ms": 150}, "Quick Hands"),
        ({"time_to_contact_ms": 151}, "Line Drive"),
        ({"exit_velo_mph": 95.0}, "Big Bat"),
        ({"exit_velo_mph": 94.9}, "Line Drive"),
        ({"launch_angle_deg": 20.0}, "Sky Ball"),
        ({"launch_angle_deg": 19.9}, "Line Drive"),
        ({"launch_angle_deg": 10.0}, "Line Drive"),
        ({"launch_angle_deg": 9.9}, "Worm Burner"),
        ({"launch_angle_deg": -8.0, "exit_velo_mph": 96.0}, "Big Bat"),
        ({"time_to_contact_ms": 140, "exit_velo_mph": 99.0}, "Quick Hands"),
        ({"exit_velo_mph": 99.0, "launch_angle_deg": 25.0}, "Big Bat"),
        ({}, "The Regular"),
    ],
)
def test_the_template_nickname_is_read_off_the_numbers(metrics: dict, nickname: str) -> None:
    """First match wins, in the order the rules are written."""
    assert template_nickname(metrics) == nickname


def test_the_template_blurb_is_one_sentence_per_measurement() -> None:
    """Then one of the bio. The fixture player is 6 ft 0 in, which a person writes 6 ft."""
    written = template_text(fixture_stats())
    assert written.blurb == (
        "Sam swings at 68.2 mph. The ball leaves at 88.5 mph. "
        "Launch angle 14 degrees, attack angle 9 degrees. A 7.1 ft swing, 155 ms to contact. "
        "Sam stands 6 ft, weighs 190 lb and is from Chicago, IL."
    )
    assert written.nickname == "Line Drive"
    assert written.tagline == "SS"


RILEY = Stats(name="Riley Chen", position="CF", bats="L", metrics={"exit_velo_mph": 93.0})
RILEY_BIO = RILEY.model_copy(update={"height_in": 70, "weight_lb": 175, "hometown": "Oakland, CA"})


def test_a_one_number_template_with_a_bio_is_not_one_line() -> None:
    """The template adds one bio sentence after the metric sentences when there
    is a bio to say. The position is the tagline."""
    assert template_text(RILEY).blurb == "The ball leaves at 93 mph."
    written = template_text(RILEY_BIO)
    assert written.blurb == (
        "The ball leaves at 93 mph. Riley stands 5 ft 10 in, weighs 175 lb and is from Oakland, CA."
    )
    assert written.tagline == "CF"
    # The template is not run through check(), but this one passes anyway.
    assert check(written, RILEY_BIO) == []


@pytest.mark.parametrize(
    ("bio", "sentence"),
    [
        ({"height_in": 73}, "Riley stands 6 ft 1 in."),
        ({"height_in": 72}, "Riley stands 6 ft."),
        ({"weight_lb": 175}, "Riley weighs 175 lb."),
        ({"hometown": "Oakland, CA"}, "Riley is from Oakland, CA."),
        ({"height_in": 70, "weight_lb": 175}, "Riley stands 5 ft 10 in and weighs 175 lb."),
        ({"hometown": "Washington, D.C."}, "Riley is from Washington, D.C."),
        ({}, None),
    ],
)
def test_the_bio_sentence_says_what_was_given(bio: dict, sentence: str | None) -> None:
    stats = RILEY.model_copy(update=bio)
    assert bio_sentence(stats, LIMITS["blurb"]) == sentence
    if sentence:
        assert check(template_text(stats), stats) == []


def test_the_bio_sentence_gives_up_the_hometown_before_the_word_limit() -> None:
    """As much as fits, height first. Never a sentence cut in half."""
    assert bio_sentence(RILEY_BIO, 14) == (
        "Riley stands 5 ft 10 in, weighs 175 lb and is from Oakland, CA."
    )
    assert bio_sentence(RILEY_BIO, 13) == "Riley stands 5 ft 10 in and weighs 175 lb."
    assert bio_sentence(RILEY_BIO, 9) == "Riley stands 5 ft 10 in."
    assert bio_sentence(RILEY_BIO, 5) is None
    # All six numbers take 27 words. A hometown that would take the bio past 40
    # goes, and the height and the weight stay.
    crowded = Stats(
        name="Sam Rivera",
        hometown="Saint Mary of the Woods Village in the Wabash Valley",
        height_in=72,
        weight_lb=190,
        metrics=fixture_stats().metrics,
    )
    written = template_text(crowded).blurb
    assert written.endswith("A 7.1 ft swing, 155 ms to contact. Sam stands 6 ft and weighs 190 lb.")
    assert len(written.split()) <= LIMITS["blurb"]


def test_a_template_with_no_numbers_is_as_it_was_whatever_its_bio() -> None:
    assert template_text(RILEY_BIO.model_copy(update={"metrics": {}})).blurb == (
        "Riley Chen plays CF. No swing numbers came with this card."
    )


@pytest.mark.parametrize(
    ("metrics", "blurb"),
    [
        ({"launch_angle_deg": 16.0}, "Launch angle 16 degrees."),
        ({"attack_angle_deg": -1.0}, "Attack angle -1 degree."),
        ({"swing_length_ft": 7.6}, "A 7.6 ft swing."),
        ({"time_to_contact_ms": 155}, "155 ms to contact."),
        ({"exit_velo_mph": 92.1}, "The ball leaves at 92.1 mph."),
    ],
)
def test_the_template_writes_only_what_it_was_given(metrics: dict, blurb: str) -> None:
    assert template_text(Stats(name="Ka'Ni Quillfeather", metrics=metrics)).blurb == blurb


def test_the_template_with_no_metrics_says_so() -> None:
    assert template_text(STATS.model_copy(update={"metrics": {}})).blurb == (
        "Sam Rivera plays SS. No swing numbers came with this card."
    )
    written = template_text(Stats(name="Sam Rivera"))
    assert written.blurb == "Sam Rivera plays ball. No swing numbers came with this card."
    assert written.nickname == "The Regular"


def test_the_template_is_not_the_players_surname() -> None:
    """The nickname used to be the last word of the name. The owner noticed."""
    assert template_text(STATS).nickname == "Line Drive"


def test_the_template_obeys_the_limits() -> None:
    """Every number and the longest bio the stats take still fit."""
    crowded = fixture_stats().model_copy(
        update={"position": "UTIL", "hometown": "Saint Mary of the Woods Village, Indiana, USA"}
    )
    for stats in (STATS, fixture_stats(), crowded):
        written = template_text(stats)
        for name, limit in LIMITS.items():
            assert len(getattr(written, name).split()) <= limit


def test_the_fixture_template_would_pass_its_own_checks() -> None:
    """The template is not run through check(), but on a plain player it passes anyway."""
    assert check(template_text(fixture_stats()), fixture_stats()) == []


# -- generate ----------------------------------------------------------------


async def test_a_good_answer_is_used() -> None:
    result = await run(completion(json.dumps(GOOD)))
    assert result.from_model
    assert result.text == CardText(**GOOD)
    assert result.meta["model"] == "mock/wordlist"
    assert result.meta["tries"] == [{"answer": json.dumps(GOOD), "problems": []}]


async def test_an_answer_that_passes_is_tidied_before_it_is_used() -> None:
    """Quotes around a tagline and a line break in a blurb are not problems. They
    are cleaned up on the way to the card."""
    tidy = {
        **GOOD,
        "tagline": '"Quick hands, short path"',
        "blurb": GOOD["blurb"].replace(" ", "\n  ", 1),
    }
    result = await run(completion(json.dumps(tidy)))
    assert result.from_model
    assert result.text == CardText(**{**GOOD, "tagline": "Quick hands, short path"})


async def test_a_back_with_problems_gets_one_retry_that_names_each() -> None:
    first = json.dumps(
        {"nickname": "Rivera", "tagline": "A solid bat", "blurb": "Sam shows great mechanics."}
    )
    sent: list[dict] = []
    result = await run(completion(first), completion(json.dumps(GOOD)), sent=sent)

    assert result.from_model
    assert result.text == CardText(**GOOD)
    problems = check(parse(first), STATS)
    assert problems == [
        "The nickname uses a word from the player's name. Use none of it.",
        "The blurb uses 0 of the numbers in the facts. "
        "Use at least 2, written the way the facts write them.",
        'Cut "solid" from the tagline.',
        'Cut "shows great" from the blurb.',
        'Cut "great mechanics" from the blurb.',
    ]
    # The retry is the conversation so far, then one line for each problem.
    assert sent[1]["messages"] == [
        *build_messages(STATS),
        {"role": "assistant", "content": first},
        {"role": "user", "content": "\n".join([RETRY, *problems])},
    ]
    assert sent[1]["messages"][-1]["content"].splitlines()[1:] == problems
    assert result.meta["tries"] == [
        {"answer": first, "problems": problems},
        {"answer": json.dumps(GOOD), "problems": []},
    ]


async def test_problems_twice_fall_back_to_the_template() -> None:
    node = json.dumps(NODE_BACK.model_dump(exclude={"comps_narrative"}))
    result = await run(completion(node), stats=NODE_PLAYER)

    assert not result.from_model
    assert result.text == template_text(NODE_PLAYER)
    assert result.error.startswith("the text failed its checks. ")
    assert [len(attempt["problems"]) >= 3 for attempt in result.meta["tries"]] == [True, True]


@pytest.mark.parametrize(
    ("stats", "made_up", "problems", "fixed", "floor"),
    [
        (
            RILEY,
            "The ball leaves at 93 mph. The bat moves late. Gap to gap.",
            [NUMBERLESS.format(2)],
            "The ball leaves at 93 mph. Gap to gap.",
            "The ball leaves at 93 mph.",
        ),
        (
            RILEY_BIO,
            "The ball leaves at 93 mph. Riley weighs 175. Gap to gap.",
            [NUMBERLESS.format(2), MADE_UP.format("blurb")],
            "The ball leaves at 93 mph. Riley stands 5 ft 10 in and weighs 175 lb. "
            "Oakland, CA is home. Gap to gap.",
            "The ball leaves at 93 mph. Riley stands 5 ft 10 in, weighs 175 lb and is from "
            "Oakland, CA.",
        ),
    ],
    ids=["no-bio", "bio"],
)
async def test_a_one_number_card_gets_the_retry_then_the_template(
    stats: Stats, made_up: str, problems: list[str], fixed: str, floor: str
) -> None:
    """With a bio and without: one retry that names each problem, then the
    template, which says the bio too when there is one."""
    card = {"nickname": "Barrel Man", "tagline": "Barrel first, questions later"}
    first = json.dumps({**card, "blurb": made_up})
    sent: list[dict] = []
    result = await run(
        completion(first), completion(json.dumps({**card, "blurb": fixed})), sent=sent, stats=stats
    )
    assert result.from_model
    assert result.text.blurb == fixed
    assert sent[1]["messages"][-1]["content"] == "\n".join([RETRY, *problems])
    assert sent[0]["messages"][1]["content"] == f"Facts:\n{facts(stats)}\n\n{blurb_plan(stats)}"

    twice = await run(completion(first), stats=stats)
    assert not twice.from_model
    assert twice.text == template_text(stats)
    assert twice.text.blurb == floor
    assert [attempt["problems"] for attempt in twice.meta["tries"]] == [problems, problems]


async def test_overlong_output_is_retried_and_not_cut_into_the_card() -> None:
    """A blurb cut at word 40 ends mid-sentence. The template reads better."""
    flood = {name: " ".join(["word"] * 100) for name in LIMITS}
    result = await run(completion(json.dumps(flood)))
    assert not result.from_model
    assert "The blurb is 100 words. Use 40 or fewer." in result.error
    assert result.text == template_text(STATS)


async def test_malformed_json_is_retried_and_then_falls_back() -> None:
    sent: list[dict] = []
    result = await run(completion("this is not json"), sent=sent)
    assert not result.from_model
    assert "not JSON" in result.error
    assert result.text.nickname == "Line Drive"
    assert sent[1]["messages"][-1]["content"].startswith(RETRY)


async def test_an_empty_answer_is_asked_again_as_it_was() -> None:
    """There is nothing to correct, so the retry is the first request again."""
    sent: list[dict] = []
    result = await run(completion(""), completion(json.dumps(GOOD)), sent=sent)
    assert result.from_model
    assert sent[1]["messages"] == build_messages(STATS)


async def test_a_retry_that_works_is_used() -> None:
    result = await run(completion("nope"), completion(json.dumps(GOOD)))
    assert result.from_model
    assert result.text.nickname == "Turnstile"


async def test_a_401_falls_back_without_raising() -> None:
    result = await run(httpx.Response(401, json={"detail": "no"}))
    assert not result.from_model
    assert "refused the api's key" in result.error
    assert [attempt["answer"] for attempt in result.meta["tries"]] == [None, None]


async def test_a_500_falls_back() -> None:
    result = await run(httpx.Response(500, text="boom"))
    assert not result.from_model
    assert "500" in result.error


async def test_a_reply_with_no_choices_falls_back() -> None:
    result = await run(httpx.Response(200, json={"choices": []}))
    assert not result.from_model
    assert "no message content" in result.error


async def test_a_connection_failure_falls_back() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        result = await generate(client, "http://llm", KEY, "a-model", STATS)
    assert not result.from_model
    assert "ConnectError" in result.error
    # The pipeline reads the cause to say the model is down, not that it wrote badly.
    assert isinstance(result.cause, httpx.ConnectError)


async def test_an_answer_that_fails_its_checks_has_no_cause() -> None:
    """The model was reached. What it wrote is the problem, not the connection."""
    result = await run(completion("this is not json"))
    assert (result.from_model, result.cause) == (False, None)


async def test_a_last_try_that_reached_the_model_clears_the_first_ones_cause() -> None:
    result = await run(httpx.Response(500, text="boom"), completion("this is not json"))
    assert (result.from_model, result.cause) == (False, None)
    one = await run(completion("this is not json"), httpx.Response(500, text="boom"))
    assert isinstance(one.cause, ServiceError)
