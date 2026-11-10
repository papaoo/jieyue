"""Preview fixed backtest cards; only --send with the reviewed hash can send.

No .env/configuration loading, schema creation, outbox recovery or model calls.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.repositories.research_delivery_repository import ResearchDeliveryRepository
from src.services.research_delivery.backtest_delivery import BacktestDelivery
from src.services.research_delivery.credentials import CredentialVault


def existing_repository(path, *, readonly):
    """Same explicit existing-only SQLite pattern as revise_research_delivery.

    Do not call repository constructor: it creates tables and expands scope.
    """
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('existing_database_required')
    def connect():
        connection = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True)
        if readonly:
            connection.execute('PRAGMA query_only=ON')
        return connection
    engine = create_engine('sqlite://', creator=connect)
    repository = object.__new__(ResearchDeliveryRepository)
    repository.session_factory = sessionmaker(bind=engine)
    return repository, engine


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--vault', type=Path)
    parser.add_argument('--expected-review-hash')
    args = parser.parse_args(argv)
    if args.send and (not args.vault or not args.expected_review_hash):
        parser.error('--send requires --vault and --expected-review-hash from the reviewed preview')
    if not args.send and (args.vault or args.expected_review_hash):
        parser.error('preview takes no vault or approval hash')
    engine = None
    try:
        repository, engine = existing_repository(args.db, readonly=not args.send)
        delivery = BacktestDelivery(repository, CredentialVault(args.vault) if args.send else None)
        result = delivery.send(args.artifact_dir, args.expected_review_hash) if args.send else delivery.preview(args.artifact_dir)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2))
        if args.send:
            return 0 if all(r['status'] == 'sent' for r in result) else 2
        return 2 if result['blockers'] or not result['cards'] else 0
    except Exception:
        # Raw exceptions may include paths, SQL bindings or secrets; preview
        # validation returns its own precise allowlisted artifact gap codes.
        print(json.dumps(dict(error='backtest_delivery_invalid_input_or_review'), sort_keys=True))
        return 2
    finally:
        if engine:
            engine.dispose()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
