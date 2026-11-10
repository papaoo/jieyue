"""Explicit saved historical revision editions. Prepare/inspect never send.

Use one operator-chosen UUID for the entire edition, including all reentries.
Deliver requires the reviewed complete manifest hash; unknown outcomes never retry.
"""
from __future__ import annotations

import argparse
from datetime import date
from html import escape
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from src.repositories.research_delivery_models import ResearchPublication
from src.repositories.research_delivery_repository import ResearchDeliveryRepository, ManagementError
from src.services.research_delivery.aggregation import canonical
from src.services.research_delivery.credentials import CredentialVault
from src.services.research_delivery.subscriptions import ResearchDeliveryService
from src.services.research_delivery.revision_publisher import RevisionPublisher


def existing_repository(path, *, readonly):
    """No constructor, initializer, recovery, implicit DB creation, or vault IO."""
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


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'inspect', 'deliver'):
        command = commands.add_parser(name)
        for key in ('db', 'vault', 'output-dir'):
            command.add_argument('--' + key, type=Path, required=True)
        command.add_argument('--day', required=True)
        command.add_argument('--edition', required=True)
        if name == 'prepare':
            for key in ('evidence-dir', 'reference-manifest', 'background-manifest'):
                command.add_argument('--' + key, type=Path)
        if name == 'deliver':
            command.add_argument('--expected-manifest-hash', required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    engine = None
    try:
        if date.fromisoformat(args.day).isoformat() != args.day:
            raise ValueError('invalid_day')
        RevisionPublisher._edition(args.edition)
        output = args.output_dir.resolve()
        if output.exists() or not output.parent.is_dir():
            raise ValueError('new_output_directory_required')
        if output.is_relative_to(ROOT) and output.relative_to(ROOT).parts[0] != '.superpowers':
            raise ValueError('output_outside_product_required')
        repository, engine = existing_repository(args.db, readonly=args.command == 'inspect')
        from src.config import Config, get_config
        # Only explicit prepare can load model configuration. Inspect/deliver
        # cannot instantiate a model or read .env through configuration setup.
        config = get_config() if args.command == 'prepare' else Config()
        publisher = RevisionPublisher(ResearchDeliveryService(repository, CredentialVault(args.vault)), config)
        if args.command == 'prepare':
            with repository.session_factory() as session:
                session.execute(text('BEGIN'))
                scope = publisher._scope(session, args.day, set())
            for member in scope['members']:
                publisher.prepare(member['original_publication_id'], edition_id=args.edition,
                    evidence_dir=args.evidence_dir, reference_manifest=args.reference_manifest,
                    background_manifest=args.background_manifest)
        manifest = publisher.inspect(args.edition)
        if manifest['business_date'] != args.day:
            raise ValueError('edition_day_conflict')
        if args.command == 'deliver':
            if not manifest['complete'] or manifest['manifest_hash'] != args.expected_manifest_hash:
                raise ValueError('revision_manifest_conflict')
            for member in manifest['members']:
                publisher.deliver(member['publication_id'], edition_id=args.edition,
                    expected_manifest_hash=args.expected_manifest_hash)
            manifest = publisher.inspect(args.edition)
        reports = []
        with repository.session_factory() as session:
            for member in manifest['members']:
                pub = session.get(ResearchPublication, member['publication_id'])
                document = json.loads(pub.document_json)
                reports.append(dict(publication_id=pub.id, title=document['title'],
                    rendered_content=document['rendered_content'],
                    llm_status=document['llm']['status'], model_adoption=document.get('model_adoption')))
        output.mkdir()
        with (output / 'manifest.json').open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, allow_nan=False, indent=2)
        with (output / 'reports.html').open('x', encoding='utf-8') as stream:
            stream.write('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
                '<title>修订版研究报告核验</title><style>body{max-width:900px;margin:30px auto;font:16px/1.8 system-ui}'
                'pre{white-space:pre-wrap;overflow-wrap:anywhere}article{border-bottom:1px solid #ccc}</style>')
            for report in reports:
                stream.write('<article><h1>' + escape(report['title']) + '</h1><p>' + escape(report['publication_id'])
                    + ' · AI: ' + escape(report['llm_status']) + '</p><pre>' + escape(report['rendered_content']) + '</pre></article>')
        print(canonical(manifest))
        return 0
    except Exception as error:
        # Fixed codes only; never print provider exceptions, paths or bindings.
        print(canonical(dict(error=error.code if isinstance(error, ManagementError) else 'revision_invalid_input_or_output')))
        return 2
    finally:
        if engine:
            engine.dispose()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
