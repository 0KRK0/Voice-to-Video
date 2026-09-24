"""Language, locale and script.

The system was English-first and said so. This module is where that stops being
an assumption baked into every function and becomes a parameter carried
explicitly through the pipeline.

Three ideas do the work.

**Script is not language.** Hindi and Marathi share Devanagari; Urdu and Arabic
share Arabic script; Serbian is written in two. Rendering cares about the script
(which font, which direction, which shaping); analysis cares about the language.
Conflating them produces a system that cannot render Urdu because it only knows
about Hindi.

**A project has several languages, not one.** The language spoken, the language
understood, the language of the captions and the language of on-screen text are
four separate decisions. Most of the time they are the same, and the default
makes that easy; when they differ — Telugu speech with English captions — the
model already supports it.

**Support is a measured fact, not a claim.** `ScriptSupport` reports what this
deployment can actually render, based on the fonts actually installed. A system
that claims to support a script it renders as boxes is worse than one that
admits it cannot.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum

from pydantic import Field

from vtv.contracts.base import Confidence, VTVModel


class Script(str, Enum):
    """Writing systems, by ISO 15924 family.

    Only scripts we have a rendering story for are listed. Adding one is a
    commitment to source a font and test the layout, not just an enum member.
    """

    LATIN = "latin"
    DEVANAGARI = "devanagari"
    TELUGU = "telugu"
    TAMIL = "tamil"
    KANNADA = "kannada"
    MALAYALAM = "malayalam"
    BENGALI = "bengali"
    GUJARATI = "gujarati"
    GURMUKHI = "gurmukhi"
    ARABIC = "arabic"
    CYRILLIC = "cyrillic"
    GREEK = "greek"
    HEBREW = "hebrew"
    THAI = "thai"
    HAN = "han"
    KANA = "kana"
    HANGUL = "hangul"
    UNKNOWN = "unknown"

    @property
    def is_rtl(self) -> bool:
        """Right-to-left scripts need mirrored layout, not just a different font."""
        return self in {Script.ARABIC, Script.HEBREW}

    @property
    def is_complex(self) -> bool:
        """Scripts needing shaping: ligatures, reordering, combining marks.

        PIL does basic shaping via libraqm when present and naive glyph
        placement when not. For these scripts that difference is visible, which
        is why `ScriptSupport` reports it rather than hoping.
        """
        return self in {
            Script.DEVANAGARI,
            Script.TELUGU,
            Script.TAMIL,
            Script.KANNADA,
            Script.MALAYALAM,
            Script.BENGALI,
            Script.GUJARATI,
            Script.GURMUKHI,
            Script.ARABIC,
            Script.THAI,
        }

    @property
    def is_cjk(self) -> bool:
        return self in {Script.HAN, Script.KANA, Script.HANGUL}

    @property
    def wraps_on_characters(self) -> bool:
        """CJK and Thai do not put spaces between words.

        Word-boundary wrapping produces a single unbroken line for a Chinese
        headline. These scripts wrap between characters instead.
        """
        return self.is_cjk or self is Script.THAI


#: Unicode block ranges, in the order they are tested. Ordered so that the more
#: specific Indic blocks are checked before the general ranges around them.
_SCRIPT_RANGES: tuple[tuple[Script, tuple[int, int]], ...] = (
    (Script.DEVANAGARI, (0x0900, 0x097F)),
    (Script.BENGALI, (0x0980, 0x09FF)),
    (Script.GURMUKHI, (0x0A00, 0x0A7F)),
    (Script.GUJARATI, (0x0A80, 0x0AFF)),
    (Script.TELUGU, (0x0C00, 0x0C7F)),
    (Script.KANNADA, (0x0C80, 0x0CFF)),
    (Script.MALAYALAM, (0x0D00, 0x0D7F)),
    (Script.TAMIL, (0x0B80, 0x0BFF)),
    (Script.ARABIC, (0x0600, 0x06FF)),
    (Script.ARABIC, (0x0750, 0x077F)),
    (Script.HEBREW, (0x0590, 0x05FF)),
    (Script.THAI, (0x0E00, 0x0E7F)),
    (Script.GREEK, (0x0370, 0x03FF)),
    (Script.CYRILLIC, (0x0400, 0x04FF)),
    (Script.HANGUL, (0xAC00, 0xD7AF)),
    (Script.HANGUL, (0x1100, 0x11FF)),
    (Script.KANA, (0x3040, 0x30FF)),
    (Script.HAN, (0x4E00, 0x9FFF)),
    (Script.HAN, (0x3400, 0x4DBF)),
    (Script.LATIN, (0x0041, 0x024F)),
)


def script_of_char(character: str) -> Script:
    code = ord(character)
    for script, (low, high) in _SCRIPT_RANGES:
        if low <= code <= high:
            return script
    return Script.UNKNOWN


def detect_script(text: str) -> Script:
    """The dominant script of a string.

    Counting rather than sampling the first letter: real text mixes scripts —
    a Telugu sentence quoting an English product name, a Hindi paragraph with
    Latin numerals — and the dominant script is what decides the font.
    """
    counts: dict[Script, int] = {}
    for character in text:
        if character.isspace() or unicodedata.category(character).startswith(("P", "N", "Z")):
            continue
        script = script_of_char(character)
        if script is Script.UNKNOWN:
            continue
        counts[script] = counts.get(script, 0) + 1
    if not counts:
        return Script.LATIN

    # Kana and Hangul are exclusive to one language each, so their presence is
    # decisive even in a minority. Japanese prose is mostly kanji by character
    # count; counting alone would call it Chinese and pick the wrong face.
    for exclusive in (Script.KANA, Script.HANGUL):
        if counts.get(exclusive):
            return exclusive

    return max(counts.items(), key=lambda item: item[1])[0]


def scripts_present(text: str) -> set[Script]:
    """Every script that appears. Used to pick a font that covers all of them."""
    found: set[Script] = set()
    for character in text:
        if character.isspace():
            continue
        script = script_of_char(character)
        if script is not Script.UNKNOWN:
            found.add(script)
    return found


#: BCP-47 primary subtag → (english name, default script).
#: Deliberately explicit rather than derived: a wrong script assignment renders
#: a language as boxes, so each entry is a decision somebody made.
LANGUAGES: dict[str, tuple[str, Script]] = {
    "en": ("English", Script.LATIN),
    "es": ("Spanish", Script.LATIN),
    "fr": ("French", Script.LATIN),
    "de": ("German", Script.LATIN),
    "pt": ("Portuguese", Script.LATIN),
    "it": ("Italian", Script.LATIN),
    "nl": ("Dutch", Script.LATIN),
    "id": ("Indonesian", Script.LATIN),
    "vi": ("Vietnamese", Script.LATIN),
    "tr": ("Turkish", Script.LATIN),
    "sw": ("Swahili", Script.LATIN),
    "ru": ("Russian", Script.CYRILLIC),
    "uk": ("Ukrainian", Script.CYRILLIC),
    "el": ("Greek", Script.GREEK),
    "he": ("Hebrew", Script.HEBREW),
    "ar": ("Arabic", Script.ARABIC),
    "fa": ("Persian", Script.ARABIC),
    "ur": ("Urdu", Script.ARABIC),
    "hi": ("Hindi", Script.DEVANAGARI),
    "mr": ("Marathi", Script.DEVANAGARI),
    "ne": ("Nepali", Script.DEVANAGARI),
    "sa": ("Sanskrit", Script.DEVANAGARI),
    "bn": ("Bengali", Script.BENGALI),
    "as": ("Assamese", Script.BENGALI),
    "pa": ("Punjabi", Script.GURMUKHI),
    "gu": ("Gujarati", Script.GUJARATI),
    "te": ("Telugu", Script.TELUGU),
    "kn": ("Kannada", Script.KANNADA),
    "ml": ("Malayalam", Script.MALAYALAM),
    "ta": ("Tamil", Script.TAMIL),
    "th": ("Thai", Script.THAI),
    "zh": ("Chinese", Script.HAN),
    "ja": ("Japanese", Script.KANA),
    "ko": ("Korean", Script.HANGUL),
}

_TAG = re.compile(r"^([a-zA-Z]{2,3})(?:[-_]([a-zA-Z]{4}))?(?:[-_]([a-zA-Z]{2}|\d{3}))?")


class Language(VTVModel):
    """A BCP-47 language tag, resolved into the parts the system needs."""

    #: The tag as supplied, e.g. ``te``, ``pt-BR``, ``zh-Hans-CN``.
    tag: str = Field(default="en", min_length=2, max_length=32)
    #: Primary subtag, lowercased.
    code: str = Field(default="en", min_length=2, max_length=3)
    script: Script = Script.LATIN
    region: str | None = Field(default=None, max_length=3)
    name: str = "English"

    @classmethod
    def parse(cls, tag: str | None) -> Language:
        """Resolve a tag. Unknown tags become English rather than failing —
        a caption in the wrong language is recoverable; a crashed pipeline is
        not — but the original tag is preserved so the mistake is visible."""
        if not tag:
            return cls()
        match = _TAG.match(tag.strip())
        if not match:
            return cls(tag=tag[:32])
        code = match.group(1).lower()
        explicit_script = match.group(2)
        region = match.group(3)
        name, script = LANGUAGES.get(code, ("Unknown", Script.LATIN))
        if explicit_script:
            script = _SCRIPT_BY_ISO.get(explicit_script.title(), script)
        return cls(
            tag=tag[:32],
            code=code,
            script=script,
            region=region.upper() if region else None,
            name=name,
        )

    @classmethod
    def from_text(cls, text: str, *, hint: str | None = None) -> Language:
        """Best guess from the text itself, preferring an explicit hint.

        Script detection is not language detection — it cannot tell Hindi from
        Marathi — so where a script maps to several languages this returns the
        most widely spoken and marks itself unconfident by leaving the tag as
        the bare code. A real language-identification provider belongs behind a
        port; this is the honest offline answer.
        """
        if hint:
            return cls.parse(hint)
        script = detect_script(text)
        for code, (name, candidate) in LANGUAGES.items():
            if candidate is script:
                return cls(tag=code, code=code, script=script, name=name)
        return cls()

    @property
    def is_rtl(self) -> bool:
        return self.script.is_rtl


_SCRIPT_BY_ISO: dict[str, Script] = {
    "Latn": Script.LATIN,
    "Cyrl": Script.CYRILLIC,
    "Grek": Script.GREEK,
    "Arab": Script.ARABIC,
    "Hebr": Script.HEBREW,
    "Deva": Script.DEVANAGARI,
    "Beng": Script.BENGALI,
    "Guru": Script.GURMUKHI,
    "Gujr": Script.GUJARATI,
    "Telu": Script.TELUGU,
    "Knda": Script.KANNADA,
    "Mlym": Script.MALAYALAM,
    "Taml": Script.TAMIL,
    "Thai": Script.THAI,
    "Hans": Script.HAN,
    "Hant": Script.HAN,
    "Jpan": Script.KANA,
    "Kore": Script.HANGUL,
}


class LanguagePolicy(VTVModel):
    """The four language decisions a project makes.

    Defaulting `understanding`, `captions` and `on_screen` to the source keeps
    the common case a single field, while making "speak Telugu, caption in
    English" expressible without a special path through the pipeline.
    """

    source: Language = Field(default_factory=Language)
    #: The language the semantic layer reasons in. Usually the source; set to
    #: English when only English-capable analysis is available for the source.
    understanding: Language | None = None
    captions: Language | None = None
    #: Text drawn into the visuals. Often shorter phrases than captions, and
    #: sometimes deliberately kept in the source language for authenticity.
    on_screen: Language | None = None

    @property
    def understanding_language(self) -> Language:
        return self.understanding or self.source

    @property
    def caption_language(self) -> Language:
        return self.captions or self.source

    @property
    def on_screen_language(self) -> Language:
        return self.on_screen or self.source

    @property
    def requires_translation(self) -> bool:
        """Whether any output language differs from the source."""
        return any(
            language is not None and language.code != self.source.code
            for language in (self.understanding, self.captions, self.on_screen)
        )

    def all_scripts(self) -> set[Script]:
        return {
            self.source.script,
            self.understanding_language.script,
            self.caption_language.script,
            self.on_screen_language.script,
        }


class ScriptSupport(VTVModel):
    """What this deployment can actually render for one script.

    Reported through `/health`, so nobody has to guess whether Telugu will come
    out as text or as boxes.
    """

    script: Script
    #: A font covering this script is installed.
    has_font: bool = False
    #: That font is a designed typeface rather than a universal fallback.
    quality: str = "none"  # none | fallback | good
    font_file: str | None = None
    #: Complex scripts additionally need shaping; without it, Devanagari
    #: conjuncts and Arabic joining are wrong even with the right font.
    shaping_available: bool = False
    note: str | None = None

    @property
    def is_usable(self) -> bool:
        """Whether we would put this script in front of a customer."""
        if not self.has_font:
            return False
        if self.script.is_complex and not self.shaping_available:
            return False
        return self.quality == "good"


class TranslationRequest(VTVModel):
    """A unit of text to translate, with the context that makes it translatable.

    Translating captions line by line loses the thread; supplying the
    surrounding narration is what stops "It was smaller" becoming grammatically
    impossible in a gendered language.
    """

    text: str = Field(min_length=1, max_length=8000)
    source: Language
    target: Language
    context: str | None = Field(default=None, max_length=4000)
    #: Terms that must not be translated: product names, technical terms.
    preserve: list[str] = Field(default_factory=list, max_length=64)


class TranslationResult(VTVModel):
    text: str
    source: Language
    target: Language
    provider: str | None = None
    confidence: Confidence | None = None


__all__ = [
    "LANGUAGES",
    "Language",
    "LanguagePolicy",
    "Script",
    "ScriptSupport",
    "TranslationRequest",
    "TranslationResult",
    "detect_script",
    "script_of_char",
    "scripts_present",
]
