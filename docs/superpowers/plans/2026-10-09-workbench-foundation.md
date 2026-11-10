# Guanlan Workbench Foundation Implementation Plan

> **For agentic workers:** Execute the scoped tasks below with parallel implementers for independent files and a fresh review of the combined diff. Steps use checkbox syntax for tracking. No Git commit, tag or push is authorized.

**Goal:** Upgrade the existing application into the first usable Guanlan research workbench, preserving analysis, authentication, screening, portfolio and backtest behavior.

**Architecture:** Keep FastAPI, React and the current routing, API clients and design tokens. Extend the shell and add an evidence-aware research page that consumes the existing data overview endpoint. A small brand configuration and motion preference provider keep visual decisions centralized.

**Tech Stack:** Existing React 19 / TypeScript / Vite / Tailwind v4 / Motion / Recharts / FastAPI / project Python virtual environment.

## Global Constraints

- User authorized design and implementation on 2026-10-09; no additional design approval gate applies to this first local iteration.
- Existing checkout and isolated `.venv` / local `node_modules` are the authorized development environment. Preserve existing user edits.
- Retain authentication, redirects, API fields, storage keys, task submission, notification settings and scheduling behavior.
- Keep existing framework and dependencies; external visual assets need explicit source and permission before copying.
- Work name: 观澜 / Guanlan Research / GL; this is a reversible product working name, not a trademark availability claim.
- No fabricated prices, quote movements, source health, research evidence, task progress or strategy performance.
- Every animation follows system reduced motion and the user motion switch; no scroll hijacking, canvas/3D background or continuous quote animation.
- Local development only. No server deployment, paid credentials, broker access, commits or remote writes in this phase.
- New UI copy supports Chinese and English. Chinese internal development notes have no translated upstream equivalent.

## Design decision

Use the graphite terminal direction from the design specification: neutral graphite surfaces, an ice-blue focus color, fine metallic edges, restrained ambient lighting, and asymmetrical research composition. Distinguish action, selection and actual state through spacing, type and contrast. The login has a larger orbital mark; dense research pages retain stable tables and numerical typography. A shared light theme uses paper-gray surfaces and darker blue focus. Reference specification: `../specs/2026-10-09-research-workbench-ui-direction.md`.

### Task 1: Brand and login

**Files:** `apps/dsa-web/src/config/brand.ts`, `src/components/brand/BrandMark.tsx`, `public/guanlan-mark.svg`, `index.html`, `src/pages/LoginPage.tsx`, scoped login CSS, visible document titles and i18n brand strings.

**Interfaces:**

```ts
export const BRAND = {
  name: '观澜', englishName: 'Guanlan Research', shortName: 'GL',
  tagline: '看清变化，验证判断',
} as const;
// BrandMark props: { className?: string }, SVG currentColor, decorative aria-hidden.
```

- [x] Centralize user-visible name and draw an original orbital mark; retain upstream license notices and technical identifiers.
- [x] Preserve login submission and safe redirect functions while restyling the form, empty/error states and one-time entry.
- [x] Show protocol information from `window.location.protocol`; HTTP must not claim TLS.
- [x] Verify existing login and redirect tests with the focused suite and final full frontend suite.

### Task 2: Shell, theme and motion

**Files:** `src/contexts/MotionPreferenceContext.tsx`, `src/main.tsx`, `src/styles/workbench.css`, `src/components/layout/Shell.tsx`, `SidebarNav.tsx`, existing HomePage layout class names.

**Interfaces:**

```ts
type MotionPreference = { motionEnabled: boolean; toggleMotion: () => void };
// Provider wraps the app. Hook has a safe fallback for existing component tests.
// document.documentElement.dataset.motion: 'on' | 'off'.
// local preference key: 'guanlan.motion'; unavailable storage never blocks rendering.
```

- [x] Add motion preference provider; system reduced-motion overrides the user's enabled preference. Stop transition and decoration CSS when disabled.
- [x] Add brand/navigation active rail, research route link, keyboard focus/skip link and accessible motion control.
- [x] Reuse existing HSL tokens with graphite/paper color values and shared polished card/input/button surfaces. Preserve semantic red/green states.
- [x] Refine home workspace panels without changing business state or submissions. Route entry runs on route changes only.
- [x] Verify shell/sidebar tests, Chinese/English copy, accessible controls, theme switching and mobile navigation. Browser smoke checks are bounded; not a full assistive-technology audit.

### Task 3: Cross-asset research entry

**Files:** `src/pages/ResearchPage.tsx`, `src/components/research/ResearchHistoryPanel.tsx`, `src/utils/researchCoverage.ts`, `src/api/stocks.ts`, `src/types/researchHistory.ts`, `src/App.tsx`, focused research tests.

**Interfaces:**

```ts
// Consume existing dataCapabilityApi.getOverview(): Promise<DataCapabilityOverview>.
// Coverage derived strictly from provider.datasetMarkets + enabled/configured/status.
// A configured provider is not proof of a successful/latest market observation.
type ResearchAsset = 'cn' | 'us' | 'gold' | 'crypto';
```

- [x] Create an asset selector with a clear research lens, driver questions and real coverage details.
- [x] Separate provider configuration from runtime dataset status; loading, failure, unknown and stale are visible. A failed refresh cannot make old data look current.
- [x] Add a browser-local hypothesis notebook: thesis, observable evidence, invalidation condition and date. Save requires explicit click; handle storage failure and export a structured JSON backup.
- [x] Link to current screening, data, portfolio and backtest capabilities without triggering model calls or analyses.
- [x] Add manually requested equity/ETF daily history with existing Recharts, abort on asset switch, no price interpolation animation, and actual provenance plus unknown adjustment/freshness.
- [x] Test coverage semantics and notebook persistence/error states; use labelled deterministic fixtures in browser visual checks. Guard all edited assets on navigation/reload and warn about draft loss at logout.

### Task 4: Verify data and handoff

**Files:** `scripts/probe-market-data.py`, `src/services/stock_service.py`, `src/services/stock_profile_service.py`, `api/v1/schemas/stocks.py`, `api/v1/endpoints/stocks.py`, `tests/test_stock_history_quality.py`, existing profile tests, static OpenAPI, `docs/data-source-local-verification.md`, `docs/guanlan-workbench.md`, `docs/CHANGELOG.md`, this progress record.

- [x] Perform a bounded read-only sample through existing providers in `.venv`; record provider, rows, latest sample date and field semantics. Do not trigger model or notification work.
- [x] Fix optional NaN/Inf and zero semantics, expose additive history provenance/quality metadata and carry limitations through the stock-profile historical block; test actual JSON serialization and failure contracts.
- [x] Run lint, build, full frontend tests and Python compile/affected tests. Validate positive OHLC/envelopes and reject wrong periods.
- [x] Capture desktop light/dark, mobile, reduced-motion, unrequested and failure states under ignored `local/`. Browser assertions also verify fixture history rendering and removal after failure; empty responses have unit coverage.
- [x] Review combined diff, resolve material regressions, document verified results and outstanding integration boundaries.
- [x] Start/verify the local application using the existing project startup script and provide the URL and concise user instructions.

## Integration queue after foundation

1. Typed asset identifiers and providers for BTC / direct gold data; ETF proxies must be explicitly identified.
2. Authorized macro releases with published/known timestamps and revision semantics, plus source citations.
3. Out-of-sample strategy experiments using existing ETF rotation before adding portfolio optimizers.
4. Evaluate CCXT / EdgarTools / skfolio as separate adapters with dataset rights and compatibility checks.

## Progress and decisions

- 2026-10-09: User requested a continuous goal and explicitly authorized first design plus local development. First phase selected to deliver a working UI and research foundation rather than treating the entire multi-asset roadmap as a single completed release.
- 2026-10-09: Parallel ownership assigned for brand/login, data verification and open-source reference audit; shell/theme/research integration owned by the main agent.
- 2026-10-09: No commit steps: repository AGENTS.md requires separate authorization for Git commits.
- 2026-10-09: Integrating real daily history exposed existing NaN/source loss. Added a narrow service/API/stock-profile quality fix with deterministic JSON tests; no provider priority or configuration change.
- 2026-10-09: Browser regression exposed textarea accessible names changing after saved asset switches; stable labelled-by spans added. Independent review exposed cross-market attribution without explicit market evidence; coverage now requires provable source-market ownership.
- 2026-10-09: Global reduced-motion review found the legacy score gauge animation loop and portfolio Pie outside CSS/Motion controls; both now consume the shared preference. The desktop update tooltip now uses the existing Tooltip component.
- 2026-10-09: Independent review exposed wrong-period and finite-but-impossible prices; both client and service now reject/skip invalid data without erasing legitimate optional zero values. Light warning text uses a darker semantic token. Research navigation guards cover all assets; logout warns before discarding research drafts.

## Final foundation verification

- Frontend: `npm test -- --maxWorkers=2 --reporter=json --outputFile=../../local/workbench-frontend-final.json`: **1,404 passed, 2 skipped, 0 failed, 112 files**. Default concurrency initially produced timeouts; two-worker reruns passed. An existing June signal fixture was updated to a recent relative creation time so its expected 90-day-window inclusion does not expire with wall-clock time. No runtime signal filtering was changed.
- `npm run lint`: 0 errors; one existing `StockScreeningPage.tsx:1383` hook dependency warning remains. Final touched TS/TSX files also pass scoped lint.
- `npm run build`: TypeScript and Vite passed; final Vite build 11.88 seconds. No dependency or lockfile updates.
- Backend: **80 passed** across history quality and the complete profile API suite; six changed Python files compile. Two existing test-environment warnings remain. Probe checks and Tencent offline tests were also run; this is not the entire backend suite.
- Browser: actual unauthenticated light/dark login; authenticated deterministic fixture research at 1440 and 375 widths; reduced motion; asset note save/switch; inactive-asset dirty navigation cancellation; sidebar collapse and mobile drawer; source refresh failure; daily chart success and failure clearing. No script errors or whole-page horizontal overflow in these checks. Research screenshots are fixtures, not live market observations.
- Live process: restart using ownership-validated `scripts/local-web.ps1`; health HTTP 200; existing authentication/password still enabled; unauthenticated history HTTP 401; served OpenAPI contains additive metadata. URL: `http://127.0.0.1:18080`. No password reset, model, notification, trading, paid API or remote deployment. Browser panel opening was queued by the app, not confirmed visible.
- Data: real bounded Tencent ETF probe returned 39 daily rows, latest bar 2026-10-09; provenance/limitations in `docs/data-source-local-verification.md`. An authenticated live browser history call was not performed; service HTTP contracts use fixtures and the independent online probe supplies the network evidence.
- First-stage goal is achieved; the remaining multi-asset integration queue is not claimed complete.

## Review focus

Authentication and redirects; truthful data coverage; notebook privacy/storage failures; reduced-motion across CSS and Motion; theme contrast; mobile overflow and navigation; no duplicate fetch loops; preserved old capabilities and technical keys; unconfigured state never presented as a live signal.
