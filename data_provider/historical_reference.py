"""Publication-lagged CAPCO and exchange calendars shared by live/replay.

No network, environment or provider-name fallback. Only pure date parsing is cached. Bundles are
explicitly hydrated by acquisition; raw source vintages are never restamped.
"""
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
from functools import lru_cache

from src.services.screening.eligibility import listing_eligibility

REFERENCE_VERSION = 'capco-major-publication-v1'


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def day(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value)
    return _parse_day_string(text, '%Y%m%d' if len(text) == 8 else '%Y-%m-%d')


@lru_cache(maxsize=4096)
def _parse_day_string(text, fmt):
    return datetime.strptime(text, fmt).date()


def normalize_aliases(rows):
    """Official API uses o_code/n_code, never inferred prefix replacement."""
    aliases = {}
    for row in rows:
        old, new = str(row['o_code']).split('.')[0], str(row['n_code']).split('.')[0]
        if not re.fullmatch(r'\d{6}', old) or not re.fullmatch(r'\d{6}', new):
            raise ValueError('reference_alias_invalid')
        if old in aliases and aliases[old] != new:
            raise ValueError('reference_alias_conflict')
        aliases[old] = new
    if len(set(aliases.values())) != len(aliases):
        raise ValueError('reference_alias_many_to_one')
    return aliases


def parse_capco_pages(pages):
    """Decode wrapped eight-column rows; manufacturing secondary != major.

    Every six-digit row token must parse exactly once. Headers/footers are
    removed per page, not by swallowing a broken row. No names imply ST state.
    """
    rows = {}
    for page in pages:
        page = re.sub(r'20\d{2}年[上下]半年上市公司行业分类结果', '', page)
        starts = list(re.finditer(r'(?<!\d)(\d{6})(?!\d)', page))
        for index, match in enumerate(starts):
            text = page[match.end():starts[index + 1].start() if index + 1 < len(starts) else len(page)]
            category = re.search(r'\s([A-S])\s', text)
            if category is None:
                raise ValueError('capco_category_extraction_failed')
            tail = text[category.end():]
            majors = list(re.finditer(r'(?<![\dA-Z])(\d{2})(?!\d)', tail))
            if len(majors) != 1:
                raise ValueError('capco_major_extraction_failed')
            major = majors[0]
            label = re.sub(r'\s+', '', tail[major.end():])
            # Printed page numbers, if present, are not part of a label.
            label = re.sub(r'\d+$', '', label)
            if not label or '代码' in label or '简称' in label:
                raise ValueError('capco_label_extraction_failed')
            code = match[1]
            if code in rows:
                raise ValueError('capco_duplicate_security')
            rows[code] = dict(industry_code=category[1] + major[1], industry=label)
    if not rows:
        raise ValueError('capco_empty')
    return rows


def parse_capco_pdf(path):
    from pypdf import PdfReader
    return parse_capco_pages(page.extract_text() for page in PdfReader(path).pages)


def closure_calendar(year, closures):
    """Explicit annual notice closures plus weekend rule; no other exchange."""
    closed = set()
    for start, end in closures:
        start, end = day(start), day(end)
        if start.year != year or end.year != year or start > end:
            raise ValueError('calendar_closure_range_invalid')
        closed.update(start + timedelta(days=i) for i in range((end - start).days + 1))
    start = date(year, 1, 1)
    return {(start + timedelta(days=i)).isoformat(): int((start + timedelta(days=i)).weekday() < 5
            and start + timedelta(days=i) not in closed)
            for i in range((date(year + 1, 1, 1) - start).days)}


def parse_bse_annual(html, year):
    """Parse only the visible official notice body, not duplicated share text."""
    from bs4 import BeautifulSoup
    node = BeautifulSoup(html, 'html.parser').select_one('div.in_main.clearfix')
    if node is None:
        raise ValueError('bse_notice_body_missing')
    text = re.sub(r'\s+', '', node.get_text())
    if f'北京证券交易所{year}年部分节假日休市安排' not in text or '二、' not in text:
        raise ValueError('bse_notice_year_or_sections_invalid')
    section = text.split('一、休市安排', 1)[1].split('二、', 1)[0]
    parts = re.split(r'（[一二三四五六七八九]+）', section)[1:]
    if len(parts) not in (6, 7) or not all(label in section for label in ('元旦', '春节', '清明节', '劳动节', '端午节', '中秋节', '国庆节')):
        raise ValueError('bse_notice_holiday_sections_missing')
    closures = []
    pattern = r'(?:\d{4}年)?(\d{1,2})月(\d{1,2})日（星期[一二三四五六日天]）(?:至(\d{1,2})月(\d{1,2})日（星期[一二三四五六日天]）)?休市'
    for part in parts:
        match = re.search(pattern, part)
        if match is None:
            raise ValueError('bse_notice_closure_parse_failed')
        start = date(year, int(match[1]), int(match[2]))
        end = date(year, int(match[3]), int(match[4])) if match[3] else start
        closures.append([start.isoformat(), end.isoformat()])
    return closures


def validate_bse_weekday_rule(html):
    from bs4 import BeautifulSoup
    node = BeautifulSoup(html, 'html.parser').select_one('div.in_main.clearfix')
    text = re.sub(r'\s+', '', node.get_text()) if node is not None else ''
    if '2.3.1本所交易日为每周一至周五。国家法定节假日和本所公告的休市日，本所市场休市。' not in text:
        raise ValueError('bse_weekday_rule_unverified')


class ReferenceBundle:
    def __init__(self, payload):
        if payload.get('version') != REFERENCE_VERSION:
            raise ValueError('reference_bundle_version_invalid')
        self.payload = payload
        self.hash = digest(payload)
        self.aliases = normalize_aliases(payload['aliases'])
        self.reports = sorted(payload['reports'], key=lambda r: r['published_on'])
        self.calendars = payload['calendars']
        self._report_rows = {}
        self._calendar_cache = {}
        if not self.reports or len({r['period'] for r in self.reports}) != len(self.reports):
            raise ValueError('reference_reports_invalid')
        for report in self.reports:
            period = re.fullmatch(r'(20\d{2})H([12])', report['period'])
            cutoff = day(report['inclusion_cutoff'])
            if (period is None or cutoff != date(int(period[1]), 6 if period[2] == '1' else 12,
                                                10 if period[2] == '1' else 20)
                    or not cutoff < day(report['published_on']) < day(report['valid_through'])):
                raise ValueError('reference_publication_or_cutoff_invalid')
            for field in ('pdf_sha256', 'article_sha256', 'guideline_sha256'):
                if not re.fullmatch('[a-f0-9]{64}', report[field]):
                    raise ValueError('reference_source_hash_invalid')
            if not report['rows'] or not report.get('guideline_sha256'):
                raise ValueError('reference_report_incomplete')
            for value in report['rows'].values():
                if not re.fullmatch('[A-S][0-9]{2}', value['industry_code']) or not value['industry']:
                    raise ValueError('reference_classification_invalid')
            normalized = {self.canonical(key): value for key, value in report['rows'].items()}
            if len(normalized) != len(report['rows']):
                raise ValueError('classification_alias_conflict')
            self._report_rows[report['period']] = normalized

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        try:
            manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
            raw = (directory / 'bundle.json').read_bytes()
            if sha256(raw).hexdigest() != manifest['bundle_sha256']:
                raise ValueError('hash mismatch')
            for name, entry in manifest['sources'].items():
                path = directory / name
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise ValueError('source path invalid')
                if sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                    raise ValueError('source hash mismatch')
                stamp = datetime.fromisoformat(entry['fetched_at'])
                if stamp.utcoffset() is None or not entry['url'].startswith('https://'):
                    raise ValueError('source vintage missing')
            bundle = cls(json.loads(raw))
            bundle.source_vintages = manifest['sources']
            hashes = {entry['sha256'] for entry in manifest['sources'].values()}
            needed = {r[k] for r in bundle.reports for k in ('pdf_sha256', 'article_sha256', 'guideline_sha256')}
            needed.update(c['source_sha256'] for c in bundle.calendars)
            needed.update(c['rule_sha256'] for c in bundle.calendars if 'rule_sha256' in c)
            needed.update(h for c in bundle.calendars for h in c.get('additional_rule_sha256', []))
            needed.add(bundle.payload['aliases_sha256'])
            if not needed <= hashes:
                raise ValueError('missing raw source parents')
            return bundle
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise ValueError('reference_bundle_missing_or_invalid: hydrate verified bundle before strict screening') from exc

    def canonical(self, code):
        code = str(code).split('.')[0]
        return self.aliases.get(code, code)

    def calendar(self, exchange, start, end):
        start, end = day(start), day(end)
        cache_key = (exchange, start, end)
        if cache_key in self._calendar_cache:
            return self._calendar_cache[cache_key]
        values, parents = {}, []
        for item in self.calendars:
            if item['exchange'] != exchange:
                continue
            if item['derivation'] == 'official_annual_closures_weekends_v1':
                data = closure_calendar(item['year'], item['closures'])
            elif item['derivation'] == 'exchange_trade_cal_v1':
                data = item['days']
            else:
                raise ValueError('calendar_derivation_unknown')
            for key, value in data.items():
                parsed = day(key)
                if start <= parsed <= end:
                    if type(value) is not int or value not in (0, 1) or (parsed in values and values[parsed] != value):
                        raise ValueError('calendar_conflict')
                    values[parsed] = value
            parents.append(item['source_sha256'])
        if len(values) != (end - start).days + 1:
            raise ValueError('calendar_year_coverage_missing')
        result = values, digest(dict(exchange=exchange, start=start.isoformat(), end=end.isoformat(), parents=parents))
        # A bounded signal window, not an unbounded process-wide cache.
        if len(self._calendar_cache) >= 32:
            self._calendar_cache.clear()
        self._calendar_cache[cache_key] = result
        return result

    def classify(self, code, listed, signal, exchange):
        listed, signal = day(listed), day(signal)
        applicable = [r for r in self.reports if day(r['published_on']) < signal <= day(r['valid_through'])]
        if not applicable:
            raise ValueError('published_industry_report_unavailable')
        report = applicable[-1]
        calendar, _ = self.calendar(exchange, day(report['published_on']) + timedelta(days=1), signal)
        if calendar[signal] != 1:
            raise ValueError('signal_is_not_exchange_session')
        effective = min(d for d, opened in calendar.items() if opened)
        rows = self._report_rows[report['period']]
        value = rows.get(self.canonical(code))
        if value is None:
            if listed >= day(report['inclusion_cutoff']):
                return dict(eligible=False, reason='ineligible_no_published_classification',
                            cutoff=report['inclusion_cutoff'], period=report['period'],
                            listing_date=listed.isoformat(), reference_hash=self.hash)
            raise ValueError('seasoned_security_classification_missing')
        return dict(eligible=True, **value, industry_taxonomy='capco_major_v1',
                    industry_as_of=effective.isoformat(), industry_source='CAPCO',
                    industry_evidence_id=digest(dict(bundle=self.hash, period=report['period'],
                        code=self.canonical(code), effective=effective.isoformat())))

    def eligibility(self, code, listed, signal, exchange, listing_evidence, *, name_status=None):
        signal = day(signal)
        calendar, evidence = self.calendar(exchange, signal - timedelta(days=150), signal)
        result = listing_eligibility(day(listed), signal, calendar, exchange=exchange,
            listing_evidence_id=listing_evidence, calendar_evidence_id=evidence)
        if not result['eligible']:
            return result
        if name_status in {'st', 'star_st', 'delisting_arrangement'}:
            return dict(result, eligible=False, reason='excluded_' + name_status)
        return dict(result, **self.classify(code, listed, signal, exchange))


def name_state(rows, signal):
    """Inclusive effective interval plus publication gate; unknown stays unknown."""
    signal = day(signal)
    active, unpublished = [], False
    for row in {digest(r): r for r in rows}.values():
        if day(row['start_date']) <= signal and (not row.get('end_date') or signal <= day(row['end_date'])):
            if not row.get('ann_date') or day(row['ann_date']) > signal:
                unpublished = True
                continue
            name = row.get('name', '').strip()
            if not name:
                unpublished = True
                continue
            status = ('delisting_arrangement' if '退' in name else
                      'star_st' if re.search(r'[*＊]ST', name, re.I) else
                      'st' if re.search('ST', name, re.I) else 'normal')
            active.append((name, status))
    if unpublished:
        raise ValueError('name_publication_unknown_or_late')
    if not active:
        raise ValueError('name_interval_missing')
    if len({value[1] for value in active}) != 1:
        raise ValueError('name_interval_conflict')
    return dict(name=sorted(active)[0][0], status=active[0][1], eligible=active[0][1] == 'normal')


def hydrate_bundle(plan, directory, *, client=None, fetch=None, max_requests=50):
    """Explicit hydration from an audited source plan, retaining original times.

    ``fetch(url) -> bytes`` is supplied only by the opt-in CLI. Local sources
    require their original fetched_at; copying bytes never re-dates evidence.
    The caller freezes the plan before invoking this function.
    """
    from datetime import timezone
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / 'manifest.json').exists():
        existing = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if existing.get('plan_sha256') != digest(plan):
            raise ValueError('reference_plan_changed_use_new_bundle_directory')
        return ReferenceBundle.load(directory)
    sources, contents, requests = {}, {}, 0
    for name, spec in plan['sources'].items():
        if Path(name).name != name:
            raise ValueError('reference_source_filename_invalid')
        destination = directory / name
        receipt_path = directory / (name + '.receipt.json')
        if destination.exists() and receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
            raw = destination.read_bytes()
            if receipt['url'] != spec['url'] or receipt['sha256'] != sha256(raw).hexdigest():
                raise ValueError('immutable_reference_source_conflict')
            fetched_at = receipt['fetched_at']
        elif 'path' in spec:
            raw = Path(spec['path']).read_bytes()
            fetched_at = spec['fetched_at']
        elif 'api' in spec:
            if client is None or requests >= max_requests or spec['api'] not in {'trade_cal', 'bse_mapping'}:
                raise ValueError('reference_api_budget_or_client_missing')
            requests += 1
            fields = 'exchange,cal_date,is_open' if spec['api'] == 'trade_cal' else 'name,o_code,n_code,list_date'
            try:
                table = client.query(spec['api'], fields=fields, request_timeout=15, **spec['params'])
            except Exception:
                raise ValueError('reference_api_request_failed') from None
            if set(table.columns) != set(fields.split(',')) or not 0 < len(table) < 5000:
                raise ValueError('reference_api_schema_or_truncation')
            raw = table.to_json(orient='records', force_ascii=False).encode()
            fetched_at = datetime.now(timezone.utc).isoformat()
        else:
            if fetch is None or requests >= max_requests:
                raise ValueError('reference_hydration_request_budget_or_fetch_missing')
            requests += 1
            raw = fetch(spec['url'])
            fetched_at = datetime.now(timezone.utc).isoformat()
        hashed = sha256(raw).hexdigest()
        if spec.get('sha256') and hashed != spec['sha256']:
            raise ValueError('reference_source_hash_mismatch')
        if destination.exists() and destination.read_bytes() != raw:
            raise ValueError('immutable_reference_source_conflict')
        if not destination.exists():
            destination.write_bytes(raw)
        sources[name] = dict(sha256=hashed, fetched_at=fetched_at, url=spec['url'])
        receipt_path.write_text(json.dumps(sources[name]), encoding='utf-8')
        contents[name] = raw
    reports = []
    for spec in plan['reports']:
        rows = parse_capco_pdf(directory / spec['pdf'])
        if len(rows) != spec['expected_rows']:
            raise ValueError('reference_report_row_count_mismatch')
        reports.append(dict(period=spec['period'], published_on=spec['published_on'],
            inclusion_cutoff=spec['inclusion_cutoff'], valid_through=spec['valid_through'],
            pdf_sha256=sources[spec['pdf']]['sha256'], article_sha256=sources[spec['article']]['sha256'],
            guideline_sha256=sources[plan['guideline']]['sha256'], rows=rows))
    calendars = []
    for spec in plan['bse_calendars']:
        rules = [spec['rule'], *spec.get('additional_rules', [])]
        for rule in rules:
            validate_bse_weekday_rule(contents[rule])
        calendars.append(dict(exchange='BSE', year=spec['year'], published_on=spec['published_on'],
            rule_sha256=sources[spec['rule']]['sha256'], source_sha256=sources[spec['notice']]['sha256'],
            additional_rule_sha256=[sources[name]['sha256'] for name in spec.get('additional_rules', [])],
            derivation='official_annual_closures_weekends_v1',
            closures=parse_bse_annual(contents[spec['notice']], spec['year'])))
    # API captures can also be supplied as local JSON sources for offline hydration.
    alias_name = plan['aliases']
    aliases = json.loads(contents[alias_name])
    normalize_aliases(aliases)
    for spec in plan['exchange_calendars']:
        rows = json.loads(contents[spec['source']])
        if any(row['exchange'] != spec['exchange'] for row in rows):
            raise ValueError('reference_calendar_exchange_conflict')
        dates = [day(row['cal_date']).isoformat() for row in rows]
        if len(dates) != len(set(dates)):
            raise ValueError('reference_calendar_duplicate_dates')
        if any(row['is_open'] not in (0, 1, '0', '1') for row in rows):
            raise ValueError('reference_calendar_open_flag_invalid')
        values = dict(zip(dates, [int(row['is_open']) for row in rows]))
        expected = (date(spec['year'] + 1, 1, 1) - date(spec['year'], 1, 1)).days
        if len(values) != expected or any(day(d).year != spec['year'] for d in values):
            raise ValueError('reference_calendar_year_incomplete')
        calendars.append(dict(exchange=spec['exchange'], year=spec['year'],
            source_sha256=sources[spec['source']]['sha256'], derivation='exchange_trade_cal_v1', days=values))
    payload = dict(version=REFERENCE_VERSION, reports=reports, calendars=calendars,
                   aliases=aliases, aliases_sha256=sources[alias_name]['sha256'])
    ReferenceBundle(payload)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    (directory / 'bundle.json').write_bytes(raw)
    (directory / 'manifest.json').write_text(json.dumps(dict(bundle_sha256=sha256(raw).hexdigest(),
        sources=sources, plan_sha256=digest(plan)), ensure_ascii=False, indent=2), encoding='utf-8')
    result = ReferenceBundle.load(directory)
    result.hydration_requests = requests
    return result
