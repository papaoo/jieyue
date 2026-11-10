"""Explicit offline replay / separately opted-in bounded historical acquisition."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_provider.historical_reference import ReferenceBundle, day, hydrate_bundle
from src.services.screening.backtest import freeze_config, run_backtest
from src.services.screening.backtest_data import BatchStore, HistoricalDataset, Acquirer, acquire_market


def acquisition_client(credentials, factory=None):
    """Use only the explicit file's token/endpoint, without global env mutation."""
    from data_provider.tushare_fetcher import create_tushare_http_client
    token = credentials.get('TUSHARE_TOKEN') or credentials.get('TUSHARE_API_TOKEN')
    if not token:
        raise ValueError('historical_credentials_missing')
    url = ((credentials.get('TUSHARE_API_URL') or '').strip()
           or (credentials.get('TUSHARE_HTTP_URL') or '').strip() or 'http://api.tushare.pro')
    return (factory or create_tushare_http_client)(token, timeout=15, api_url=url)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--max-requests', type=int, default=1000)
    parser.add_argument('--hydrate-references', action='store_true')
    parser.add_argument('--workers', type=int, choices=(1, 2, 3, 4, 5, 6), help='opt-in spawned signal-date workers; omitted is serial')
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    config['output_dir'] = str(args.output_dir)
    if args.workers is not None:
        config['execution_workers'] = args.workers
    frozen, _ = freeze_config(config)  # Must precede bundle/client/data access.
    client = None
    if args.fetch:
        if args.env_file is None:
            parser.error('--fetch requires explicit --env-file (never loaded by calculation)')
        from dotenv import dotenv_values
        credentials = dotenv_values(args.env_file)
        client = acquisition_client(credentials)
    reference_requests = 0
    if args.hydrate_references:
        def fetch(url):
            if not args.fetch:
                raise ValueError('remote_reference_hydration_requires_fetch')
            import requests
            response = requests.get(url, timeout=30, allow_redirects=False)
            response.raise_for_status()
            if len(response.content) > 10_000_000:
                raise ValueError('reference_source_too_large')
            return response.content
        hydrated = hydrate_bundle(config['reference_plan'], config['reference_bundle'], client=client,
                                  fetch=fetch, max_requests=args.max_requests)
        reference_requests = getattr(hydrated, 'hydration_requests', 0)
    refs = ReferenceBundle.load(config['reference_bundle'])
    store = BatchStore(config['dataset_path'])
    try:
        if args.fetch:
            start = min(day(value) for value in frozen['start_dates']) - timedelta(days=300)
            acquire_market(Acquirer(store, client, max_requests=args.max_requests - reference_requests), refs,
                start, day(config['acquisition_end']), progress=lambda info: print(json.dumps(info)))
        result = run_backtest(config, HistoricalDataset(store, refs))
        print(json.dumps(dict(category=result['category'], completed=result['completed_cells'], planned=result['planned_cells'])))
    finally:
        store.close()


if __name__ == '__main__':
    main()
