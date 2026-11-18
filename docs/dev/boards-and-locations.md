# Boards, slugs and locations

> Developer notes, moved verbatim from the root `CLAUDE.md`. Not loaded every
> session: the root file points here, and the nested `CLAUDE.md` beside the code
> imports it when Claude works in that directory.

## Slug lists and discovery

Consequences already worked out, which should not be re-derived:

- **There is no dead-slug skip list, and the seed-plus-delta scheme that used to
  solve it must not come back.** Every slug in an enabled board's file is tried on
  every run. A 404 is recorded as a `not_found` row in `run_companies` for that run
  and changes nothing for the next one.

  What was removed: a curated `config/bad_slugs.json` in ROOT (15,584 slugs), plus
  `user_bad_slugs.json` and `user_recovered_slugs.json` deltas in DATA, combined as
  `seed ∪ user_bad − user_recovered`, with `scripts/verify_bad_slugs.py --prune`
  writing recoveries as a delta rather than editing a file about to be replaced. All
  three files and that script are gone.

  **The reason is the failure direction, not the bookkeeping** — the ROOT/DATA
  layering was correct and is what makes this tempting to rebuild. The list was read
  once before a sweep and never re-checked during one, so it could only grow: a slug
  that 404'd through a transient outage, or a company that moved boards and came
  back, was skipped on every future sweep. The only road back was a terminal command,
  in a plugin whose premise is that users never open a terminal. So a wrong entry was
  permanent and invisible, and it landed on exactly the employers a user would most
  want re-checked. That is the same rule as `_RETRYABLE_SKIP_REASONS` below: a 404 on
  one sweep is a deferral, not a verdict, and the two must not be confused.

  The price, accepted: a default sweep goes from ~9,805 companies to all 15,871, and
  ~6,066 of those requests get a 404. It is paid in a phase that is already I/O-bound
  and rate-limited per board. `docs/SPECS.md`'s default-sweep figure was always the
  unfiltered one, so it needed no correction — it is simply true now.
- **The Greenhouse, Lever, Ashby and Gem lists grow only through
  `scripts/discover_slugs.py`, and it only ever adds.** With no skip list every
  shipped slug is a request on every sweep for every user, so a slug is admitted only
  when the board's own API (the scraper's `BASE_URL`) answers with at least one
  posting. Removal is deliberately absent: it would be the 404-as-verdict the bullet
  above forbids, just made by hand. `tests/test_discover_slugs.py` fails if a removal
  path appears.

  Candidates come from Common Crawl's URL index, which lists URLs its crawler fetched
  and never guesses. So coverage is whatever the crawler reached: one crawl found
  ~1,100 new live Greenhouse and Ashby boards, and **Lever almost nothing**, because
  the crawler barely visits `jobs.lever.co` (its robots.txt allows it). `--extra
  lever=<list>` runs any other list through the same check, and is Lever's source.

  **The index server lies with a 200.** It can cut a page off mid-URL and still
  answer 200; measured, 8,105 lines came back as 2,232, and the first version of this
  tool cached that and undercounted Ashby by half. `page_is_complete` rejects a page
  that does not end on a whole record, pages are one index block (`pageSize=1`, ~4 s)
  rather than the server's 5, and nothing is cached until whole. It also answers
  502/504 often and the occasional transient 400, so a failed page is reported and
  skipped, and a rerun fetches only that page. Do not "simplify" the completeness
  check away because the status code looks fine.

  **Gem's posting gate is not enough on its own.** Gem (`jobs.gem.com`, API
  `api.gem.com/job_board/v0/<slug>/job_posts/`) publishes no sitemap, so the crawl is
  its only source, and its customers' sandbox and test boards are public and carry fake
  postings — `oats-testing` had 133 — so they pass the "at least one posting" check.
  `Board.reject` drops them *before* the check, and only from candidates not yet
  shipped, so it is admission, never removal. The rule in `_GEM_JUNK` was read off a
  real crawl and is narrower than it looks on purpose: `test` matches only where a word
  starts, because `turbotest-ai` is a real QA company, and bare `gem` (Gem's own
  careers page) is kept while Gem's `gem-*` staff boards are not.

  **Gem reached existing installs by a load-time rule, not by the default.** The
  user's `scraper.yaml` is in DATA, so `enabled_platforms` never receives a shipped
  default. `config._with_new_defaults` appends `gem` when the list still holds all four
  pre-Gem defaults, and leaves a trimmed list alone because trimming was a choice. The
  accepted cost: with those four on, Gem cannot be switched off by itself. A future
  board that should reach existing installs needs the same treatment, with its own
  pre-board baseline. **Oracle Recruiting Cloud is the second such board**:
  `_PRE_ORACLE_DEFAULTS` is the five defaults before it, and the rule runs *after* the
  Gem rule, so an install from before Gem that never trimmed its boards gains both.

- **Oracle Recruiting Cloud employers are rows in `config/oracle_companies.json`, grown
  only by `scripts/discover_oracle.py`** (add-only, the sibling of
  `discover_eightfold.py`). `hireshire/oracle_tenants.py` is the only reader. Five
  things were measured on live pods and fail silently if undone:
  - **Everything goes to the pod, `<pod>.fa.<region>.oraclecloud.com`, never the
    employer's careers domain.** `careers.americanexpress.com` answers the API with a
    redirect to a 404 page; the pod host is written in that page, which is how
    `--extra` turns a vanity URL into a row.
  - **A pod answers an unknown site number with its default site's jobs.** On BDO,
    `CX`, `CX_xx` and `jobsearch` all returned the same 675 as `BDOExperiencedCareers`,
    while the real second site `CX_1` had jobs the main one lacks. So every crawled
    site "works": `prune_sites` keeps a site only if its first page reaches ids the
    kept ones do not, and the scraper still dedupes a pod's sites by requisition id.
    Do not drop the pruning because every site answered 200.
  - **Requisition ids are small integers that repeat between employers**, so `job_id`
    is `oracle:<token>:<id>`. A bare id would make the overview's buttons ambiguous.
  - **`PostedDate` has no time, and it is read as the END of its day** (capped at the
    scrape), not midnight. `scraper.py`'s age gate compares instants, so a midnight
    reading drops a job posted at 22:00 UTC from the 01:00 sweep that first sees it —
    and from every later sweep, so it is never seen at all. The end-of-day reading can
    only keep a job up to a day too long. The newest-first walk (`limit` caps at 200)
    stops on a whole page dated before the cutoff's *day*, and the detail call
    replaces the date with the exact `ExternalPostedStartDate`.
  - **The list has no description**, so `oracle` is in `DETAIL_SOURCES` and
    `detail_path` is `host|site|id`: the funnel's detail fetcher holds no employer
    list. The detail also adds the secondary locations, which the list omits, so the
    location gate at scrape time sees only the primary one.

  The token is a readable slug of the site page's title, because the dashboard prints
  `board_token` as the company; a clash with another board's slug gets `-oracle`.
  Every Oracle token is in the applier's exclusions: guest apply asks for an emailed
  code, which would end each session in Needs Attention anyway.

## Board defaults, direct portals, location gate

- **Board defaults.** Workday and BambooHR default **off**, and they are the two
  biggest lists, so the default sweep is well under half of what ships. Setup presents
  it as a time trade-off — and **no specific multiplier has been measured yet**, so say
  "considerably longer", not "3x".

  **Company counts live in exactly two places**: `README.md`'s first line (rounded
  down, e.g. "N,000+") and the Scale table in `docs/SPECS.md` (exact per board, plus the default
  sweep). Re-derive both from `config/*_companies.json` when a board or slug batch is
  added. Do not copy totals into skills, manifests, config comments or other docs — they
  grow every release, and every copy went stale (the shipped `scraper.yaml` comment was
  off by nearly half).
- **The direct portals are searched for the user's countries, derived from
  `scraper.location_filter`.** `hireshire/direct/scope.py` resolves each term (country,
  state, city, `remote - us`) against `portal_locations.COUNTRIES`, and each handler
  builds its list URL from the result. Three rules, each learned from the portals:
  - **The portal codes are a checked-in table, never looked up at sweep time.** Apple and
    Google answer an unknown location with **zero results and a 200**, so a bad code
    empties the board silently every sweep. Apple's codes are irregular (`USA`, `GBR`
    but `INDC`, `CANC`, `AUSC`) and its lookup answers `georgia` with the Republic of
    Georgia. `scripts/refresh_direct_locations.py` re-derives the columns by hand.
    Intuit's free-text `Location=` is ignored outright; it scopes by a GeoNames country
    facet, which exists only where Intuit has openings, so its column is sparse.
  - **Anything unresolvable widens to everywhere; nothing narrows.** One unknown term,
    a bare `remote`, or a country missing from one portal's column leaves that portal
    unscoped. A narrowed scope hides jobs with no sign; a wide one only spends pages.
  - **A job whose list entry names no place carries `location_is_placeholder`**, and
    `scraper._matches_location` passes it. Google's list has no locations and Intuit's
    says "Multiple Locations"; a filter of cities or states never contains the country
    placeholder, which is how Google's whole board was once dropped and every Intuit
    multi-city job with it. The price, accepted: such a job reaches scoring unchecked
    until the applier's location verdict (`mark_not_shortlisted`) retires it.

- **The location gate normalises the job, never the terms, and must not go back to a
  raw substring test.** `scraper._matches_location` matches `location_filter` against
  `locations.filter_haystack(raw)` — the employer's string plus its inferred country,
  the spelled-out name of a US state written as an abbreviation, and every alias of
  that country. The two sides are written by different people and almost never agree:
  users write `california` or `united states`, employers write `Foster City, CA`.
  Measured on a real install, the raw test **silently discarded 15% of every job
  scraped** (84.7% → 99.2% of 447,742 rows), and it discarded them per employer rather
  than at random: all 236 of Zoox's Lever postings bar the 46 whose alternate office
  happened to be a city someone had typed out by hand. A user naming only a state was
  worse — `["california"]` admitted 11%, against 28% now.

  Four things here that look redundant and are not:

  - **`filter_haystack` is not `normalize_location`.** The latter appends only the
    country, and the six direct handlers **store** what it returns, so it is a string a
    user reads; its exact output is pinned by tests. The haystack is matching-only text
    and is where new synonyms go. Folding the two together puts `", california, usa,
    u.s., us, america"` on the overview page.
  - **The country's aliases are appended because the terms are matched verbatim.**
    Setup records what the user typed, so `usa`, `us` and `america` have to be found in
    the haystack — none is a substring of `United States`.
  - **The gate still fails closed, with one exception.** A location that resolves to no
    country is dropped, so `Portugal` and `Ukraine` stay out of a US+India scope. The
    exception is a **remote-shaped** location when the filter contains a `remote` term:
    `Remote` cannot be rolled up to a country, so inference has nothing to answer with.
    That is why a setup writing short country terms must still write a `remote` term for
    a user who wants remote work.
  - **`US_STATE_BY_ABBREV` is the only state table.** `US_STATES` and
    `US_STATE_ABBREVS` are derived from it. They were two hand-kept tuples and could
    not map `CA` → `california` between them, which is the whole reason a state-level
    filter failed. `_US_MARKER_RE` is derived from the table's aliases for the same
    reason — the hand-copy it replaced had drifted and lacked bare `us`, so thousands of
    `Remote - US` postings inferred nothing.

  **Every direct portal is plain HTTP, including the two once thought browser-only.**
  Do not bring back a browser path for either:
  - **Microsoft** is `/api/pcsx/search`. The 403 "Not authorized for PCSX" comes from
    `/api/apply/v2/jobs` only. Its page is fixed at 10 and `location=` takes one
    country (a second is silently ignored), so it walks one series per country.
  - **Lumen, Qualcomm and Applied Materials are Eightfold tenants, like Microsoft**, and share its walk in
    `handlers/eightfold.py` and one scope column (`Country.eightfold`): `location=` is
    free text geocoded by the platform, not the tenant. Their own search pages put
    `India OR United States` in the URL, and the API **silently reduces that to one
    country** (Lumen: US 113, India 14, the `OR` form 113) — so do not "simplify" the
    per-country walk into one request. Read `locations`, never `standardizedLocations`:
    on Lumen the short form renders "Remote, United States" as `Remote, OR, US`, Oregon.

    **Every other Eightfold tenant is a row in `config/eightfold_tenants.json`, not a
    module.** `hireshire/direct/tenants.py` is its only reader, so the company loader,
    `direct._HANDLERS` and the applier's exclusions cannot disagree; a module wins a
    token clash. Rows come only from `scripts/discover_eightfold.py`, which is add-only
    like `discover_slugs.py` and reuses its index plumbing. Four things it learned:
    - **A tenant is a host, so the candidate is the subdomain of `*.eightfold.ai`** and
      `domain=` is read off the crawled URLs' own query strings, then the careers page's
      escaped config. It is **never guessed**: a wrong one answers zero positions, which
      reads as an empty board. `volkscience.com` (Eightfold's legal name) is the
      placeholder an unconfigured host reports, and is never a tenant's domain.
    - **`base` is the tenant's own host and is never shared.** `app.eightfold.ai`
      answers Lumen by `domain=` but gives Boston Scientific zero, and a moved tenant's
      careers page redirects to its own domain, which becomes `base`.
    - **A token that is another board's slug gets `-eightfold`.** The applier excludes
      by company token, so a bare `deloitte` tenant would have switched auto-apply off
      for Lever's `deloitte` board. `tests/test_discover_eightfold.py` fails on a clash.
    - **Not every tenant serves PCSX.** Netflix answers 403 on `/api/pcsx/search` and
      426 jobs on the older `/api/apply/v2/jobs`, a different shape the walk does not
      read; such tenants stay `unverified`, as do BNY and American Express, which have
      moved off `eightfold.ai` (404) to vanity domains `--extra` can add.
  - **Rivian** is a Jibe board (`/api/jobs`, 100 a page, description in the list) with
    **no scope column**: its `country=` answers an unknown value with 0 jobs and a 200,
    and the facet exists only where Rivian has openings. The board is ~1,000 jobs, so it
    is read newest-first to the cutoff and the location gate narrows it, like Meta. Its
    location is built from `city`/`state`/`country`, never `full_location`, which drops
    the country.
  - **Meta** answers 400 until a request carries browser fetch metadata (`Sec-Fetch-*`,
    `Origin`, `Referer`). With it, one GraphQL POST returns the whole board (~1,000
    jobs), so Meta has **no scope column** and `scraper.py` filters afterwards. Its
    `offices` filter wants exact names and empties the board on a wrong one.
    `meta.DOC_ID` is **checked in**: it is in neither the page nor its eager bundles.
    Meta rotates it, and a stale one answers 404, an error row rather than an empty
    board. The module docstring says how to refresh it. Its list has no date, so the
    first sweep takes in Meta's whole backlog once and `postings.retired_at` handles
    the rest.

