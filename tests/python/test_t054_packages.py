"""Nine-package selective loading acceptance; semantic quality uses real evaluations."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from focusmate.core.skill_sections import load_index, load_sections
from focusmate.contracts.skill_files import PackageError

PACKAGES = sorted(path for path in (ROOT / "skills").iterdir() if path.is_dir())


@pytest.mark.parametrize("package", PACKAGES, ids=lambda p: p.name)
def test_every_package_has_complete_bounded_index(package):
    descriptor = json.loads((package / "descriptor.json").read_text(encoding="utf-8"))
    index = load_index(package, descriptor["version"], descriptor["hash"])
    assert 2 <= len(index.sections) <= 4
    assert len({section.section_id for section in index.sections}) == len(index.sections)
    assert {section.file_ref for section in index.sections} == {
        path.relative_to(package).as_posix() for path in (package / "references").glob("*.md")
    }
    for section in index.sections:
        loaded = load_sections(index, [section.section_id])
        assert set(loaded["sections"]) == {section.section_id}
        assert loaded["sections"][section.section_id].strip()
    two = [section.section_id for section in index.sections[:2]]
    assert set(load_sections(index, two)["sections"]) == set(two)
    with pytest.raises(PackageError):
        load_sections(index, two + [two[0]])
    with pytest.raises(PackageError):
        load_sections(index, [two[0], two[0]])


def test_nine_packages_are_present():
    assert len(PACKAGES) == 9
