"""
A last check on an answer as it streams, in plain code, one sentence at a time.
Rules in the prompt don't hold with thinking off: answers kept naming players
from the model's memory ("Gabriel Martinelli" at a club he left), inventing
first names ("Omari Rogers" where the material says "Rogers"), crediting sources
that don't exist ("The Run of Play's partners") and talking about the material
itself. The final check by a model that replaced this added 3-4s and mostly
false alarms; this one adds the time to the end of a sentence and costs nothing.

- Every answer: invented credits are rewritten, and sentences about the
  material are dropped.
- Answers built on search results (check_names): a wrong first name is cut back
  to the surname the material has, and a sentence naming someone the material
  doesn't mention at all is dropped. Evergreen answers are left alone, since
  they're meant to come from football knowledge.
"""
import re
import unicodedata

SENTENCE_END = re.compile(r"(?<=[.!?])[\"'”’)*_]*\s+|\n+")
FOLLOW_UPS = re.compile(r"^\s*(?:\*\*|__)?Ask next", re.I | re.M)

CREDIT_CLAUSE = re.compile(
    r",?\s*(?:according to|as per|via)\s+The Run of Play['’]s\s+(?:partners|sources|friends|contacts)"
    r"(?:\s+at\s+[A-Z][\w.]*(?:\s+[A-Z][\w.]*)*)?,?",
)
CREDIT_NAMING = re.compile(r"The Run of Play['’]s\s+(?:partners|sources|friends|contacts)\s+at\s+")
CREDIT_BARE = re.compile(r"The Run of Play['’]s\s+(?:partners|sources|friends|contacts)")
SOURCE_TALK = re.compile(
    r"\b(?:latest|recent|our|these|those|the)\s+dispatches\b"
    r"|\b(?:the|this|our)\s+(?:provided\s+|available\s+|reference\s+)?material\b"
    r"|\breference\s+material\b"
    r"|\bprovided\s+(?:context|articles?|information)\b"
    r"|\bthe\s+context\s+(?:does|doesn|didn|provided|given|i\b)"
    r"|\b(?:information|details)\s+(?:available|provided)\b",
    re.I,
)

NAME = re.compile(r"[A-ZÀ-Þ][\w'’-]+(?:\s+(?:de\s+|van\s+|von\s+|di\s+|da\s+)?[A-ZÀ-Þ][\w'’-]+)+")
# Capitalised because they start a sentence, not because they're part of a name.
STARTERS = {
    "a", "after", "against", "ah", "all", "also", "an", "and", "as", "at", "back", "before", "behind", "beyond",
    "both", "but", "by", "despite", "down", "even", "for", "from", "given", "having", "he", "her", "here", "his",
    "how", "however", "if", "in", "indeed", "it", "its", "just", "like", "meanwhile", "much", "my", "next", "no",
    "not", "now", "of", "on", "one", "only", "or", "our", "over", "players", "right", "she", "since", "so", "still",
    "that", "the", "their", "then", "there", "these", "they", "this", "those", "though", "to", "under", "up", "was",
    "well", "what", "when", "where", "while", "who", "why", "will", "with", "without", "yes", "yet", "you", "young",
    "your",
}
# Capitalised pairs that are places, teams, titles and dates, not people.
NOT_A_PERSON = {
    "academy", "albion", "arena", "athletic", "award", "awards", "ballon", "bar", "blancos", "blue", "blues", "boot", "bridge",
    "bundesliga",
    "camp", "carabao", "casuals", "clasico", "celtic", "champions", "championship", "city", "club", "clubs", "conference",
    "coventry", "cup", "cups", "derby", "devils", "division", "east", "emirates", "england", "etihad", "europa",
    "europe", "fa", "fantasy", "fc", "fifa", "final", "finals", "football", "forest", "fpl", "gameweek", "ghost",
    "giants", "glove", "golden", "ground", "gunners", "hotspur", "hove", "international", "jack", "king", "kings", "kingdom",
    "la", "lane", "league", "liga", "ligue", "london", "madrid", "magpies", "manager", "matchday", "milan",
    "national", "nations", "new", "nou", "north", "old", "olympic", "park", "player", "premier", "primeira", "prize",
    "qualifiers", "queens", "rangers", "real", "red", "reds", "road", "round", "royal", "saints", "san", "santiago",
    "seagulls", "season", "selecao", "serie", "shield", "sky", "south", "spain", "sporting", "sports", "spurs",
    "stadium", "stamford", "star", "stars", "super", "tigers", "toffees", "tottenham", "town", "trafford", "trophy",
    "uefa", "united", "valley", "villa", "wanderers", "way", "west", "wembley", "white", "wolves", "women", "world",
    "yankee", "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}


POSSESSIVE = re.compile(r"['\u2019]s?$")


def _folded(text):
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def _has_word(text, word):
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _people(sentence):
    """The first-and-last-name pairs in a sentence, without sentence starters.
    A possessive ends a name: "Arsenal's Max Dowman's" holds Max Dowman, and
    "Mikel Arteta's Arsenal" Mikel Arteta."""
    for match in NAME.finditer(sentence):
        run = []
        for word in match.group(0).split() + [""]:
            ends_name = POSSESSIVE.search(word) or not word
            run.append(POSSESSIVE.sub("", word))
            if not ends_name:
                continue
            words = [w for w in run if w]
            run = []
            while words and _folded(words[0]) in STARTERS:
                words.pop(0)
            if len(words) >= 2 and not any(_folded(w) in NOT_A_PERSON for w in words):
                yield " ".join(words)


def check_sentence(sentence, allowed, check_names):
    """The sentence to show, possibly edited, or "" to drop it. `allowed` is
    the folded text of everything the answer may draw on."""
    if sentence.lstrip().startswith("#"):
        return sentence
    edited = CREDIT_CLAUSE.sub("", sentence)
    edited = CREDIT_NAMING.sub("", edited)
    edited = CREDIT_BARE.sub("reports", edited)
    if edited != sentence:
        print(f"[OffTheBar check] credit rewritten: {sentence.strip()[:120]}")
    if SOURCE_TALK.search(edited):
        print(f"[OffTheBar check] dropped, talks about its sources: {sentence.strip()[:120]}")
        return ""
    if check_names:
        for person in _people(edited):
            folded = _folded(person)
            if _has_word(allowed, folded):
                continue
            surname = folded.split()[-1]
            if len(surname) >= 4 and _has_word(allowed, surname):
                print(f"[OffTheBar check] {person}: only the surname is known")
                edited = edited.replace(person, person.split()[-1])
            else:
                print(f"[OffTheBar check] dropped, {person} isn't in the material: {sentence.strip()[:120]}")
                return ""
    return edited


def checked(pieces, allowed_text, check_names):
    """Streamed text passed on sentence by sentence, each checked. A sentence
    waits for its end, so text moves in sentences rather than chunks. Nothing
    after the Ask next line is checked, and if every sentence would go, the
    original text is shown rather than an empty answer."""
    allowed = _folded(allowed_text)
    buffer = ""
    follow_ups = False
    shown = False
    dropped = []

    def emit(sentence):
        nonlocal shown
        result = check_sentence(sentence, allowed, check_names)
        if result.strip():
            shown = True
        else:
            dropped.append(sentence)
        return result

    for piece in pieces:
        if follow_ups:
            yield piece
            continue
        buffer += piece
        heading = FOLLOW_UPS.search(buffer)
        ready, rest = (buffer[:heading.start()], buffer[heading.start():]) if heading else (buffer, "")
        position = 0
        for boundary in SENTENCE_END.finditer(ready):
            text = emit(ready[position:boundary.end()])
            position = boundary.end()
            if text:
                yield text
        buffer = ready[position:] + rest
        if heading:
            if buffer[:len(buffer) - len(rest)].strip():
                text = emit(buffer[:len(buffer) - len(rest)])
                if text:
                    yield text
            if not shown and dropped:
                yield "".join(dropped)
                shown = True
            yield rest
            buffer = ""
            follow_ups = True
    if buffer:
        text = emit(buffer)
        if text:
            yield text
    if not shown and dropped:
        yield "".join(dropped)
