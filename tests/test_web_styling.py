"""The stylesheet must define every class the markup uses.

The user's complaint was blunt: "website tetap jelek, polosan semua". The cause
was measurable. The model wrote `class="flex items-center justify-between"` on a
header, and the stylesheet had no `.flex`, `.items-center`, or `.justify-between`
rule at all. An undefined class is not an error in CSS - the element just keeps
the browser's default look. Real sites shipped with headings as default black
serif and a theme toggle that rendered nothing.

These tests pin the fix: the sheet defines the utilities, and anything the model
invents is backfilled rather than silently dropped.
"""
from __future__ import annotations

import re

from app.services.design_system import base_stylesheet, choose_direction
from app.services.web_builder import _backfill_missing_classes, _default_rule_for

# The exact classes the model used on a real generated site whose layout broke.
REAL_WORLD_CLASSES = [
    "flex", "items-center", "justify-between", "gap-6", "space-x-4",
    "h1", "lead", "hero-content", "grid-2", "grid-3",
]


def _defines(sheet: str, name: str) -> bool:
    return bool(re.search(rf"\.{re.escape(name)}(?![\w-])", sheet))


def test_the_stylesheet_defines_the_classes_the_model_reaches_for():
    sheet = base_stylesheet(choose_direction("toko kopi"))
    missing = [c for c in REAL_WORLD_CLASSES if not _defines(sheet, c)]
    assert not missing, f"stylesheet must define these, missing: {missing}"


def test_a_utility_actually_does_something():
    """Defined is not enough - it must carry the rule the name promises."""
    sheet = base_stylesheet(choose_direction("toko kopi"))
    assert re.search(r"\.flex\s*\{\s*display:\s*flex", sheet), ".flex must set display"
    assert re.search(r"\.items-center\s*\{\s*align-items:\s*center", sheet)
    assert re.search(r"\.justify-between\s*\{\s*justify-content:\s*space-between", sheet)
    assert re.search(r"\.h1\s*\{[^}]*font-size", sheet), ".h1 must size the heading"
    assert re.search(r"\.lead\s*\{[^}]*font-size", sheet), ".lead must size the text"


def test_an_unknown_class_is_backfilled_not_dropped():
    html = '<div class="flex gap-6 weird-thing">x</div>'
    sheet, patched = _backfill_missing_classes(html, "body { color: #000; }")
    assert "gap-6" in patched, "a utility the sheet lacks must be patched"
    assert "weird-thing" in patched, "an unknown class must not be left undefined"
    assert _defines(sheet, "gap-6")
    assert _defines(sheet, "weird-thing")


def test_the_scale_follows_the_name():
    """gap-6 means 1.5rem, not an arbitrary number."""
    assert "1.5rem" in _default_rule_for("gap-6")
    assert "1rem" in _default_rule_for("gap-4")
    assert "2rem" in _default_rule_for("gap-8")


def test_a_silly_number_cannot_blow_up_the_layout():
    """A typo like gap-96 must not produce a 24rem gap."""
    rule = _default_rule_for("gap-96")
    assert "24rem" not in rule, "the scale must be capped"


def test_backfill_reports_nothing_when_the_sheet_is_complete():
    sheet = ".flex { display: flex; }"
    _, patched = _backfill_missing_classes('<div class="flex">x</div>', sheet)
    assert patched == [], "a complete sheet needs no patching"


def test_the_backfilled_sheet_is_valid_css_shape():
    """Whatever we append must still look like CSS, or the page breaks worse."""
    html = '<div class="gap-6 custom-card">x</div>'
    sheet, _ = _backfill_missing_classes(html, "body { margin: 0; }")
    assert sheet.count("{") == sheet.count("}"), "braces must balance"
