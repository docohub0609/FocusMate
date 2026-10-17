"""Active package identities must also load through the section boundary."""
import json
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from focusmate.core.skill_sections import load_index, load_sections

PACKAGES = sorted(p for p in Path('skills').iterdir() if p.is_dir())

@pytest.mark.parametrize('package', PACKAGES, ids=lambda p: p.name)
def test_active_version_can_load_every_section(package):
    descriptor = json.loads((package / 'descriptor.json').read_text(encoding='utf-8'))
    index = json.loads((package / 'references/index.json').read_text(encoding='utf-8'))
    snapshot = load_index(package, descriptor['version'], descriptor['hash'])
    for section in index['sections']:
        load_sections(snapshot, [section['sectionId']])
