# run_extraction.py
#
# CLI for running CPQ extraction with local-first run folder layout and status tracking:
#   python run_extraction.py --instance <name> [--run-id X] [--fixture <path>] [--use-bulk]

import os
import sys
import json
import argparse
import logging
from datetime import datetime

import sf_auth
import extractor
import transformer
import storage
import config
from synthetic_data import generate_synthetic_csvs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def update_status(run_dir: str, state: str, step: str = "", progress: float = 0.0, counts: dict = None, error: str = None):
    os.makedirs(run_dir, exist_ok=True)
    status_file = os.path.join(run_dir, "status.json")
    data = {
        "state": state,
        "current_step": step,
        "progress": progress,
        "counts": counts or {},
        "error": error,
        "updated_at": datetime.now().isoformat(),
        "run_dir": run_dir,
    }
    tmp_file = status_file + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_file, status_file)


def run_extraction_cli(instance_name: str = None, run_id: str = None, fixture_path: str = None,
                       use_bulk: bool = False, credentials: dict = None,
                       include_inactive: bool = config.INCLUDE_INACTIVE_RECORDS):
    run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    instance_label = instance_name or (os.path.basename(fixture_path) if fixture_path else "default")
    run_dir = os.path.abspath(os.path.join("data", "runs", instance_label, run_id))
    raw_dir = os.path.join(run_dir, "raw")
    out_dir = os.path.join(run_dir, "output")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    update_status(run_dir, "running", step="Initializing extraction...", progress=0.05)

    try:
        # Fixture mode
        if fixture_path:
            logger.info(f"Running in fixture mode from: {fixture_path}")
            update_status(run_dir, "running", step=f"Loading fixture data from {fixture_path}...", progress=0.2)
            if os.path.isdir(fixture_path):
                # Copy raw CSVs
                import shutil
                for f in os.listdir(fixture_path):
                    if f.endswith(".csv"):
                        shutil.copy2(os.path.join(fixture_path, f), os.path.join(raw_dir, f))
            elif fixture_path.endswith(".json"):
                # Generate raw CSVs from JSON
                try:
                    with open(fixture_path, "r", encoding="utf-8") as f:
                        f_data = json.load(f)
                except Exception:
                    with open(fixture_path, "r", encoding="utf-16") as f:
                        f_data = json.load(f)
                with open(os.path.join(out_dir, "cpq_extract.json"), "w", encoding="utf-8") as f:
                    json.dump(f_data, f, indent=2)

        # Salesforce connection mode
        else:
            if not credentials:
                # Check environment for credentials
                env_auth = os.environ.get("SF_AUTH_CONFIG")
                if env_auth:
                    credentials = json.loads(env_auth)
                else:
                    auth_method = os.environ.get("SF_AUTH_METHOD", "password")
                    if auth_method == "client_credentials":
                        credentials = {
                            "auth_method": "client_credentials",
                            "client_id": os.environ.get("SF_CLIENT_ID"),
                            "client_secret": os.environ.get("SF_CLIENT_SECRET"),
                            "domain": os.environ.get("SF_DOMAIN", "login"),
                        }
                    else:
                        credentials = {
                            "auth_method": "password",
                            "username": os.environ.get("SF_USERNAME"),
                            "password": os.environ.get("SF_PASSWORD"),
                            "security_token": os.environ.get("SF_SECURITY_TOKEN", ""),
                            "domain": os.environ.get("SF_DOMAIN", "login"),
                        }

            update_status(run_dir, "running", step="Connecting to Salesforce...", progress=0.1)
            sf = sf_auth.connect(credentials)

            extract_log = {}
            data, counts = extractor.extract_all(
                sf=sf,
                progress_callback=lambda msg: update_status(run_dir, "running", step=msg, progress=0.4),
                run_dir=run_dir,
                use_bulk=use_bulk,
                extract_log=extract_log,
                include_inactive=include_inactive,
            )

        # Transform and stream output
        update_status(run_dir, "running", step="Transforming and streaming outputs with DuckDB...", progress=0.7)
        counts, trunc_log = transformer.transform_run(
            run_dir=run_dir,
            progress_callback=lambda msg: update_status(run_dir, "running", step=msg, progress=0.85),
        )

        update_status(run_dir, "completed", step="Extraction and export complete.", progress=1.0, counts=counts)
        logger.info(f"Run completed successfully at: {run_dir}. Counts: {counts}")
        return run_dir

    except Exception as e:
        logger.error(f"Extraction run failed: {e}", exc_info=True)
        update_status(run_dir, "failed", step=f"Failed: {e}", progress=1.0, error=str(e))
        raise


def main():
    parser = argparse.ArgumentParser(description="Salesforce CPQ Extractor CLI")
    parser.add_argument("--instance", help="Salesforce instance name")
    parser.add_argument("--run-id", help="Optional run identifier (defaults to timestamp)")
    parser.add_argument("--fixture", help="Offline fixture path (.json or raw directory)")
    parser.add_argument("--use-bulk", action="store_true", help="Use Bulk API 2.0 when possible")
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        default=config.INCLUDE_INACTIVE_RECORDS,
        help="Include inactive products (excluded by default).",
    )

    args = parser.parse_args()

    try:
        run_extraction_cli(
            instance_name=args.instance,
            run_id=args.run_id,
            fixture_path=args.fixture,
            use_bulk=args.use_bulk,
            include_inactive=args.include_inactive,
        )
    except Exception as e:
        sys.exit(1)


if __name__ == "__main__":
    main()
