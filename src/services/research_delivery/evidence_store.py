"""Content-addressed endpoint-table captures. Never receives authentication data."""
from hashlib import sha256
import json
from pathlib import Path
import re

import pandas as pd

from src.services.research_delivery.live_contracts import json_safe


class EvidenceStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _write(self, payload, business_date):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = sha256(encoded.encode("utf-8")).hexdigest()
        directory = self.directory / business_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.json"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != encoded:
                raise ValueError("evidence_hash_conflict") from None
        return dict(sha256=digest, evidence_id=digest, path=str(path.resolve()))

    def capture_payload(self, source, dataset, payload, *, fetched_at, business_date, provenance):
        """Capture whitelisted caller-normalized facts, never claim raw HTTP provenance.

        Callers must select public fact fields; this recursive key guard is an
        additional boundary, not a scrubber for arbitrary provider responses.
        """
        def safe_keys(value):
            if isinstance(value, dict):
                return all(isinstance(k, str) and not re.search(r"token|secret|auth|password|key", k, re.I)
                           and safe_keys(v) for k, v in value.items())
            return all(safe_keys(v) for v in value) if isinstance(value, (list, tuple)) else True

        if (not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", source)
                or not isinstance(dataset, str) or not re.fullmatch(r"[a-z_]{1,80}", dataset)
                or provenance not in {"provider_normalized", "derived"} or not safe_keys(payload)):
            raise ValueError("evidence_capture_invalid")
        normalized = json_safe(payload)
        data = dict(source=source, dataset=dataset, provenance=provenance, payload=normalized,
                    fetched_at=fetched_at.isoformat(), business_date=business_date.isoformat())
        return dict(self._write(data, business_date), source=source, dataset=dataset, provenance=provenance,
                    fetched_at=fetched_at.isoformat(), business_date=business_date.isoformat())

    def capture(self, endpoint, params, table, *, fetched_at, business_date):
        if (not re.fullmatch(r"[a-z_]+", endpoint)
                or set(params) - {"trade_date", "start_date", "end_date", "exchange", "list_status", "ts_code"}
                or not isinstance(table, pd.DataFrame) or not table.columns.is_unique
                or any(not isinstance(c, str) or re.search(r"token|secret|auth|password|key", c, re.I) for c in table)):
            raise ValueError("evidence_capture_invalid")
        rows = [[None if v is None or v is pd.NA or v is pd.NaT else json_safe(v) for v in row]
                for row in table.itertuples(index=False, name=None)]
        payload = dict(source="tushare", endpoint=endpoint, params=params,
                       fetched_at=fetched_at.isoformat(), business_date=business_date.isoformat(),
                       columns=table.columns.tolist(), rows=rows)
        return dict(self._write(payload, business_date), source="tushare", endpoint=endpoint, fetched_at=fetched_at.isoformat(),
                    business_date=business_date.isoformat(), params=dict(params), row_count=len(table))
