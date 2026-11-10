# Historical source feasibility — 2026-10-17

Purpose: select reproducible historical inputs for the repaired ten A-share screeners. This is a bounded probe record, not a declaration that all-market coverage has passed acceptance.

## User decisions

- Use multiple providers rather than assuming Tushare is the only source.
- Do not send industry-free approximations or incomplete status messages to strategy groups.
- A versioned migration to one industry standard is approved, provided live selection and historical replay use that same standard.
- No new paid plan, account upgrade or purchase is authorized.

## Actual probes

| Source | Verified capability | Remaining boundary |
| --- | --- | --- |
| Existing Tushare account | Historical daily, daily_basic, adj_factor, stock_basic delisted records, sample namechange intervals, suspend_d, stk_limit, CSI300 index; sampled latest session 2026-10-16 | Full date/security coverage and historical interval completeness are not established by a successful sample |
| Existing Tushare account | bak_basic, bak_daily, stock_st returned permission errors | Do not repeatedly probe or presume a paid upgrade is necessary before alternatives are examined |
| BaoStock anonymous | query_all_stock(2026-01-05): 7,074 rows; query_all_stock(2026-10-16): 7,438 rows; fields code, tradeStatus, code_name | Counts include non-stock instruments; SH/SZ only, no BJ; not counts of eligible A-shares |
| BaoStock anonymous | Sample daily tradestatus/isST and dated CSRC industry; sh.600000 Jan 5 gives J66 and updateDate Jan 5; Oct 16 gives updateDate Oct 12 | Weekly snapshots, not daily classification revisions; full industry query timed out in a separate probe; change-case and complete coverage verification pending |
| CNINFO p_stock2110 | Anonymous historical changes for 000001, 002594, 920047; sample total=count=records length | Per-code request; empty scode returns business error 402, old BJ alias and some old delisted examples empty; no proof of all-market completeness |

CNINFO raw response keys include SECCODE, SECNAME, ORGNAME, VARYDATE, F001V through F008C. Standards include CSRC, SW, CNINFO, CSI and CAC; preserve standard identity and hierarchy rather than mixing labels. The installed AKShare normalizer drops the latest-marker field and raises on some empty results. Raw response validation must precede normalization.

CNINFO exposes a change date but no announcement/publication/load timestamp in this response. A historical effective-date reconstruction is not evidence that a classification was available to investors on that date. BaoStock's query date similarly needs a documented historical snapshot/availability assumption, not an invented publication timestamp.

The installed BaoStock client uses removed pandas DataFrame.append in get_data(). Iterate next()/get_row_data() instead; no dependency patch is needed.

## Industry identity

Original Tushare stock_basic labels such as 全国地产/区域地产 cannot be recovered losslessly from CSRC K70 房地产业. SW and CSRC are not interchangeable. A source migration therefore changes grouping semantics and must have a named taxonomy/version used by both live and historical adapters. Old reports keep their original labels.

Selected contract after user-approved migration: `capco_major_v1`, latest official published CAPCO snapshot, grouping by category letter plus two-digit major code. Whole-universe coverage checks remain required, including stocks delisted during the test period, newly listed securities and Beijing aliases. Empty rows are missing, not evidence that a security did not exist or was non-ST.

## Official CAPCO snapshots actually fetched and parsed in memory

| Report | Official article date | PDF facts | Conservative usable date |
| --- | --- | --- | --- |
| 2025H1 | 2025-09-30 | HTTP 200, 4,626,417 bytes, 114 pages | First exchange session after release |
| 2025H2 | 2026-04-03 | HTTP 200, 772,144 bytes, 158 pages | First exchange session after release |
| 2026H1 | 2026-09-30 | HTTP 200, 782,339 bytes, 160 pages | First exchange session after release |

All have code/name and category/secondary/major code+name columns. Six-digit-token counts are only parser sanity checks, not certified securities-row counts. 2025H1 uses 430047, H2 uses 920047; official BSE mapping is required. 002210 remains L72 in H1/H2 and becomes D44 in 2026H1, agreeing with separately observed later BaoStock/CNINFO changes without using those changes before report release.

Article metadata and attachments (read as data, never execute remote JS):

- [2025H1 metadata](https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/29adc9e66d844d6fba0001bb2cdc529c.js), [classification PDF](https://sp.capco.org.cn:82/file/202603/hangyefenlei/2025shangban/hangyefenleigupiaodaima.pdf)
- [2025H2 metadata](https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/d9221cd5eb1448e5956f0e9d84ecba89.js), [classification PDF](https://sp.capco.org.cn:82/file/202604/hangyefenlei/2025xiaban/2025xiabangupiaodaima.pdf)
- [2026H1 metadata](https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/2e1b516684544cc7be7eab6f93f7dddc.js), [classification PDF](https://sp.capco.org.cn:82/file/202609/gwfile/2026shangbangupiaodaima.pdf)
- [BSE alias PDF](https://sp.capco.org.cn:82/file/202603/hangyefenlei/2025shangban/beijiaosuocunliang.pdf)
- [Official classification guideline data](https://www.capco.org.cn/dataFile/xhdt/tzgg/53b8127cbb9d46f5af59a5a9fce32838.js): Article 15 work-start June 10/December 20; Article 16 IPO initial classification follows prospectus, IPO before work-start enters current report, work-start through period-end enters next; Article 18 half-year publication. No immediately complete IPO supplement verified.

Use explicit `ineligible_no_published_classification` only for IPOs outside the report's verified inclusion cutoff. Unexplained absence of seasoned securities remains a gap. Official historical URLs may have been migrated (H1 path includes 202603), so preserve raw hashes/fetch times and describe reconstruction from extant official historical releases, not an independently archived never-revised vintage.

Tushare SW alternative also verified: Y/N wholemarket queries work, offset2/limit2 exactly matches the last two rows of offset0/limit4 for both flags; current membership of 920047 and bse_mapping work. Later-delisted 920305 has a historical N row but its sample interval ends in 2023; completeness in 2026 is unproven. SW is not used to silently fill CAPCO grouping.

## Execution and chronology

- SSE mainboard risk-warning price limits change from 5% to 10% on 2026-07-06. Read dated actual limits; never derive the whole 2026 period from a fixed ST percentage.
- BaoStock isST is binary; it does not distinguish ST, *ST, delisting arrangements or applicable daily price limit.
- BSE old/new codes require official entity mapping, not mechanical prefix replacement. Supplier aliases may differ even when 2026 exchange codes are new.
- CAPCO classification report period is not publication time. An official 2025 annual report states the 2025H2 classification was released in April 2026; do not backfill that report into January 2026.

## Primary references

- [BaoStock historical industry API](https://www.baostock.com/mainContent?file=stockIndustry.md)
- [BaoStock security master](https://www.baostock.com/mainContent?file=stockBasic.md)
- [CNINFO API documentation](https://webapi.cninfo.com.cn/#/apiDoc)
- [Tushare stock_basic](https://tushare.pro/document/1?doc_id=25)
- [SSE 2026 rule change and effective date](https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml)
- [SZSE 2026 rule notice](https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html)
- [BSE full code switch notice](https://www.bse.cn/important_news/200026735.html)
- [BSE code continuity explanation](https://www.bse.cn/important_news/200024115.html)
- [CAPCO historical classifications](https://www.capco.org.cn/xhgg/hyfl/hyfljg/index.html)
- [Company report documenting classification publication lag](https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-04-27/00e53526-1765-4b10-891b-78f6f8fd7268.PDF)

No return estimates, model calls or group messages were produced during these probes.

### Acquisition volume and pacing

The [current official permission table](https://tushare.pro/document/2?doc_id=290), checked 2026-10-18, lists the 2000-point tier at 200 requests/minute and 100000 requests per API/day, subject to each endpoint's separate permissions. Older source-code comments claiming 500 requests/day are not a reliable current planning limit. This public tier table does not prove account-specific/custom-gateway limits; actual permission/rate responses remain authoritative. Preserve explicit total request caps, bounded retries and pacing for the batch collector; do not buy access or assume unlimited requests.

## Exchange calendar live probe (2026-10-18 Beijing)

Three bounded account calls queried October 15–16, 2026 with explicit exchange and fields. SSE and SZSE each returned two matching exchange rows with `is_open=1`; **BSE returned an empty table**. Thus the new generic exchange-calendar collector cannot be declared fully live-ready merely because fake BSE fixtures pass. Do not treat BSE's empty response as no trading days, or silently relabel SSE rows as a verified BSE calendar. Task 3 must acquire a legitimate dated BSE calendar/official annual closure notices and preserve its derivation/source hashes. Official notices need to cover both warmup and evaluation years; unsupported years remain unknown. No denied API plan was purchased or retried.

Verified fallback, all native BSE pages returned public HTTP 200 with full article text:

- [2025 annual notice](https://www.bse.cn/important_news/200024437.html), published 2024-12-23, 北证公告〔2024〕233号. Inclusive closures: Jan 1; Jan 28–Feb 4; Apr 4–6; May 1–5; May 31–Jun 2; Oct 1–8. Extra explicitly closed government make-up weekends: Jan 26, Feb 8, Apr 27, Sep 28, Oct 11.
- [2026 annual notice](https://www.bse.cn/important_news/200027428.html), published 2025-12-22 (listing timestamp 16:38:31), 北证公告〔2025〕58号. Inclusive closures: Jan 1–3; Feb 15–23; Apr 4–6; May 1–5; Jun 19–21; Sep 25–27; Oct 1–7. Explicit closed make-up weekends: Jan 4, Feb 14, Feb 28, May 9, Sep 20, Oct 10.
- [Old trading rules](https://www.bse.cn/jygl_list/200010919.html), published 2021-11-02/effective November 15; [2026 replacement](https://www.bse.cn/jygl_list/200028217.html), published April 24/effective July 6. Clause 2.3.1 in both defines Monday–Friday sessions excluding statutory/BSE-announced closures.
- [September 2026 notice](https://www.bse.cn/important_news/200029478.html), dated September 17, corroborates September/October dates; its October 6 system test is not a trading session. The [CNINFO copy of the BSE annual PDF](https://dataclouds.cninfo.com.cn/sjother2/regulatory/2025/20251222/55875a9937374da4ae2d4999dfda2722.pdf) independently matches the native 2026 article.

Derive a complete civil-date calendar only for supported years: weekday AND outside inclusive closures. Make-up work weekends stay closed. Record `exchange=BSE`, `derivation=official_weekday_rule_plus_annual_closures`, raw bytes/hash, fetch time, publication and rule version. This is a documented derivation, not observed daily exchange telemetry; exceptional closures need dated official overrides. Unsupported years, missing/malformed annual body/sections or failed source verification remain unknown. No 2027 schedule has been verified.

Native article extraction uses `div.in_main.clearfix`; whole-page stripping duplicates hidden escaped share text. Public future-notice discovery was verified via POST `https://www.bse.cn/info/listse.do`, URL-encoded `page=0&pageSize=20&keywords=<title>&nodeIds[]=1289&needFields[]=infoId&needFields[]=title&needFields[]=htmlUrl&needFields[]=publishDate`. Strictly parse `null([...])` wrapper then JSON, never execute it. The title `2026年部分节假日休市安排` yielded exactly the above official article. Endpoint parameters came from the public site's `/template/6/bluewise/_files/js/aboutNEEQ/neeqNews.min.js` source, read as data.

## Local parser preflight artifacts

Four native BSE HTML documents were also saved in the same ignored preflight directory. These timestamps record local download completion in UTC, not article publication time. The controller independently read the extracted annual closure dates and rule 2.3.1 after download.

| File | Bytes | SHA256 | Download completion UTC |
| --- | ---: | --- | --- |
| bse-2025-annual.html | 21355 | `a493234de8ddb5d1a60a7ed360e02cfb65af6ab4cfa01cc1e0e22d1fa078b5a6` | 2026-10-17T16:27:18.5900466Z |
| bse-2026-annual.html | 21440 | `1d6178157837f021dcb98c1206ee2f6f8cafcadbbb5b1cb0ac3637bbe82387ab` | 2026-10-17T16:27:18.6309995Z |
| bse-rules-2021.html | 119658 | `9e2271bef634fd9337c4ee1cef8679a5cb99f813f6e1aae4d099b57d2a01ad18` | 2026-10-17T16:27:18.6401519Z |
| bse-rules-2026.html | 158953 | `cf89a37eb754baae0e303c4b9e6ff2fe2b328118aac1203d1de853d807983082` | 2026-10-17T16:27:18.6474797Z |

Controller downloaded the four verified public PDFs into ignored `local/audits/strategy-integrity-20261017/preflight/`, preserving existing files. SHA256:

- capco-2025h1.pdf: `b622b76b96d59611db36b01888cf6afd8cd711c5753a19ab8547e74bb0bb8228`
- capco-2025h2.pdf: `b1d0140572b20de11cd62ca478edb4da6e229928b216116df6345b3c2e461f58`
- capco-2026h1.pdf: `1f1862ae2199a82a3a35c62f16a7d576120bca97f3de608989c8897df9b6c8a6`
- capco-bse-aliases.pdf: `f85a6691a74a8c7e4d2bb838277161b9d0496f52cc66ddde4ba0211153401c3d`

First pages of 2025H1 and 2026H1 were rendered and visually checked against extracted text. Eight-column layout confirmed; manufacturing secondary codes are not major codes. Text extraction differs across versions: 2026H1 often concatenates six-digit code and company name without a separating space; names/major names may span lines; manufacture secondary code can be visually spaced. Parser must not rely on a single-line or single-space row format. Mapping PDF has explicit old/new codes and listing dates (six pages), not a prefix-only transform. Poppler emitted missing-font/substitution warnings for 2026H1; rendered Chinese text was legible for the inspected page, not a clean-warning claim or a complete-document visual review.

## Whole-universe reference preflight

Four read-only account queries returned stock_basic L=5577, D=339, P=0 and bse_mapping=248. Public alias PDF and account mapping agreed without mechanical prefix replacement. Universe reconstruction used listing date and retained delisting day to expose its execution-state boundary.

| As of | Applicable report | Unique report codes | Historically listed | Matched | Verified post-cutoff IPO absence | Unexplained seasoned absence |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2026-01-05 | 2025H1 | 5413 | 5470 | 5400 | 70 | 0 |
| 2026-04-07 | 2025H2 | 5463 | 5497 | 5456 | 41 | 0 |
| 2026-10-16 | 2026H1 | 5521 | 5577 | 5514 | 63 | 0 |

Examples of cutoff gaps: 688775 listed 2025-06-11 (January); 920121 (April); 920083 listed 2026-06-11 (October). These are genuine missing published classifications, not filled observations. The approved versioned eligibility policy discloses these as ineligible until published classification is available, rather than claiming whole-market coverage.

Later-delisted checks: 920305 has old 835305 in H1 and 920305 in H2, both I65; absent from H1 2026 after delisting. 920680 has old 839680 in H1/I65 and was retained on its 2026-01-05 delisting day for execution-state audit. Historical PDF company names were never used as daily ST status. All report code tokens were unique before and after verified alias normalization in this exploratory extraction; production parser still requires complete row/major-code validation and independent fixtures.

## Historical name / ST state preflight

### Core-metric and suspension source semantics (controller verified 2026-10-18)

The official [daily_basic contract](https://tushare.pro/document/2?doc_id=32) explicitly defines PE and PE_TTM as blank for loss-making companies. A present dated record with an explicitly null PE is therefore not interchangeable with an absent record or failed request. The shared valuation-compatible eligibility policy records `ineligible_pe_not_reported`; this preserves raw null and does not independently establish a company's earnings status. Other unknown required fields remain gaps. The same document specifies a 15:00–17:00 update window, so market close alone does not prove all required daily batches have arrived.

The official [suspend_d contract](https://tushare.pro/document/2?doc_id=214) specifies that `suspend_timing` has a time interval for intraday suspensions and is otherwise empty; `S` denotes suspension, `R` resumption. Its examples distinguish S+None from S+09:30–10:00. In a complete verified dated batch, an S event with a present null/normalized-empty timing field supports full-session suspension; an absent field, malformed interval or conflicting events remains unresolved. Intraday timing must govern open/close execution separately, not automatically exclude the entire session. These are source-contract semantics, not an assumption that all missing quotes represent suspensions.

Two bounded controller probe waves (three account calls each, no returns calculated):

| Business date | Daily / basic rows, unique matching identities | PE_TTM null | PB null | Volume ratio null | Suspension records |
| --- | ---: | ---: | ---: | ---: | --- |
| 2026-10-16 | 5567 / 5567 | 1634 | 52 | 4 | 10 S, 1 R; timing all null |
| 2026-01-05 | 5457 / 5457 | 1537 | 43 | 5 | 12 S, 4 R; timing all null |

In both samples the returned daily OHLC/volume/amount/change fields were non-null and basic total_mv/turnover_rate were non-null. October's 56 rows with other core nulls overlap 52 PE-null rows, leaving four with reported PE; January leaves six: 001369.SZ, 688809.SH, 300157.SZ, 920045.BJ, 301687.SZ, 001396.SZ. These require the independent IPO/ST/source-semantic checks; they are not declared valid or filled. Counts matching suspension totals do not themselves prove identity coverage.

January SDK-normalized records are saved in ignored `preflight/field-probe-20260105-{daily,daily_basic,suspend_d}.json`, each with source/date/fetched_at/hash/hash-basis. They are source feasibility evidence, not production-store batches. October's first wave only retained the summarized output, not raw response files, and must not be represented as replay-ready evidence. Probe code `preflight/probe_field_semantics.py` performs three calls, no credential logging, and exclusive artifact creation to avoid overwriting prior observations.

### Verified narrow historical PB fallback

Read-only BaoStock preflight (two anonymous queries) found 300157.SZ on 2026-01-05: unadjusted close 4.6400, PE_TTM 159.131502, PB_MRQ -17.568612, isST=0 and tradestatus=1. Saved Tushare same-date basic has PE_TTM 159.1315, PB=null and total_mv=349827.096万元. Thus a real signed historical PB is available; the source-null cannot be equated to ST or nontrading status.

The [BaoStock API documentation](https://github.com/zxygithub/baostock/blob/master/docs/pythonAPI.md) defines PB using latest disclosed parent equity minus other equity instruments, compatible with Tushare's stated denominator. The controller also checked the company's original [2025Q3 report on CNINFO](https://static.cninfo.com.cn/finalpage/2025-10-28/1224740791.PDF), published 2025-10-28, pages 10–11: parent equity 150879485.79元, other equity instruments 350000000元. Independent arithmetic `349827.096 * 10000 / (150879485.79 - 350000000) = -17.5686115209` matches BaoStock to its reported precision. The negative quantity is equity AFTER deducting the other instruments, not parent equity itself. The report is unaudited quarterly data; this is a denominator cross-check, not an audit of all vendor revisions.

Use only a bounded dated field fallback with price/PE agreement and explicit provenance. This observation proves one code/date and compatible formulas, not all-date availability or never-revised historical vintages. Do not infer why Tushare internally returned null. Unsupported BSE, mismatch, missing primary rows and other missing fields remain unresolved. BaoStock current basic metadata is not used to assert historical status; the dated daily record supplies the cross-check.

A separate bounded read-only probe (9 account calls, 3 public documentation reads) found usable historical intervals but did **not** prove a complete export:

- `namechange(fields='ts_code,name,start_date,end_date,ann_date,change_reason', limit=10000, offset=...)` returned 10,000 / 4,243 / 0 rows at offsets 0 / 10,000 / 20,000. Combined 14,243 rows had 333 exact duplicates, 5,919 distinct codes and one null announcement date.
- An overlap query at offset 9,998, limit 4 did not reproduce the corresponding large-page slice. Therefore pagination is not certified stable; do not equate a short terminal page with complete interval coverage.
- All 5,598 securities listed at some point in the evaluation span had at least one name record. Literal inclusive interval coverage nevertheless left 91 gaps on January 5 (5,379/5,470 matched) and 87 on October 16 (5,490/5,577 matched). These are probe gaps, not proof that the provider lacks those histories. Broader duplicate-overlap counts were not filtered to eligible securities and must not be quoted as contradictory eligible-stock counts.
- 920305 has `*ST云创` through 2026-07-08 and `云创退` from July 9 (announced July 1); 920680 has `广道退` from 2025-12-11 (announced December 3). Future-effective rows also occur, e.g. `萃华退` starting October 23 but announced October 16. Effective date and announcement date must both be checked.
- Actual `bse_mapping` fields are `name,o_code,n_code,list_date`; use these, not guessed alias field names. The exploratory ST endpoint counts did not apply aliases; sampled relevant histories already used 920 codes. Production must normalize before final coverage auditing.

Acquisition design: retain and exact-deduplicate broad name history, audit every historically listed security/trading-date pair, then issue per-security history requests only for gaps/conflicts. Merge duplicate evidence only if the effective status agrees. Parse ST variants separately from delisting arrangements; absent intervals are unknown, not non-ST. BaoStock dated `isST` can cross-check SH/SZ problem cases but cannot establish BJ or delisting-arrangement state. Do not derive status from a fixed price-limit percentage.

The [official namechange contract](https://tushare.pro/document/2?doc_id=100) describes input date filters as announcement dates, not interval coverage; a 2026-only filter would omit older names still effective in 2026. Preserve open-ended intervals, listing lifetime, announcement dates and raw evidence. This reconstructs provider historical state; it is not an independently archived never-revised vintage. No remaining gaps have yet been repaired or declared complete.

## Delisting date is not the last trading date

Controller directly read the [600225 company announcement 2025-021 on CNINFO](https://static.cninfo.com.cn/finalpage/2025-02-28/1222662254.PDF): last arrangement-period trading date 2025-02-27, suspension from 2025-02-28, termination/delisting 2025-03-06. The [SSE announcement](https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20250227_10773066.shtml) independently confirms the first and last dates. Thus neither delist_date nor its preceding exchange session proves an executable bar.

Read-only source research also located 920680's [BSE company announcement 2025-116](https://www.bse.cn/disclosure/2025/2025-12-31/8690dbac1d5c42db98e9c7388e562129.pdf): arrangement period 2025-12-11 through 2025-12-31, delisting 2026-01-05. Search indexing exposed its original text; direct web retrieval encountered a redirect loop, so this PDF is not represented as an installed or hash-verified production source. Its [earlier announcement](https://www.bse.cn/disclosure/2025/2025-12-03/042baf7419ce41de97c4eea63206174b.pdf) was identified as corroboration, not a substitute downloaded artifact.

Acceptance question for the whole-branch review: universe lifetime boundaries and warmup market coverage must not require a nonexistent normal-trading bar merely because a security remains in a master until its delisting date. Pre-delisting suspension still needs dated evidence; absence alone cannot prove a terminal suspension or authorize an invented fill. These samples establish source semantics, not the completeness of all delisting histories or a reason to remove later-delisted securities from earlier signals.
