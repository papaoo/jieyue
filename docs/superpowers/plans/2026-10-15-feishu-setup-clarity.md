# Feishu setup clarity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans inline. Steps use checkbox syntax. User has approved the design and requested execution; do not pause for another execution-choice prompt.

**Goal:** Separate legacy notifications and create a complete group subscription with one durable save.

**Architecture:** Add an idempotent atomic setup command over existing destination/subscription tables and vault. Use one frontend setup form, preserve existing advanced editors and explicit test flow.

**Tech Stack:** FastAPI/Pydantic/SQLAlchemy SQLite, existing vault; React/TypeScript, existing common inputs/buttons and CSS; pytest/Vitest/Playwright.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-10-15-feishu-setup-clarity-design.md` (approved design recorded).
- No real credentials read, real sends, LLM calls, automatic tasks, deployment, Git commit/push or data deletion.
- Existing dirty non-main checkout retained; tests isolated; screenshots ignored.
- Existing DTOs snake_case; admin/session/same-origin/secure credential transport remain enforced.
- Preserve legacy configuration and original subscription/credential APIs.

### Task 1: Atomic persisted setup command

**Files:** modify `api/v1/schemas/research_delivery.py`, `api/v1/endpoints/research_delivery.py`, `src/repositories/research_delivery_models.py`, `src/repositories/research_delivery_repository.py`; create `src/services/research_delivery/setup.py`; test `tests/test_research_delivery_setup.py`.

**Interfaces:** consumes existing service categories, vault and repository.atomic(); produces POST/GET `/setups` with the exact request/result described in spec.

- [ ] RED tests using the existing isolated API client, e.g.:
```python
response = client.post(PREFIX + '/setups', json=body)
assert response.status_code == 201
saved = response.json()
assert client.get(PREFIX + '/setups/' + body['request_id']).json() == saved
assert len(client.get(PREFIX + '/destinations').json()['items']) == 1
```
Run `.venv/Scripts/python.exe -m pytest tests/test_research_delivery_setup.py -q`; expect 404 before route.
- [ ] Implement strict union validation and receipt table. In `with repository.atomic() as session`, check request receipt/hash before side effects; check existing target/version/duplicate pair or new URL fingerprint; store immutable vault ref, add target and subscription and receipt; flush, return public dictionaries, context commits before return reaches HTTP. Same request returns existing result; mismatch 409.
```python
with repository.atomic() as session:
    receipt = session.get(DeliverySetupReceipt, request_id)
    if receipt is not None:
        if receipt.payload_hash != digest:
            raise ManagementError('setup_request_conflict', 'Request changed', 409)
        return setup_public(session, receipt)
```
- [ ] GREEN + API/repository/credential regressions. No commit; record test command and before/after evidence.

### Task 2: Separate entry and single-save form

**Files:** `apps/dsa-web/src/{types/researchDelivery.ts,api/researchDelivery.ts,pages/SettingsPage.tsx,components/settings/ResearchDeliveryPanel.tsx,components/settings/research-delivery/SetupEditor.tsx,components/settings/research-delivery/shared.ts,components/settings/research-delivery/research-delivery.css}` plus corresponding test files and `e2e/fixtures/research-delivery.spec.ts`.

**Interfaces:** consumes `api.setup(body): Promise<SetupResult>`, `api.setupStatus(request_id): Promise<SetupResult>`; produces guarded single-form state and public saved rows. Existing advanced editors unchanged.

- [ ] RED tests: default view hides legacy fields/top global save; category without subscription opens setup form; one click makes one setup write; success after reload shows same pair; rejected response preserves inputs; uncertain result permits only same-ID recovery/retry; existing target passes version. Use `expect(api.setup).toHaveBeenCalledTimes(1)` and `expect(api.test).not.toHaveBeenCalled()`.
- [ ] Add setup API/type and focused SetupEditor. All inputs in one form; default no category/no existing target selected, required native fields, controlled select; source request frozen on first submit and retained through uncertain result. Only known rejected 4xx (not timeout/rate limit) allow edited resubmission with new ID; unknown offers GET lookup and same payload idempotent retry.
```tsx
const saved = await api.setup(body);
setGroups(rows => replace(rows, saved.destination));
setSubscriptions(rows => replace(rows, saved.subscription));
```
- [ ] SettingsPage notification mode local state `research | legacy`, default research; legacy panel/test only visible legacy; global save/reset scopes clearly labelled and new draft owner remains mounted when dirty/busy.
- [ ] GREEN frontend tests, lint/TypeScript/build, browser fixture including reload, two independent groups, failure/unknown, mobile overflow. Update handbook/flat changelog and capture screenshots. No commit.

## Review Focus

Inspect transaction/receipt race and response-loss recovery; no half-created public rows; failed credential validation or stale existing version does not write; no secret echo in exceptions or logs; hidden legacy drafts and reset/navigation behavior; save never calls transport/model; confirmed saved state distinguish automatic delivery readiness. Final independent reviewer is required by executing-plans; only one fix pass for Important/Critical with RED→GREEN, defer Minors.
