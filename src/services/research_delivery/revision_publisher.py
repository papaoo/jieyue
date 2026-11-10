"""Explicit historical editions, complete frozen scope and durable one-attempt send.

No scheduler lease, original-row writes, schema creation or recovery on reads.
An existing publication reserves the edition scope before any model call.
"""
from datetime import datetime, timedelta
from pathlib import Path
import uuid

from sqlalchemy import select, text

from src.notification_sender.feishu_sender import DeliveryOutcome
from src.repositories.automation_models import AutomationControl, PublicationRelease, ResearchBatch
from src.repositories.research_delivery_models import DeliveryDestination, DeliveryOutbox, DeliverySubscription, ResearchPublication
from src.services.research_delivery.aggregation import canonical, content_hash
from src.services.research_delivery.brief_rendering import render_brief, frozen_payload, normalize_brief_llm
from src.services.research_delivery.formal_publisher import FormalPublisher, PAYLOAD_BYTE_LIMIT, _fail
from src.services.research_delivery.publisher import ResearchPublisher
from src.services.research_delivery.render_formal import formal_envelope
from src.services.research_delivery.research_brief import FORMAT_VERSION, PROMPT_VERSION, build_brief_input, validate_brief_explanation
from src.services.research_delivery.saved_research import _json, verified_source, saved_context, augmented_source


def _row_hash(row):
    return content_hash({c.name: (getattr(row, c.name).isoformat() if hasattr(getattr(row, c.name), 'isoformat')
        else getattr(row, c.name)) for c in row.__table__.columns})


class _ValidationCache(set):
    """One transaction/check only: deduplicate shared historical evidence IO."""
    def __init__(self):
        super().__init__()
        self.originals, self.related, self.contexts = {}, {}, {}


class RevisionPublisher(FormalPublisher):
    """Reuse formal validators/clock/transport; never its same-day lease/window."""
    @staticmethod
    def _edition(edition_id):
        try:
            if str(uuid.UUID(edition_id)) != edition_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            _fail('revision_edition_invalid')
        return edition_id

    @staticmethod
    def request_id(edition_id, subscription_id):
        value = f'revision:{edition_id}:{subscription_id}'
        if len(value) >= 100:
            _fail('revision_identity_invalid')
        return value

    def _original(self, session, publication_id, cache):
        if isinstance(cache, _ValidationCache) and publication_id in cache.originals:
            return cache.originals[publication_id]
        pub = session.get(ResearchPublication, publication_id)
        release = session.get(PublicationRelease, publication_id)
        rows = list(session.scalars(select(DeliveryOutbox).where(DeliveryOutbox.publication_id == publication_id)))
        if pub is None or release is None or len(rows) != 1:
            _fail('revision_original_not_found')
        receipt = rows[0]
        batch = session.get(ResearchBatch, release.batch_id)
        if batch is None:
            _fail('revision_original_batch_missing')
        source = verified_source({c.name: getattr(batch, c.name) for c in batch.__table__.columns},
            now=self._now(), evidence_cache=cache)
        document, payload = _json(pub.document_json), _json(release.payload_json)
        if (receipt.kind != 'formal' or receipt.status != 'sent' or receipt.attempt_count != 1
                or receipt.request_id != FormalPublisher.request_id(batch.business_date, pub.subscription_id)
                or receipt.destination_id != pub.destination_id or receipt.destination_version != pub.destination_version
                or receipt.credential_ref != release.credential_ref or receipt.expires_at != release.expires_at
                or release.expires_at > batch.expires_at or pub.source_snapshot_json != batch.result_json
                or content_hash(document) != pub.content_hash or content_hash(payload) != release.payload_hash
                or receipt.payload_json != release.payload_json or receipt.payload_hash != release.payload_hash
                or frozen_payload(document) != payload or document['envelope']['strategy_id'] != batch.category_id
                or document['envelope']['business_date'] != batch.business_date
                or source['strategy_version'] != self._version(batch.category_id)):
            _fail('revision_original_changed')
        expected_run = source['result'].get('run_id') or batch.id
        if pub.source_run_id != expected_run:
            _fail('revision_original_source_changed')
        result = pub, batch, source, dict(publication=_row_hash(pub), release=_row_hash(release),
            receipt=_row_hash(receipt), batch=_row_hash(batch))
        if isinstance(cache, _ValidationCache):
            cache.originals[publication_id] = result
        return result

    def _scope(self, session, day, cache):
        control = session.get(AutomationControl, 'main')
        if control is None or not control.enabled:
            _fail('automation_paused')
        members = []
        for sub in session.scalars(select(DeliverySubscription).where(DeliverySubscription.enabled.is_(True)).order_by(DeliverySubscription.id)):
            target = session.get(DeliveryDestination, sub.destination_id)
            if target is None or not target.enabled:
                continue
            receipt = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.request_id == FormalPublisher.request_id(day, sub.id)))
            if receipt is None or receipt.kind != 'formal' or receipt.status != 'sent':
                continue
            pub, batch, _, _ = self._original(session, receipt.publication_id, cache)
            _, target = self._configuration(session, sub.id, batch)
            if pub.subscription_id != sub.id or pub.destination_id != target.id or batch.business_date != day:
                _fail('revision_scope_changed')
            members.append(dict(original_publication_id=pub.id, subscription_id=sub.id,
                destination_id=target.id, subscription_version=sub.version, destination_version=target.version,
                category_id=sub.strategy_id, top_n=sub.top_n, llm_mode=sub.llm_mode,
                credential_digest=content_hash(dict(reference=target.credential_ref, keyword=target.keyword))))
        if not members:
            _fail('revision_scope_empty')
        return dict(business_date=day, authorization_epoch=control.version,
            members=sorted(members, key=lambda row: (row['original_publication_id'], row['subscription_id'], row['destination_id'])))

    def _edition_rows(self, session, edition):
        # Includes preparing reservations, which deliberately have no outbox yet.
        prefix = f'revision:{edition}:'
        return [row for row in session.scalars(select(ResearchPublication))
                if _json(row.document_json).get('revision_binding', {}).get('request_id', '').startswith(prefix)]

    def _inputs(self, session, original_id, edition, locations, cache):
        original, batch, source, chain = self._original(session, original_id, cache)
        scope = self._scope(session, batch.business_date, cache)
        members = [row for row in scope['members'] if row['original_publication_id'] == original_id]
        if len(members) != 1:
            _fail('revision_original_not_active')
        member = members[0]
        related_cache = cache.related if isinstance(cache, _ValidationCache) else {}
        if batch.business_date not in related_cache:
            related, bindings = [], []
            for other in session.scalars(select(ResearchBatch).where(ResearchBatch.business_date == batch.business_date).order_by(ResearchBatch.created_at, ResearchBatch.id)):
                raw = _json(other.result_json)
                if raw.get('state') not in {'ready', 'degraded'} or raw.get('blockers'):
                    continue
                if other.strategy_version != self._version(other.category_id):
                    _fail('strategy_version_changed')
                related.append(verified_source({c.name: getattr(other, c.name) for c in other.__table__.columns},
                    now=self._now(), evidence_cache=cache))
                bindings.append(dict(id=other.id, input_hash=other.input_hash))
            related_cache[batch.business_date] = related, bindings
        related, bindings = related_cache[batch.business_date]
        context_cache = cache.contexts if isinstance(cache, _ValidationCache) else {}
        context_key = canonical(dict(day=batch.business_date, locations=locations))
        if context_key not in context_cache:
            context_cache[context_key] = (saved_context(related, day=batch.business_date, now=self._now(), bindings=bindings, **locations)
                if any(locations.values()) else None)
        context = context_cache[context_key]
        frozen = augmented_source(source, related, top_n=member['top_n'], context=context)
        snapshot = dict(original_batch=dict(id=batch.id, input_hash=batch.input_hash), source=frozen)
        binding = dict(edition_id=edition, request_id=self.request_id(edition, original.subscription_id),
            original_publication_id=original_id, original_source_json=original.source_snapshot_json,
            original_chain=chain, scope=scope, scope_hash=content_hash(scope), locations=locations,
            source_digest=content_hash(snapshot))
        for row in self._edition_rows(session, edition):
            existing = _json(row.document_json)['revision_binding']
            if existing['scope'] != scope or existing['scope_hash'] != binding['scope_hash']:
                _fail('revision_scope_changed')
        return original, batch, member, snapshot, binding

    def prepare(self, original_publication_id, *, edition_id, evidence_dir=None, reference_manifest=None, background_manifest=None):
        edition = self._edition(edition_id)
        locations = {key: str(Path(value).resolve(strict=True)) if value else None for key, value in
            dict(evidence_dir=evidence_dir, reference_manifest=reference_manifest, background_manifest=background_manifest).items()}
        with self.repository.atomic() as session:
            original, batch, member, snapshot, binding = self._inputs(session, original_publication_id, edition, locations, _ValidationCache())
            source = snapshot['source']
            envelope, detail = formal_envelope(source, member['top_n'], format_version=FORMAT_VERSION)
            brief_input = build_brief_input(envelope, detail, source)
            key = content_hash(dict(revision=binding, frozen_input=brief_input, format_version=FORMAT_VERSION, prompt_version=PROMPT_VERSION))
            existing = [p for p in self._edition_rows(session, edition) if p.subscription_id == original.subscription_id]
            if len(existing) > 1:
                _fail('revision_identity_conflict')
            receipt = session.scalar(select(DeliveryOutbox).where(DeliveryOutbox.request_id == binding['request_id']))
            if receipt is not None and (receipt.kind != 'revision' or not existing or receipt.publication_id != existing[0].id):
                _fail('revision_identity_conflict')
            title = next(row['display_name'] for row in self.service.categories() if row['category_id'] == batch.category_id)
            title += ' · 修订版 · ' + batch.business_date
            if existing:
                pub = existing[0]
                if (_json(pub.document_json).get('revision_binding') != binding or pub.input_hash != key
                        or pub.source_snapshot_json != canonical(snapshot) or content_hash(_json(pub.document_json)) != pub.content_hash):
                    _fail('revision_identity_conflict')
                if pub.status != 'preparing':
                    self._validate_publication(session, pub, edition, _ValidationCache())
                    return ResearchPublisher._publication_public(pub)
            else:
                document = dict(revision_binding=binding, title=title, blockers=['revision_preparing'],
                    llm=dict(status='not_requested'), rendered_content='修订版准备中，尚不可发送。')
                pub = ResearchPublication(id=uuid.uuid4().hex, subscription_id=original.subscription_id,
                    destination_id=member['destination_id'], subscription_version=member['subscription_version'],
                    destination_version=member['destination_version'], source_run_id=original.source_run_id,
                    input_hash=key, content_hash=content_hash(document), source_snapshot_json=canonical(snapshot),
                    document_json=canonical(document), status='preparing', created_at=self._utc(self._now()))
                session.add(pub)
                session.flush()
            publication_id = pub.id
        if not envelope['candidates'] or (not any(row['metrics'] for row in envelope['candidates']) and not envelope['evidence']):
            llm = dict(status='off' if member['llm_mode'] == 'off' else 'fallback', reason='no_supported_evidence', explanation=None)
        else:
            llm = self.explanations.explain(brief_input, key, member['llm_mode'] == 'explain_only', member['llm_mode'])
        if llm.get('reason') == 'attempt_in_progress_or_unknown':
            with self.repository.session_factory() as session:
                return ResearchPublisher._publication_public(session.get(ResearchPublication, publication_id))
        if llm.get('explanation') is not None:
            llm = dict(llm, explanation=validate_brief_explanation(canonical(llm['explanation']), brief_input))
        llm = {key: llm[key] for key in ('status', 'reason', 'model', 'prompt_version', 'usage', 'finish_reason', 'explanation') if key in llm}
        brief = render_brief(title, envelope, detail, llm, brief_input)
        llm = normalize_brief_llm(llm, brief)
        document = dict(revision_binding=binding, title=title, envelope=envelope, detail=detail,
            blockers=[], llm=llm, brief_input=brief_input, **brief)
        payload = frozen_payload(document)
        if len(canonical(payload).encode('utf-8')) > PAYLOAD_BYTE_LIMIT:
            _fail('payload_too_large')
        with self.repository.atomic() as session:
            _, batch, _, current_snapshot, current_binding = self._inputs(session, original_publication_id, edition, locations, _ValidationCache())
            if current_binding != binding or current_snapshot != snapshot:
                _fail('revision_identity_conflict')
            pub = session.get(ResearchPublication, publication_id)
            if pub.status != 'preparing':
                self._validate_publication(session, pub, edition, _ValidationCache())
                return ResearchPublisher._publication_public(pub)
            _, target = self._configuration(session, pub.subscription_id, batch)
            if target.keyword and target.keyword not in brief['rendered_content']:
                _fail('destination_keyword_mismatch')
            now = self._utc(self._now())
            pub.document_json, pub.content_hash, pub.status = canonical(document), content_hash(document), 'prepared'
            expires = now + timedelta(minutes=60)
            release = PublicationRelease(publication_id=pub.id, batch_id=batch.id,
                authorization_version=binding['scope']['authorization_epoch'], credential_ref=target.credential_ref,
                payload_hash=content_hash(payload), payload_json=canonical(payload), expires_at=expires, created_at=now)
            outbox = DeliveryOutbox(id=uuid.uuid4().hex, request_id=binding['request_id'], kind='revision',
                destination_id=target.id, destination_version=target.version, credential_ref=target.credential_ref,
                publication_id=pub.id, payload_json=canonical(payload), payload_hash=content_hash(payload),
                status='pending', attempt_count=0, expires_at=expires, created_at=now)
            session.add_all([release, outbox])
            session.flush()
            return ResearchPublisher._publication_public(pub)

    def _validate_publication(self, session, pub, edition, cache):
        document = _json(pub.document_json)
        binding = document['revision_binding']
        _, batch, member, snapshot, expected = self._inputs(session, binding['original_publication_id'], edition, binding['locations'], cache)
        if (binding != expected or pub.source_snapshot_json != canonical(snapshot) or content_hash(document) != pub.content_hash
                or pub.subscription_id != member['subscription_id'] or pub.destination_id != member['destination_id']
                or pub.subscription_version != member['subscription_version'] or pub.destination_version != member['destination_version']):
            _fail('revision_binding_changed')
        expected_key = content_hash(dict(revision=binding, frozen_input=document['brief_input'],
            format_version=FORMAT_VERSION, prompt_version=PROMPT_VERSION))
        if pub.input_hash != expected_key:
            _fail('revision_identity_conflict')
        release = session.get(PublicationRelease, pub.id)
        rows = list(session.scalars(select(DeliveryOutbox).where(DeliveryOutbox.publication_id == pub.id)))
        if pub.status != 'prepared' or release is None or len(rows) != 1:
            _fail('revision_incomplete')
        receipt = rows[0]
        _, target = self._configuration(session, pub.subscription_id, batch, pub, release)
        payload = frozen_payload(document)
        if (release.batch_id != batch.id or release.authorization_version != binding['scope']['authorization_epoch']
                or release.payload_json != canonical(payload) or release.payload_hash != content_hash(payload)
                or receipt.kind != 'revision' or receipt.request_id != binding['request_id']
                or receipt.destination_id != target.id or receipt.destination_version != target.version
                or receipt.credential_ref != release.credential_ref or receipt.payload_json != release.payload_json
                or receipt.payload_hash != release.payload_hash or receipt.expires_at != release.expires_at
                or receipt.status == 'pending' and receipt.attempt_count != 0
                or receipt.status in {'sending', 'sent', 'rejected', 'delivery_unknown'} and receipt.attempt_count != 1
                or release.expires_at != release.created_at + timedelta(minutes=60)
                or len(canonical(payload).encode('utf-8')) > PAYLOAD_BYTE_LIMIT
                or target.keyword and target.keyword not in document['rendered_content']):
            _fail('revision_payload_changed')
        return binding, release, receipt, payload

    def _manifest(self, session, edition, cache=None):
        rows = self._edition_rows(session, edition)
        if not rows:
            _fail('revision_edition_not_found')
        scope = _json(rows[0].document_json)['revision_binding']['scope']
        cache = _ValidationCache() if cache is None else cache
        current = self._scope(session, scope['business_date'], cache)
        if current != scope:
            _fail('revision_scope_changed')
        expected = {row['original_publication_id'] for row in scope['members']}
        actual = [_json(pub.document_json)['revision_binding']['original_publication_id'] for pub in rows]
        if len(actual) != len(set(actual)) or set(actual) - expected:
            _fail('revision_scope_changed')
        members = []
        complete = set(actual) == expected and all(row.status == 'prepared' for row in rows)
        for pub in rows:
            binding = _json(pub.document_json)['revision_binding']
            if binding['scope'] != scope or binding['scope_hash'] != content_hash(scope):
                _fail('revision_scope_changed')
            if pub.status != 'prepared':
                continue
            binding, release, receipt, _ = self._validate_publication(session, pub, edition, cache)
            members.append(dict(publication_id=pub.id, original_publication_id=binding['original_publication_id'],
                subscription_id=pub.subscription_id, destination_id=pub.destination_id,
                subscription_version=pub.subscription_version, destination_version=pub.destination_version,
                payload_hash=release.payload_hash, source_digest=binding['source_digest'], content_hash=pub.content_hash,
                expires_at=release.expires_at.isoformat()))
        manifest = dict(edition_id=edition, business_date=scope['business_date'], scope_hash=content_hash(scope),
            authorization_epoch=scope['authorization_epoch'], expected_scope=[{k: member[k] for k in
                ('original_publication_id', 'subscription_id', 'destination_id', 'subscription_version',
                 'destination_version', 'category_id', 'top_n', 'llm_mode')} for member in scope['members']],
            expected_count=len(expected), complete=complete, members=sorted(members, key=lambda r: r['publication_id']))
        manifest['manifest_hash'] = content_hash(manifest) if complete else None
        manifest['outcomes'] = [{k: self.repository.delivery_public(row)[k] for k in
            ('publication_id', 'status', 'error_code', 'attempt_count')} for row in session.scalars(
                select(DeliveryOutbox).where(DeliveryOutbox.publication_id.in_([pub.id for pub in rows])))]
        return manifest

    def inspect(self, edition_id):
        with self.repository.session_factory() as session:
            session.execute(text('BEGIN'))
            return self._manifest(session, self._edition(edition_id))

    def deliver(self, publication_id, *, edition_id, expected_manifest_hash):
        edition = self._edition(edition_id)
        with self.repository.atomic() as session:
            cache = _ValidationCache()
            manifest = self._manifest(session, edition, cache)
            if (not manifest['complete'] or manifest['manifest_hash'] != expected_manifest_hash
                    or publication_id not in {m['publication_id'] for m in manifest['members']}):
                _fail('revision_manifest_conflict')
            pub = session.get(ResearchPublication, publication_id)
            binding, release, row, payload = self._validate_publication(session, pub, edition, cache)
            now = self._utc(self._now())
            if row.status == 'sending' and row.sending_at and row.sending_at <= now - timedelta(seconds=90):
                row.status, row.error_code, row.completed_at = 'delivery_unknown', 'abandoned_sending', now
            if row.status != 'pending':
                return self.repository.delivery_public(row)
            if release.expires_at <= now:
                _fail('release_expired')
            if any(datetime.fromisoformat(m['expires_at']) <= now for m in manifest['members']):
                _fail('release_expired')
            credentials = self.service.vault.resolve(release.credential_ref)
            # Final authorization/complete-set check follows local credential resolution.
            session.expire_all()
            checked = self._manifest(session, edition)
            if checked['manifest_hash'] != expected_manifest_hash:
                _fail('revision_manifest_conflict')
            claim_time = self._utc(self._now())
            if release.expires_at <= claim_time or any(datetime.fromisoformat(m['expires_at']) <= claim_time for m in checked['members']):
                _fail('release_expired')
            row.status, row.attempt_count, row.sending_at = 'sending', 1, claim_time
            delivery_id = row.id
        try:
            outcome = self.sender(credentials, payload)
            if not isinstance(outcome, DeliveryOutcome) or outcome.status not in {'sent', 'rejected', 'delivery_unknown'}:
                outcome = DeliveryOutcome('delivery_unknown', 'invalid_transport_outcome')
        except Exception:
            outcome = DeliveryOutcome('delivery_unknown', 'transport_exception')
        with self.repository.atomic() as session:
            row = session.get(DeliveryOutbox, delivery_id)
            if row.status == 'sending':
                row.status, row.error_code, row.completed_at = outcome.status, outcome.error_code, self._utc(self._now())
            return self.repository.delivery_public(row)
