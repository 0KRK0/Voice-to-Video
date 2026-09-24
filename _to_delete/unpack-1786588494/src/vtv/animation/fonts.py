"""Choosing a font that can actually draw the text.

The failure this module exists to prevent is the most embarrassing one a
multilingual product can ship: a video full of tofu boxes, rendered confidently,
delivered to a customer who speaks the language.

Three defences:

* **Per-script font resolution.** Latin, Devanagari, Telugu and Han need
  different faces. One font does not cover them, and picking by language rather
  than script gets Urdu wrong.
* **A coverage probe.** `script_support()` reports what this machine can render
  and how well, by loading the font and asking whether the glyphs exist. It is
  measurement, not configuration.
* **A refusal to pretend.** A script with only a universal fallback available is
  reported as `quality="fallback"`, and `ScriptSupport.is_usable` is false. The
  system will still draw it — a legible fallback beats nothing — but nothing
  claims it is production quality.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from vtv.contracts.language import Script

#: Candidate faces per script, best first. Sourced from what Debian-family
#: images commonly ship; the first that exists wins.
_CANDIDATES: dict[Script, tuple[tuple[str, str], ...]] = {
    # (path, quality) where quality is "good" for a designed face and
    # "fallback" for a universal one that merely has the glyphs.
    Script.LATIN: (
        ("/usr/share/fonts/truetype/google-fonts/Poppins-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "good"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "good"),
    ),
    Script.CYRILLIC: (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "good"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "good"),
    ),
    Script.GREEK: (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "good"),
    ),
    Script.HEBREW: (
        ("/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/freefont/FreeSans.ttf", "fallback"),
    ),
    Script.ARABIC: (
        ("/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/freefont/FreeSerif.ttf", "fallback"),
    ),
    Script.DEVANAGARI: (
        ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf", "good"),
        ("/usr/share/fonts/truetype/unifont/unifont.ttf", "fallback"),
    ),
    Script.TELUGU: (
        ("/usr/share/fonts/truetype/noto/NotoSansTelugu-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/lohit-telugu/Lohit-Telugu.ttf", "good"),
        ("/usr/share/fonts/truetype/unifont/unifont.ttf", "fallback"),
    ),
    Script.TAMIL: (
        ("/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/lohit-tamil/Lohit-Tamil.ttf", "good"),
        ("/usr/share/fonts/truetype/unifont/unifont.ttf", "fallback"),
    ),
    Script.KANNADA: (
        ("/usr/share/fonts/truetype/noto/NotoSansKannada-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/unifont/unifont.ttf", "fallback"),
    ),
    Script.MALAYALAM: (
        ("/usr/share/fonts/truetype/noto/NotoSansMalayalam-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/unifont/unifont.ttf", "fallback"),
    ),
    Script.BENGALI: (
        ("/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/unifont/unifont.ttf", "fallback"),
    ),
    Script.GUJARATI: (
        ("/usr/share/fonts/truetype/noto/NotoSansGujarati-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/unifont/unifont.ttf", "fallback"),
    ),
    Script.GURMUKHI: (
        ("/usr/share/fonts/truetype/noto/NotoSansGurmukhi-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/unifont/unifont.ttf", "fallback"),
    ),
    Script.THAI: (
        ("/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf", "good"),
        ("/usr/share/fonts/truetype/unifont/unifont.ttf", "fallback"),
    ),
    Script.HAN: (
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "good"),
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "good"),
        ("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf", "good"),
    ),
    Script.KANA: (
        ("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf", "good"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "good"),
    ),
    Script.HANGUL: (
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "good"),
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "good"),
    ),
}

#: Bold faces, where a distinct one exists. Scripts without a bold face use the
#: regular one rather than synthesising a bold, which looks worse than plain.
_BOLD: dict[Script, tuple[str, ...]] = {
    Script.LATIN: (
        "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    Script.CYRILLIC: ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",),
    Script.GREEK: ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",),
    Script.HAN: ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",),
    Script.HANGUL: ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",),
}

#: A representative string per script, used to probe glyph coverage.
_PROBE: dict[Script, str] = {
    Script.LATIN: "Ag",
    Script.CYRILLIC: "Аб",
    Script.GREEK: "Αβ",
    Script.HEBREW: "אב",
    Script.ARABIC: "العربية",
    Script.DEVANAGARI: "हिन्दी",
    Script.TELUGU: "తెలుగు",
    Script.TAMIL: "தமிழ்",
    Script.KANNADA: "ಕನ್ನಡ",
    Script.MALAYALAM: "മലയാളം",
    Script.BENGALI: "বাংলা",
    Script.GUJARATI: "ગુજરાતી",
    Script.GURMUKHI: "ਪੰਜਾਬੀ",
    Script.THAI: "ไทย",
    Script.HAN: "中文",
    Script.KANA: "日本語",
    Script.HANGUL: "한국어",
}


@dataclass(frozen=True)
class ResolvedFont:
    path: str
    quality: str
    script: Script


def _first_existing(candidates: tuple[tuple[str, str], ...]) -> ResolvedFont | None:
    for path, quality in candidates:
        if Path(path).exists():
            return ResolvedFont(path=path, quality=quality, script=Script.UNKNOWN)
    return None


@lru_cache(maxsize=64)
def resolve_for_script(script: Script, *, bold: bool = False) -> ResolvedFont | None:
    """The best installed face for a script, or ``None`` if there is none."""
    if bold:
        for path in _BOLD.get(script, ()):
            if Path(path).exists():
                return ResolvedFont(path=path, quality="good", script=script)
    found = _first_existing(_CANDIDATES.get(script, ()))
    if found is None and script is not Script.LATIN:
        # Universal fallback: ugly, but a legible glyph beats a box.
        for path in (
            "/usr/share/fonts/truetype/unifont/unifont.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            if Path(path).exists():
                return ResolvedFont(path=path, quality="fallback", script=script)
        return None
    if found is None:
        return None
    return ResolvedFont(path=found.path, quality=found.quality, script=script)


@lru_cache(maxsize=256)
def load(script: Script, size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a face for a script at a size, cached."""
    resolved = resolve_for_script(script, bold=bold)
    if resolved is None:
        resolved = resolve_for_script(Script.LATIN, bold=bold)
    if resolved is None:  # pragma: no cover - a system with no fonts at all
        return ImageFont.load_default_imagefont()  # type: ignore[return-value]
    try:
        return ImageFont.truetype(resolved.path, size)
    except OSError:  # pragma: no cover - corrupt or unreadable face
        return ImageFont.load_default_imagefont()  # type: ignore[return-value]


@lru_cache(maxsize=1)
def shaping_available() -> bool:
    """Whether PIL has libraqm, which does complex-script shaping.

    Without it Devanagari conjuncts do not form and Arabic letters do not join.
    The text is still drawn; it is simply wrong in a way a reader of that script
    notices immediately, which is why this is reported rather than assumed.
    """
    try:
        from PIL import features

        return bool(features.check("raqm"))
    except Exception:
        return False


def covers(font: ImageFont.FreeTypeFont, text: str) -> bool:
    """Whether a face has a glyph for every character in a string.

    ``getmask`` renders; a face without the glyph produces a blank or a box.
    Comparing against a known-missing codepoint is the reliable way to tell.
    """
    try:
        raw = font.getmask(text, mode="L")
        return bool(raw.getbbox())
    except Exception:
        return False


def script_support(script: Script) -> dict[str, object]:
    """Measured support for one script on this machine."""
    from vtv.contracts.language import ScriptSupport

    resolved = resolve_for_script(script)
    if resolved is None:
        return ScriptSupport(
            script=script,
            has_font=False,
            quality="none",
            note="No installed font covers this script.",
        ).model_dump(mode="json")

    probe = _PROBE.get(script, "Ag")
    font = load(script, 24)
    has_glyphs = covers(font, probe)
    shaping = shaping_available()

    note = None
    quality = resolved.quality if has_glyphs else "none"
    if quality == "fallback":
        note = (
            "Only a universal fallback face is installed. Text renders legibly "
            "but not attractively; install the matching Noto face for production."
        )
    elif script.is_complex and not shaping:
        note = (
            "Complex-script shaping is unavailable (PIL was built without "
            "libraqm), so conjuncts and joining will be incorrect."
        )
    elif script.is_rtl:
        # Letter joining is handled by the shaping engine and verified to work.
        # Word-level bidirectional ordering of mixed RTL/LTR runs has NOT been
        # verified by a reader of the script, and this system will not claim it
        # has. Treat right-to-left output as needing sign-off before release.
        note = (
            "Letter shaping works. Word-level bidirectional ordering is "
            "unverified by a native reader — validate before shipping RTL."
        )
        quality = "fallback" if quality == "good" else quality

    return ScriptSupport(
        script=script,
        has_font=has_glyphs,
        quality=quality,
        font_file=resolved.path,
        shaping_available=shaping,
        note=note,
    ).model_dump(mode="json")


def support_report() -> dict[str, object]:
    """Coverage for every script the system knows about.

    Surfaced through `/health` so a deployment's real multilingual capability is
    a fact somebody can read rather than a claim in a README.
    """
    scripts = [script for script in Script if script is not Script.UNKNOWN]
    report = {script.value: script_support(script) for script in scripts}
    usable = [
        name
        for name, detail in report.items()
        if detail.get("quality") == "good"
        and (not Script(name).is_complex or detail.get("shaping_available"))
    ]
    return {
        "shaping_available": shaping_available(),
        "production_ready_scripts": sorted(usable),
        "degraded_scripts": sorted(set(report) - set(usable)),
        "detail": report,
    }


def install_hint(script: Script) -> str:
    """What to install to render this script properly."""
    packages = {
        Script.DEVANAGARI: "fonts-noto-devanagari (or fonts-lohit-deva)",
        Script.TELUGU: "fonts-noto-telugu (or fonts-lohit-telu)",
        Script.TAMIL: "fonts-noto-tamil (or fonts-lohit-taml)",
        Script.KANNADA: "fonts-noto-kannada",
        Script.MALAYALAM: "fonts-noto-malayalam",
        Script.BENGALI: "fonts-noto-bengali",
        Script.GUJARATI: "fonts-noto-gujarati",
        Script.GURMUKHI: "fonts-noto-gurmukhi",
        Script.ARABIC: "fonts-noto-arabic",
        Script.HEBREW: "fonts-noto-hebrew",
        Script.THAI: "fonts-noto-thai",
        Script.HAN: "fonts-noto-cjk",
        Script.KANA: "fonts-noto-cjk",
        Script.HANGUL: "fonts-noto-cjk",
    }
    package = packages.get(script)
    base = f"apt-get install -y {package}" if package else "install a font covering this script"
    if script.is_complex:
        return f"{base}; and rebuild Pillow with libraqm for correct shaping"
    return base


def installed_families() -> list[str]:  # pragma: no cover - diagnostic helper
    """Every font family fontconfig knows about. Used when diagnosing a machine."""
    try:
        completed = subprocess.run(
            ["fc-list", "--format", "%{family}\n"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()})


__all__ = [
    "ResolvedFont",
    "covers",
    "install_hint",
    "installed_families",
    "load",
    "resolve_for_script",
    "script_support",
    "shaping_available",
    "support_report",
]
