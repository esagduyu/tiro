"""Persona format, loader, scope contexts, interpolation, PersonaAgent
(Phase 6 K3). Adversarial cases live in test_personas_adversarial.py."""

import pytest

VALID_BODY = "Consider this piece:\n\n{{article}}\n\nAnd its highlights:\n{{highlights}}\n\nSteelman the opposing view."


def write_persona(config, slug="devils-advocate", *, scope="article",
                  output="note", body=VALID_BODY, extra_fm="", name=None):
    from tiro.agents.personas import personas_dir

    pdir = personas_dir(config)
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{slug}.md"
    path.write_text(
        "---\n"
        f"name: {name or slug}\n"
        f"scope: {scope}\n"
        f"output: {output}\n"
        f"{extra_fm}"
        "---\n\n"
        f"{body}\n"
    )
    return path


def test_parse_valid_persona_with_defaults(test_config):
    from tiro.agents.personas import parse_persona

    p = parse_persona(write_persona(test_config))
    assert p.slug == "devils-advocate"
    assert p.name == "devils-advocate"
    assert p.scope == "article" and p.output == "note"
    assert p.version == "1" and p.schedule == "manual" and p.tier == "light"
    assert "{{article}}" in p.body


def test_parse_explicit_frontmatter(test_config):
    from tiro.agents.personas import parse_persona

    path = write_persona(
        test_config, "themes", scope="day", output="digest_section",
        body="Themes across:\n{{day_articles}}",
        extra_fm="version: '2'\nschedule: cron\ntier: heavy\n")
    p = parse_persona(path)
    assert (p.version, p.schedule, p.tier) == ("2", "cron", "heavy")


@pytest.mark.parametrize("mutation, match", [
    ({"scope": "galaxy"}, "scope"),
    ({"output": "tweet"}, "output"),
    ({"extra_fm": "tier: enormous\n"}, "tier"),
    ({"extra_fm": "schedule: hourly\n"}, "schedule"),
    ({"output": "note", "scope": "day",
      "body": "x {{day_articles}}"}, "output 'note'"),      # kind x scope
    ({"body": "Read {{config}} please {{article}}"}, "unknown placeholder"),
    ({"body": "See {{day_articles}} {{article}}"},
     "not available in scope"),                             # wrong-scope ph
    ({"slug": "Bad_Slug"}, "slug"),
])
def test_load_errors_no_partial_render(test_config, mutation, match):
    from tiro.agents.personas import PersonaLoadError, parse_persona

    kwargs = dict(mutation)
    slug = kwargs.pop("slug", "broken")
    path = write_persona(test_config, slug, **kwargs)
    with pytest.raises(PersonaLoadError, match=match):
        parse_persona(path)


def test_missing_required_keys(test_config):
    from tiro.agents.personas import PersonaLoadError, parse_persona

    pdir = test_config.library / "personas"
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / "nameless.md"
    path.write_text("---\nscope: article\n---\n\n{{article}}\n")
    with pytest.raises(PersonaLoadError, match="name"):
        parse_persona(path)


def test_load_personas_partitions_valid_and_broken(test_config):
    from tiro.agents.personas import load_personas

    write_persona(test_config, "good")
    write_persona(test_config, "bad", scope="galaxy")
    personas, errors = load_personas(test_config)
    assert [p.slug for p in personas] == ["good"]
    assert "bad" in errors and "scope" in errors["bad"]


def test_ensure_personas_copies_once_never_overwrites(test_config):
    from tiro.agents.personas import ensure_personas, personas_dir

    ensure_personas(test_config)
    pdir = personas_dir(test_config)
    slugs = sorted(p.stem for p in pdir.glob("*.md"))
    assert slugs == ["daily-themes", "devils-advocate", "research-brief"]
    edited = pdir / "devils-advocate.md"
    edited.write_text(edited.read_text() + "\nUSER EDIT\n")
    ensure_personas(test_config)
    assert "USER EDIT" in edited.read_text()      # never overwritten


def test_packaged_defaults_all_parse(test_config):
    from tiro.agents.personas import ensure_personas, load_personas

    ensure_personas(test_config)
    personas, errors = load_personas(test_config)
    assert errors == {}
    by_slug = {p.slug: p for p in personas}
    assert by_slug["devils-advocate"].scope == "article"
    assert by_slug["daily-themes"].scope == "day"
    assert by_slug["research-brief"].scope == "query"
