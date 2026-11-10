"""LOCAL ONLY saved-batch replay. No collection, publication, send or scheduling.

Default: SQLite mode=ro and pure renderers, no environment/model configuration.
--use-model alone permits the existing daily attempt ledger to change; every
other table remains write-denied. Output files are explicit, exclusive-create,
outside product sources. Historical observations never become current quotes.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.research_delivery.aggregation import canonical, content_hash
from src.services.research_delivery.brief_rendering import render_brief, normalize_brief_llm
from src.services.research_delivery.render_formal import formal_envelope
from src.services.research_delivery.research_brief import build_brief_input

from src.services.research_delivery.saved_research import SHANGHAI, _json, _source, read_batches, saved_context, augmented_source


def replay(source, related, *, top_n=5, title='研究简报', context=None, explain=None):
    source_hash = content_hash(source)
    frozen = augmented_source(source, related, top_n=top_n, context=context)
    day = source['business_date']
    compared = frozen['result']['research_context']['compared_strategies']
    envelope, detail = formal_envelope(frozen, top_n, format_version='research-brief-v1')
    brief_input = build_brief_input(envelope, detail, frozen)
    key = content_hash(dict(source_input_hash=source_hash, preview_input=frozen, top_n=top_n, format_version='research-brief-v1'))
    llm = explain(brief_input, key) if explain else dict(status='off', explanation=None)
    brief = render_brief(title, envelope, detail, llm, brief_input)
    llm = normalize_brief_llm(llm, brief)
    return dict(preview_only=True, publish_allowed=False, preview_label='本地预览 · 未发送 · 非正式发布',
        title=title, business_date=day, generated_at=datetime.now(SHANGHAI).isoformat(),
        source_input_hash=source_hash, preview_input_hash=content_hash(frozen), compared_strategies=sorted(compared),
        observations=[{key: entry[key] for key in ('source', 'endpoint', 'dataset', 'business_date', 'fetched_at', 'sha256') if key in entry}
            for entry in frozen['evidence']], envelope=envelope, detail=detail, brief_input=brief_input, llm=llm, **brief)


class LedgerOnly:
    """No repository constructor/schema/recovery; only existing ledger methods."""
    def __init__(self, path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from src.repositories.research_delivery_repository import ResearchDeliveryRepository

        def connect():
            connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=rw', uri=True)

            def authorize(action, table, column, database, trigger):
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                    return sqlite3.SQLITE_OK if table == 'research_delivery_llm_attempts' else sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table not in {'research_delivery_llm_attempts', 'sqlite_master'}:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and column is not None:
                    return sqlite3.SQLITE_DENY
                if action in {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
                              sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_CREATE_INDEX,
                              sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TRIGGER,
                              sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_DROP_TEMP_TABLE,
                              sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_ANALYZE}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            return connection

        self.engine = create_engine('sqlite://', creator=connect)
        self.session_factory = sessionmaker(bind=self.engine)
        self.atomic = lambda: ResearchDeliveryRepository.atomic(self)

    def reserve_llm(self, *args):
        from src.repositories.research_delivery_repository import ResearchDeliveryRepository
        return ResearchDeliveryRepository.reserve_llm(self, *args)

    def finish_llm(self, *args):
        from src.repositories.research_delivery_repository import ResearchDeliveryRepository
        return ResearchDeliveryRepository.finish_llm(self, *args)


def _output(path):
    path = Path(path).resolve()
    if path.exists() or (path.is_relative_to(ROOT) and path.relative_to(ROOT).parts[0] != '.superpowers'):
        raise ValueError('preview_output_must_be_new_and_outside_product_sources')
    if not path.parent.is_dir():
        raise ValueError('preview_output_parent_missing')
    return path


def _html(result):
    # Every dynamic cell is escaped, no Markdown/HTML execution or remote assets.
    date_note = '（历史观测，不代表当前行情）' if result['business_date'] < result['generated_at'][:10] else '（保存的观测，非实时行情）'
    audit = json.dumps(dict(observations=result['observations'], audit=result['detail'].get('audit', {}), llm=result['llm']), ensure_ascii=False, indent=2)
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>本地研究预览</title><style>
body{{max-width:860px;margin:24px auto;padding:0 20px;font:16px/1.9 system-ui;color:#15252d;background:#f5f7f8}}
article{{background:white;border:1px solid #dce2e6;border-radius:12px;padding:24px}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}}summary{{cursor:pointer}}
</style><body><article><strong>{escape(result['preview_label'])}</strong>
<h1>{escape(result['title'])}</h1><p>数据日期：{escape(result['business_date'])}{date_note}<br>
本次生成：{escape(result['generated_at'])}</p><pre>{escape(result['rendered_content'])}</pre>
<details><summary>来源观测时间与核验</summary><pre>{escape(audit)}</pre></details></article></body></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('--top-n', type=int, choices=range(1, 21), default=5)
    parser.add_argument('--title', default='研究简报')
    parser.add_argument('--evidence-dir', type=Path, help='Read/hash-verify saved stock_basic/daily captures only.')
    parser.add_argument('--reference-manifest', type=Path)
    parser.add_argument('--background-manifest', type=Path)
    parser.add_argument('--use-model', action='store_true', help='Explicitly allow existing daily ledger/budget and configured model; never sends.')
    parser.add_argument('--output-json', type=Path, required=True)
    parser.add_argument('--output-html', type=Path)
    args = parser.parse_args(argv)
    ledger = None
    try:
        outputs = [_output(path) for path in (args.output_json, args.output_html) if path]
        if len(set(outputs)) != len(outputs):
            raise ValueError('preview_output_paths_must_differ')
        source, related, metadata = read_batches(args.db, args.batch_id)
        context = None
        if args.evidence_dir or args.reference_manifest or args.background_manifest:
            context = saved_context(related, day=source['business_date'], now=datetime.now(SHANGHAI),
                evidence_dir=args.evidence_dir, reference_manifest=args.reference_manifest,
                background_manifest=args.background_manifest, bindings=metadata['verified_batches'])
        explain = None
        if args.use_model:
            from src.config import get_config
            from src.services.research_delivery.explanation import ExplanationService
            ledger = LedgerOnly(args.db)
            service = ExplanationService(ledger, get_config())
            explain = lambda brief, key: service.explain(brief, key, True, 'explain_only')
        result = replay(source, related, top_n=args.top_n, title=args.title, context=context, explain=explain)
        result['source_batch'] = {key: value for key, value in metadata.items() if key != 'verified_batches'}
        artifacts = [json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)]
        if args.output_html:
            artifacts.append(_html(result))
        for path, text in zip(outputs, artifacts):
            with path.open('x', encoding='utf-8') as stream:
                stream.write(text)
        print(canonical(dict(preview_only=True, publish_allowed=False, llm_status=result['llm']['status'], artifact_count=len(outputs))))
        return 0
    except Exception:
        # Do not expose DB/file paths, credentials, provider responses or exceptions.
        print(canonical(dict(preview_only=True, publish_allowed=False, error='preview_invalid_input_or_output')))
        return 2
    finally:
        if ledger:
            ledger.engine.dispose()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
