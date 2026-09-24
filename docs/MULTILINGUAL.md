# Multilingual

## Why this is architecture and not a translation table

A product that works in English and "supports" eighteen other languages by
running the same code with different strings ships tofu boxes to people who
speak those languages. The failures are not linguistic; they are structural, and
each one has to be designed for:

| The mistake | What it produces | Where it is handled |
| --- | --- | --- |
| Picking a font by language | Urdu rendered in Devanagari | `Script`, not `Language`, selects the font |
| One font for everything | Boxes for Telugu, Tamil, Han | Per-script resolution with a coverage probe |
| Wrapping CJK on spaces | One unbroken line off the frame | `Script.wraps_on_characters` |
| Assuming LTR | Arabic in reverse word order | `Script.is_rtl`, and an honest caveat |
| Ignoring shaping | Devanagari conjuncts drawn as separate glyphs | `shaping_available()` detects libraqm |
| Latin timing constants | Captions that outrun dense scripts | Timing is measured, not assumed |

## Language and script are different things

```python
Language.parse("ur").script   # Script.ARABIC — not Devanagari
Language.parse("hi").script   # Script.DEVANAGARI
Language.parse("mr").script   # Script.DEVANAGARI — same script, different language
Language.parse("zh-Hant-TW")  # explicit script subtag wins
```

Hindi and Urdu are mutually intelligible and are written in different scripts.
Hindi and Marathi are different languages in the same script. Any font logic
keyed on the language gets one of these wrong.

`LANGUAGES` maps each tag to a name and a script explicitly, rather than
deriving it. A wrong script assignment renders a language as boxes, so each
entry is a decision somebody made and can be reviewed.

## The eighteen the platform commits to

Telugu, Hindi, Tamil, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi,
Urdu, Spanish, French, German, Portuguese, Arabic, Japanese, Korean, Chinese —
plus English, Russian, Hebrew, Thai and others already in the table.

`test_every_committed_language_is_known` asserts each one resolves to a real
script and a real name. It is the test that would have caught shipping Telugu as
Latin.

## Script detection is not language detection

`detect_script` counts characters rather than sampling the first, because real
text mixes scripts — a Telugu sentence quoting an English product name, a Hindi
paragraph with Latin numerals.

Kana and Hangul are decisive even in a minority: Japanese prose is mostly kanji
by character count, so counting alone would call it Chinese and pick the wrong
face.

What this **cannot** do is tell Hindi from Marathi, or Hindi from Sanskrit. They
are the same script. `Language.from_text` returns the most widely spoken
candidate and leaves the tag as the bare code to mark itself unconfident. Real
language identification belongs behind a port.

## Fonts: measurement, not configuration

`fonts.script_support(script)` loads the face and asks whether the glyphs exist.
`fonts.support_report()` returns two disjoint lists:

- `production_ready_scripts` — a designed typeface is installed, and if the
  script needs shaping, shaping is available.
- `degraded_scripts` — everything else, each with a `note` saying what is wrong
  and an `install_hint()` saying how to fix it.

A script with only a universal fallback is reported as `quality="fallback"` and
`is_usable` is false. The system will still draw it — a legible fallback beats
nothing — but nothing claims it is production quality.

### The RTL caveat, stated plainly

Arabic and Hebrew letter *shaping* works when libraqm is present. Word-level
**bidirectional ordering** of mixed RTL/LTR runs is **not verified by anyone who
reads those scripts**, and this system will not claim it is. The support report
downgrades RTL scripts to `fallback` with:

> Letter shaping works. Word-level bidirectional ordering is unverified by a
> native reader — validate before shipping RTL.

That caveat is the honest position. Removing it would be the single easiest way
to ship something embarrassing to an Arabic-speaking customer.

## Language flows as a policy, not a global

```python
LanguagePolicy(
    source=Language.parse("te"),      # spoken Telugu
    captions=Language.parse("en"),    # captioned in English
)
```

Four independent decisions — source, understanding, captions, on-screen text —
each defaulting to the source, so the common case is one field and "speak
Telugu, caption in English" needs no special path through the pipeline.
`all_scripts()` returns every script the render must be able to draw, which is
what the font layer checks before the first frame.

## Provider routing is where language becomes real

A speech or text provider declares `languages` in its `ProviderCapabilities`.
The router does not select a provider for a language it does not list, and the
fallback ladder descends instead. That is what makes eighteen languages a
routing question rather than a rewrite — and what stops a provider producing
confident nonsense in the wrong accent.

## What is not done

- **Translation** is a contract (`TranslationRequest`/`TranslationResult`) and a
  port, not an implementation. It needs a provider.
- **Per-language timing calibration.** Speaking rates differ substantially by
  language; the synthetic-timing constant is Latin-calibrated and is used only
  for written input, which is labelled synthetic.
- **Native review.** No output in any of the eighteen has been read by a native
  speaker. The font report is a machine's opinion about glyph coverage, which is
  necessary and not sufficient.
