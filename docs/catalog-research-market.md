# Catalog research market

Set `PANDA_CATALOG_RESEARCH_MARKET=RU` in the deployment environment to enable
Russian-market research for catalog enrichment. Unset preserves the existing
unscoped behavior. This is deployment configuration, not a universal model rule.

For tenant-specific settings, `PANDA_CATALOG_RESEARCH_MARKETS` accepts a JSON object:
`{"tenant-a":"RU","tenant-b":""}`. An explicit tenant value overrides the deployment
default. Only RU and empty/unscoped are currently supported; invalid settings fail
preparation rather than silently widening research.

RU research:
- Queries the registered manufacturer's Russian site section first, then Russian
  catalogs (at most two research searches, with URL deduplication).
- Rejects foreign/global pages before fetching specifications or discovering media:
  accepted scopes are .ru/.рф domains, /ru/ localized paths and /region/russia/
  or /region/ru/ document paths. This is source scoping, not proof of legal
  distribution or country of manufacture.
- Reuses the existing manufacturer-domain registry and confidence rules.
  Brands absent from that registry still get Russian catalog search; their sources
  are not automatically promoted to official manufacturers.
- Includes market in the identity/cache key; tenant cache isolation remains.
- Reports the research market, cache usage, fact-source URLs and retrieval times
  in the selected-product write preview.

An explicit refresh ("заново", "повторно", "перепроверь", "обновлённый",
"refresh", "recheck") clears the selected identity's cache entry before research
and replaces it with the new result. Ordinary preview can reuse cache.
Neither refresh nor RU configuration authorizes Bitrix writes. Existing approval,
brand-before-product creation, inactive status and verified property bindings apply.
Already frozen plans are not silently migrated: request a fresh preview.

Remaining limits: this change does not implement PDF/OCR extraction, discover
missing Bitrix properties, or certify every regional specification. Empty/conflicting
evidence stays unresolved. Verify live selected-product output after deployment.

## Trusted source policy
RU specification research now requires both Russian scope and a recognized
manufacturer or configured catalog domain. Blogs, arbitrary .ru domains and
manufacturer support pages do not become specification/media sources.
Default catalog domains: mvideo.ru, dns-shop.ru, citilink.ru, eldorado.ru,
technopark.ru. Override the comma-separated list with
`PANDA_RESEARCH_CATALOG_DOMAINS` after verifying the catalog.
Extend/override a brand's official domains with
`PANDA_RESEARCH_MANUFACTURER_DOMAINS` JSON, e.g.
`{"example-brand":["manufacturer.example"]}`. Configuration does not create
any brand element or special per-model behavior.

Exact-product headings from these fetched pages supply independent category
evidence through the existing category vocabulary. Conflicting categories are
not resolved by guessing; explicit supplier categories keep priority.
Resolution extraction requires resolution/pixel context and excludes dimension
units/triples. Linked model/size selectors are not spec values. Differing
structured values on the same page remain contradictions rather than selecting
the first row silently.
