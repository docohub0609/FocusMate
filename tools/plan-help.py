"""T072 independent entry; uses the same persistent Manager as diagnosis."""
from pathlib import Path
import runpy

if __name__=='__main__':
    entry=runpy.run_path(str(Path(__file__).with_name('diagnose-help.py')))
    raise SystemExit(entry['main'](plan_action=True))
