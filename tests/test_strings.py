import pathlib
import re

from src.core.strings import STRINGS

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


FIELD = re.compile(r"{(\w+)}")


def test_languages_have_same_keys():
    for lang, table in STRINGS.items():
        assert set(table) == set(STRINGS["en"]), lang


def test_placeholders_match_between_languages():
    for lang, table in STRINGS.items():
        for key, en in STRINGS["en"].items():
            assert set(FIELD.findall(en)) == set(FIELD.findall(table[key])), (lang, key)


def test_web_panel_languages_match():
    html = (SRC / "web" / "index.html").read_text(encoding="utf-8")
    blocks = {
        lang: dict(re.findall(r'(\w+): "((?:[^"\\]|\\.)*)"', body))
        for lang, body in re.findall(r"\n  (\w+): \{\n(.*?)\n  \},", html, re.S)
    }
    assert set(blocks) == {"vi", "en", "es"}
    for lang, table in blocks.items():
        assert set(table) == set(blocks["en"]), lang
        for key, en in blocks["en"].items():
            assert set(FIELD.findall(en)) == set(FIELD.findall(table[key])), (lang, key)


def test_every_used_key_exists():
    used = set()
    for path in SRC.rglob("*.py"):
        used |= set(re.findall(r'get_string\(\s*"(\w+)"', path.read_text(encoding="utf-8")))
    missing = used - set(STRINGS["en"])
    assert not missing, f"get_string keys missing from STRINGS: {sorted(missing)}"
