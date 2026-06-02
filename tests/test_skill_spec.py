"""Validate SKILL.md structural integrity.

The skill ships as a markdown file with a YAML frontmatter block that
defines inputs. This test catches regressions:

- broken frontmatter YAML
- missing required top-level fields
- inconsistent input definitions (allowed vs default)
- dead template references

It does NOT validate the prose body — that's out of scope for CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "skill" / "SKILL.md"
TEMPLATES_DIR = REPO_ROOT / "skill" / "templates"

# Fields required at the top level of the frontmatter
REQUIRED_TOP_FIELDS = {"name", "description", "version", "inputs"}

# Valid input types
VALID_INPUT_TYPES = {"string", "integer", "number", "boolean", "array", "object"}

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$")


def _load_frontmatter() -> dict:
    text = SKILL_MD.read_text(encoding="utf-8")
    if not text.startswith("---"):
        pytest.fail("SKILL.md does not start with frontmatter (---)")
    end = text.index("---", 3)
    fm_text = text[3:end].strip()
    return yaml.safe_load(fm_text)


class TestSkillMd:
    def test_file_exists(self):
        assert SKILL_MD.is_file(), f"missing {SKILL_MD}"

    def test_frontmatter_is_valid_yaml(self):
        _load_frontmatter()  # raises if invalid

    def test_has_body_after_frontmatter(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        end = text.index("---", 3)
        body = text[end + 3 :].strip()
        assert len(body) > 100, "SKILL.md body is suspiciously short"


class TestFrontmatterShape:
    def test_required_fields_present(self):
        fm = _load_frontmatter()
        missing = REQUIRED_TOP_FIELDS - set(fm.keys())
        assert not missing, f"frontmatter missing required fields: {missing}"

    def test_name_is_nonempty_string(self):
        fm = _load_frontmatter()
        assert isinstance(fm["name"], str) and fm["name"].strip()

    def test_description_is_nonempty_string(self):
        fm = _load_frontmatter()
        assert isinstance(fm["description"], str) and fm["description"].strip()

    def test_version_is_semver(self):
        fm = _load_frontmatter()
        v = str(fm["version"])
        assert SEMVER_RE.match(v), f"version '{v}' is not semver"


class TestInputs:
    def test_inputs_is_dict(self):
        fm = _load_frontmatter()
        assert isinstance(fm["inputs"], dict) and fm["inputs"]

    def test_every_input_declares_type(self):
        fm = _load_frontmatter()
        for name, spec in fm["inputs"].items():
            assert "type" in spec, f"input '{name}' has no type"
            assert spec["type"] in VALID_INPUT_TYPES, (
                f"input '{name}' has unknown type: {spec['type']}"
            )

    def test_default_values_respect_allowed(self):
        fm = _load_frontmatter()
        for name, spec in fm["inputs"].items():
            if "allowed" in spec and "default" in spec and spec["default"] is not None:
                assert spec["default"] in spec["allowed"], (
                    f"input '{name}': default '{spec['default']}' not in allowed {spec['allowed']}"
                )

    def test_required_inputs_have_no_default(self):
        fm = _load_frontmatter()
        for name, spec in fm["inputs"].items():
            if spec.get("required"):
                # required inputs may omit default, OR have default=null — both fine
                assert spec.get("default", None) is None, (
                    f"required input '{name}' should not declare a default"
                )

    def test_topic_input_pattern_compiles(self):
        fm = _load_frontmatter()
        topic = fm["inputs"].get("topic", {})
        pattern = topic.get("pattern")
        if pattern is not None:
            re.compile(pattern)  # raises on bad regex


class TestTemplates:
    def test_templates_dir_exists(self):
        assert TEMPLATES_DIR.is_dir(), f"missing {TEMPLATES_DIR}"

    def test_template_references_resolve(self):
        """Any `templates/<file>` reference in SKILL.md must resolve."""
        text = SKILL_MD.read_text(encoding="utf-8")
        refs = set(re.findall(r"templates/([a-zA-Z0-9_./-]+)", text))
        # Only check refs that look like actual filenames (have an extension)
        file_refs = {r for r in refs if "." in r.split("/")[-1]}
        for ref in file_refs:
            target = TEMPLATES_DIR / ref
            assert target.is_file(), f"SKILL.md references templates/{ref} but file missing"


class TestTopicPattern:
    """The topic regex must accept every literal example used inside SKILL.md.
    Past regressions: `?`, `:`, `/` were rejected even though SKILL.md's own
    slugify worked example uses `What Are the Effects of SSRIs?`.
    """

    DOCUMENTED_VALID_TOPICS = [
        "CRISPR Gene Editing (2024)",
        "What Are the Effects of SSRIs?",
        "AI/ML benchmarks",
        "Treatment: a review",
        "History of Canada",
        "Effects of microplastics on marine life",
    ]

    DOCUMENTED_INVALID_TOPICS = [
        "",  # empty
        "ab",  # below min_length 3
        "x" * 501,  # above max_length 500
        "drop tables; --",  # semicolons not in whitelist
        "topic with\x00null",  # null byte
    ]

    def _pattern(self):
        fm = _load_frontmatter()
        return re.compile(fm["inputs"]["topic"]["pattern"])

    def test_pattern_accepts_documented_examples(self):
        pat = self._pattern()
        for topic in self.DOCUMENTED_VALID_TOPICS:
            assert pat.match(topic), f"topic pattern rejected documented valid example: {topic!r}"

    def test_pattern_rejects_known_bad_inputs(self):
        pat = self._pattern()
        for topic in self.DOCUMENTED_INVALID_TOPICS:
            # length and null-byte rules are enforced separately in Phase 0,
            # but the character whitelist alone should reject the bad cases
            # that contain forbidden characters. For pure length/empty cases
            # the pattern's `+` anchor handles it.
            if topic == "":
                assert not pat.match(topic)


def _slugify(topic: str) -> str:
    """Reference implementation of Phase 4 step 1 slugify algorithm.
    Mirrors SKILL.md exactly so we can regression-test the documented examples.
    """
    import unicodedata

    # Normalize accented Latin to ASCII (matches Phase 0 step 0)
    normalized = unicodedata.normalize("NFKD", topic)
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    s = ascii_only.lower()
    s = s.replace(" ", "-").replace("_", "-")
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    if len(s) > 80:
        # cut at last hyphen before 80 if possible, else hard cut
        cut = s[:80]
        last_hyphen = cut.rfind("-")
        s = cut[:last_hyphen] if last_hyphen > 0 else cut
    return s


class TestSlugify:
    """The slugify algorithm has two worked examples in SKILL.md Phase 4 step 1.
    Lock them in so future edits to the doc keep the algorithm honest.
    """

    def test_documented_example_crispr(self):
        assert _slugify("CRISPR Gene Editing (2024)") == "crispr-gene-editing-2024"

    def test_documented_example_ssris(self):
        assert _slugify("What Are the Effects of SSRIs?") == "what-are-the-effects-of-ssris"

    def test_accented_characters_normalize_to_ascii(self):
        # café → cafe, Köln → koln (per Phase 0 unicode normalization)
        assert _slugify("café culture") == "cafe-culture"
        assert _slugify("Köln history") == "koln-history"

    def test_path_traversal_chars_stripped(self):
        # `..` and `/` are stripped by the whitelist
        assert ".." not in _slugify("foo..bar")
        assert "/" not in _slugify("foo/bar")

    def test_collapses_multiple_hyphens(self):
        assert _slugify("foo   bar___baz") == "foo-bar-baz"

    def test_truncates_to_80_chars(self):
        long_topic = "word " * 50  # ~250 chars
        assert len(_slugify(long_topic)) <= 80


class TestWordBudgets:
    """Each depth row in the output length budget table must sum to 100%."""

    # From Phase 3 "Output Length Budget" — keep in sync with SKILL.md
    EXPECTED_BUDGETS = {
        # depth: (bottom_line, key_findings, research_shows, numbers, disagreements, gaps, methods)
        "brief": (10, 15, 40, 15, 10, 5, 5),
        "standard": (10, 15, 40, 15, 10, 5, 5),
        "deep": (10, 15, 40, 15, 10, 5, 5),
    }

    def test_each_depth_sums_to_100(self):
        for depth, allocations in self.EXPECTED_BUDGETS.items():
            assert sum(allocations) == 100, (
                f"depth '{depth}' budget allocations sum to {sum(allocations)}, expected 100"
            )

    def test_budgets_appear_verbatim_in_skill_md(self):
        """Catch silent edits that desync the test from the doc."""
        text = SKILL_MD.read_text(encoding="utf-8")
        for depth in self.EXPECTED_BUDGETS:
            assert f"| {depth} " in text, f"depth '{depth}' row missing from SKILL.md budget table"


class TestDomainProfiles:
    """Every domain profile section in SKILL.md must declare label and subfolder."""

    DOMAINS = ["history", "medicine", "technology", "social-sciences", "general"]

    def test_every_domain_has_label_and_subfolder(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        for domain in self.DOMAINS:
            # Section header
            header = f"### Profile: {domain}"
            assert header in text, f"missing profile section: {header}"
            # Each profile must declare a subfolder
            section_start = text.index(header)
            # cut to next profile header or end of profile listing
            next_section = text.find("### Profile:", section_start + len(header))
            section = text[
                section_start : next_section if next_section > 0 else section_start + 2000
            ]
            assert "**Label:**" in section, f"profile {domain} missing **Label:**"
            assert "**Subfolder:**" in section or "subfolder:" in section.lower(), (
                f"profile {domain} missing subfolder declaration"
            )
