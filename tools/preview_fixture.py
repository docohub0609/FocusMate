"""Authored README example: isolated database, no model calls or user history."""
import json
import os
from pathlib import Path
from apps.api.__main__ import create_server
from apps.api.problem_storage import ProblemRepository
from tools.experiments.t082_backend_probe import SMOKE, seed

folder = Path(os.environ['FOCUSMATE_DB_PATH']).parent / 'preview-data'
service, goal, session = seed(folder, dict(SMOKE))
server = create_server(port=0, problem_repository=ProblemRepository(service.path))
print(json.dumps({'port': server.server_port}), flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
