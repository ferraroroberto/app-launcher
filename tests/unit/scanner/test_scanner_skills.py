"""src.scanner.scan_skills — life-os skill discovery (issue #102)."""

from __future__ import annotations

from pathlib import Path

from src.scanner import scan_skills, skills_dir_for


def _make_skill(
    life_os: Path,
    folder: str,
    *,
    name: str | None = None,
    description: str | None = None,
    desc_md: str | None = None,
    skill_md: bool = True,
) -> Path:
    """Create a skill folder with an optional frontmatter'd SKILL.md."""
    skill_dir = skills_dir_for(life_os) / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    if skill_md:
        fm = ["---"]
        if name is not None:
            fm.append(f"name: {name}")
        if description is not None:
            fm.append(f"description: {description}")
        fm.append("---")
        fm.append("")
        fm.append(f"# {folder}")
        (skill_dir / "SKILL.md").write_text("\n".join(fm), encoding="utf-8")
    if desc_md is not None:
        (skill_dir / "description.md").write_text(desc_md, encoding="utf-8")
    return skill_dir


class TestScanSkills:
    def test_lists_skill_dirs_sorted(self, tmp_path: Path):
        _make_skill(tmp_path, "sparring-work", name="sparring-work")
        _make_skill(tmp_path, "journal-daily", name="journal-daily")
        found = scan_skills(tmp_path)
        assert [s.id for s in found] == ["journal-daily", "sparring-work"]

    def test_excludes_underscore_prefixed(self, tmp_path: Path):
        _make_skill(tmp_path, "_template", name="template")
        _make_skill(tmp_path, "_recap", name="recap")
        _make_skill(tmp_path, "alt-text", name="alt-text")
        found = scan_skills(tmp_path)
        assert [s.id for s in found] == ["alt-text"]

    def test_frontmatter_name_and_description(self, tmp_path: Path):
        _make_skill(
            tmp_path, "journal-daily",
            name="journal-daily", description="Turns a transcript into a journal.",
        )
        found = scan_skills(tmp_path)
        assert found[0].name == "journal-daily"
        assert found[0].command == "journal-daily"
        assert found[0].description == "Turns a transcript into a journal."

    def test_falls_back_to_description_md(self, tmp_path: Path):
        _make_skill(
            tmp_path, "ip-check", name="ip-check",
            desc_md="# heading\n\nChecks IP reputation thoroughly.\n",
        )
        found = scan_skills(tmp_path)
        # description.md's first non-heading paragraph wins when frontmatter
        # has no description.
        assert found[0].description == "Checks IP reputation thoroughly."

    def test_missing_skill_md_uses_folder_name(self, tmp_path: Path):
        _make_skill(tmp_path, "roast-posts", skill_md=False)
        found = scan_skills(tmp_path)
        assert found[0].id == "roast-posts"
        assert found[0].name == "roast-posts"
        assert found[0].command == "roast-posts"

    def test_unsafe_frontmatter_name_falls_back_to_folder(self, tmp_path: Path):
        # A frontmatter name with spaces / shell metachars is rejected as a
        # slash-command; the safe folder name is used instead.
        _make_skill(tmp_path, "meeting-prep", name="rm -rf /")
        found = scan_skills(tmp_path)
        assert found[0].command == "meeting-prep"

    def test_missing_skills_dir_returns_empty(self, tmp_path: Path):
        assert scan_skills(tmp_path / "no-life-os") == []
