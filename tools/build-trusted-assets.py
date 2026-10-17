"""Seal an internal T032 private-test manifest into the FocusMate SQLite store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from jsonschema import ValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.api.problem_storage import DEFAULT_DB_PATH, StorageFailure  # noqa: E402
from apps.api.trusted_assets import (  # noqa: E402
    OracleEvaluationFailure,
    TrustedAssetBuilder,
)
from apps.api.verifier_assets import VerifierAssetRepository  # noqa: E402
from services.oj_worker.docker_runner import DockerPythonRunner  # noqa: E402
from services.verifier_builder.docker_oracle import DockerOracleEvaluator  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build private expected outputs and seal T032 test material.",
    )
    parser.add_argument("manifest", type=Path, help="Internal trusted-build manifest JSON")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "oracle-workspaces",
        help="Temporary host workspace used by the isolated Oracle runner",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        evaluator = DockerOracleEvaluator(DockerPythonRunner(), args.workspace_root)
        result = TrustedAssetBuilder(args.database, evaluator=evaluator).build(manifest)
        ref = result["problemRef"]
        asset_status = VerifierAssetRepository(args.database).get(
            ref["problemId"], ref["problemVersion"],
        )["status"]
    except FileNotFoundError:
        return _error("MANIFEST_NOT_FOUND", "The trusted-build manifest was not found.")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error("MANIFEST_JSON_INVALID", "The trusted-build manifest is not valid UTF-8 JSON.")
    except ValidationError:
        return _error("MANIFEST_SCHEMA_INVALID", "The trusted-build manifest does not match its schema.")
    except (StorageFailure, OracleEvaluationFailure) as failure:
        return _error(failure.code, failure.message)
    except Exception:
        return _error(
            "TRUSTED_BUILD_INTERNAL_ERROR",
            "Trusted material could not be built safely.",
        )

    # Deliberately exclude every stdin, expected output, and Oracle source.
    print(json.dumps({
        "manifestId": manifest["manifestId"],
        "problemRef": result["problemRef"],
        "verifierAssetRef": result["verifierAssetRef"],
        "status": asset_status,
        "publicTestCount": result["publicTestCount"],
        "privateTestCount": result["privateTestCount"],
        "testCount": result["testCount"],
        "buildMethod": result["buildMethod"],
        "materialHash": result["materialHash"],
    }, ensure_ascii=False))
    return 0


def _error(code: str, message: str) -> int:
    print(json.dumps({"status": "failed", "code": code, "message": message}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
