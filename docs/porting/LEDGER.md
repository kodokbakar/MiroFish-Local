# Porting Ledger

Single source of truth for porting state across sessions and worktrees.
Only the orchestrator edits this file.

## Baseline
- Upstream remote: `upstream` -> https://github.com/666ghj/MiroFish
- Base commit (last upstream commit already contained in MiroFish-Offline): 985f89f49acbb44ee14d9d680682c741a44eeebe (v0.1.2)
- Last analyzed upstream commit: 7657031ac01184afe2cb220f5ee3545573b5e843

## Status values
`pending` | `in-progress` | `ported` | `skipped` | `blocked`

## Portability values
`direct` (cherry-pick works) | `adapt` (needs Neo4j/GraphStorage adaptation) | `skip`

## Items
| ID | Upstream commit | Issue | Class | Portability | Risk | Status | Branch/PR | Note |
|----|-----------------|-------|-------|-------------|------|--------|-----------|------|
| 001 | `32fa3c8` | AttributeError on GET /api/graph/tasks due to duplicate .to_dict() call on task dicts | bugfix | direct | low | ported | `ce13e80` | Ported and merged via PR/branch port/001; 17 tests passed. |
| 002 | `7c7c7a2` | Supply chain vulnerability risk from unpinned caret dependency (^1.13.2) on axios | bugfix | direct | low | ported | `67dd3ef` | Ported and merged via PR/branch port/002; 18 tests passed. |
| 003 | `e9cac5f` | Flask debug mode enabled by default on 0.0.0.0 exposing interactive console / RCE | bugfix | direct | low | ported | `bf2a25f` | Ported and merged via PR/branch kodokbakar/port-003; 22 tests passed. |
| 004 | `442d5fd` | Chat history filter drops earlier duplicate user messages when identical prompt re-sent | bugfix | direct | low | ported | `02fda4e` | Ported and merged via branch kodokbakar/port-004; 25 tests passed. |
| 005 | `e6a5021` | History cards display UTC date causing incorrect calendar date near local midnight | bugfix | direct | low | ported | `e70169e` | Ported and merged via branch kodokbakar/port-005; 30 tests passed. |
| 006 | `aaf3ea0` | Silent data loss when platform defaults to reddit for Twitter-only simulations | bugfix | direct | low | ported | `b372834` | Ported and merged via branch kodokbakar/port-006; 47 tests passed. |
| 007 | `b651883` | SimulationManager crashes attempting to load Twitter profiles from missing JSON file | bugfix | direct | low | ported | `f8ce87b` | Ported and merged via branch kodokbakar/port-007; 58 tests passed. |
| 008 | `1d96b09` | LLM hallucination in report agent caused by self-generated <tool_result> blocks | bugfix | direct | low | ported | `e3ed1f8` | Ported and merged via branch kodokbakar/port-008; 67 tests passed. |
| 009 | `f0e3794` | Fabricated tool_result tags across report paths bypass single-point sanitization | bugfix | direct | low | ported | `09e6555` | Ported and merged via branch kodokbakar/port-009; 77 tests passed. |
| 010 | `41fc932` | Structured LLM output returning dict/list in text fields crashes profile serialization | bugfix | direct | low | ported | `67fae64` | Ported and merged via branch kodokbakar/port-010; 85 tests passed. |
| 011 | `c81d628` | High severity security vulnerabilities in npm dependencies (axios, vite, concurrently) | bugfix | direct | low | ported | `c23084d` | Ported and merged via branch kodokbakar/port-011; 88 tests passed. |
| 012 | `f7138e0` | HTTP 500 error when using GPT-5 family models due to unsupported chat completion params | bugfix | direct | medium | ported | `fa3f346` | Ported and merged via branch kodokbakar/port-012; 111 tests passed. |
| 013 | `e67a52e` | Broad speculative error handling in GPT-5 compatibility layer causes retry bugs | bugfix | direct | low | ported | `fb9b3d0` | Ported and merged via branch kodokbakar/port-013; 115 tests passed. |
| 014 | `223b283` | Security CVE vulnerabilities in frontend build dependencies (axios, rollup, picomatch) | bugfix | adapt | low | ported | `9380f91` | Ported and merged via branch kodokbakar/port-014; 119 tests passed. |
| 015 | `fe2fd5b` | CVE-2025-14009 vulnerability in transitive dependency NLTK | bugfix | adapt | low | ported | `6638a69` | Ported and merged via branch kodokbakar/port-015; 127 tests passed. |
| 016 | `867b54c` | Frontend polling continues endlessly when simulation runner encounters fatal failure | bugfix | adapt | low | ported | `5e78cc0` | Ported and merged via branch kodokbakar/port-016; 133 tests passed. |
| 017 | `8b41460` | Runner failure status masked by platform completion flags in simulation polling | bugfix | adapt | low | ported | `0044622` | Ported and merged via branch kodokbakar/port-017; 136 tests passed. |
| 018 | `a07841e` | TypeError crash (#135) caused by LLM returning string instead of list for ontology attributes | bugfix | adapt | low | ported | `3c0e590` | Ported and merged via branch kodokbakar/port-018; 143 tests passed. |
| 019 | `f0d487f` | Ontology JSON generation crashes on diverse LLM providers with malformed JSON responses | bugfix | adapt | low | ported | `6d0eac5` | Ported and merged via branch kodokbakar/port-019; 180 tests passed. |
| 020 | `e3350a9` | Inconsistent naming casing and duplicate entity types in ontology generation | bugfix | adapt | medium | ported | `59508e6` | Ported and merged via branch kodokbakar/port-020; 215 tests passed. |
| 021 | `f0e936a` | Ontology generator truncates long texts to first chunk, missing critical context | bugfix | adapt | medium | ported | `307045d` | Ported and merged via branch kodokbakar/port-021; 225 tests passed. |
| 022 | `3f4d561` | Unconstrained Python versions lead to build failures on Python 3.13+ | bugfix | adapt | medium | ported | `74af732` | Ported and merged via branch kodokbakar/port-022; 235 tests passed. |
| 023 | `05bea4a` | Step 2 environment setup polling continues after background preparation failure | bugfix | adapt | medium | ported | `473d16b` | Ported and merged via branch kodokbakar/port-023; 245 tests passed. |
| 024 | `daec4b6` | Missing type hints in config/logger and missing FileParser.is_supported() helper | refactor | direct | low | ported | `0f2c83f` | Ported and merged via branch kodokbakar/port-024; 281 tests passed. |
| 025 | `9f80ad6` | Field normalization scattered across profile generation rather than centralized | refactor | direct | low | ported | `64dbd49` | Ported and merged via branch kodokbakar/port-025; 290 tests passed. |
| 026 | `9506e6f` | Ontology attribute normalization duplicated across graph builder and generator | refactor | adapt | low | ported | `c5c2864` | Ported and merged via branch kodokbakar/port-026; 300 tests passed. |
| 027 | - | Issue #4: Unkillable zombie subprocesses and IPC hijacking due to invalid status check in stop_simulation | bugfix | direct | medium | ported | `1755abe` | Fixed and tested on branch kodokbakar/unkillable-zombie-subprocesses-and-ipc-hijacking; 317 tests passed. |
| 028 | - | Issue #7: State-machine deadlock prevents single-platform simulation restart and status check | bugfix | direct | low | ported | `b51b464` | Fixed and tested on branch kodokbakar/state-machine-deadlock-prevents-single-platform; 506 tests passed. |
| 029 | - | Issue #8: Race condition between stop_simulation/start_simulation and monitor thread corrupts state and orphans processes | bugfix | direct | medium | ported | `2617d67` | Fixed and tested on branch kodokbakar/race-condition-between-stop_simulation-start_sim; 474 tests passed. |
| 030 | - | Issue #9: unhandled KeyError and AttributeError crash in ReACT loop and chat endpoint during tool call parsing | bugfix | direct | low | ported | `0f806e8` | Fixed and tested on branch kodokbakar/fix-report_agent-unhandled-keyerror-and-attribut; 499 tests passed. |
| 031 | - | Issue #10: Unhandled TypeError and AttributeError on malformed source_targets and entity_types in ontology validation | bugfix | direct | low | ported | `59cae4c` | Fixed and tested on branch kodokbakar/unhandled-typeerror-and-attributeerror-on-malfor; 476 tests passed. |

## Skipped (with reason)
| Upstream commit | Reason |
|-----------------|--------|
| `1536a79` | fix(readme): update Discord link to valid invite URL — Upstream Discord invite link update in README files; cosmetic documentation change not needed in local fork. |
| `c8a1bd5` | feat(i18n): add shared translation files and language registry — Adds shared zh/en translation files in locales/; touches locales folder and redundant for English-only fork. |
| `2ffadd3` | feat(i18n): add Accept-Language header to all API requests — Injects vue-i18n Accept-Language header into Axios requests; redundant for English-only fork. |
| `0c18e1a` | feat(i18n): add backend translation utility with shared locale files — Adds backend locale.py translation utility loading locales/; redundant for English-only fork. |
| `22bf50f` | feat(i18n): set up vue-i18n with dynamic locale loading — Sets up vue-i18n dependency and dynamic locale loader; redundant for English-only fork. |
| `8f6110d` | feat(i18n): inject language instruction into LLM system prompts — Appends dynamic get_language_instruction() to LLM prompts; local fork already has fixed English prompts. |
| `3d5e5d0` | feat(i18n): add language switcher component to navigation — Adds LanguageSwitcher component and mounts it in navigation views; redundant for English-only fork. |
| `74f673a` | feat(i18n): replace hardcoded Chinese in backend API responses with t() calls — Replaces Chinese backend API messages with t() calls; touches locales/ and local fork already has English API responses. |
| `7083382` | feat(i18n): replace hardcoded Chinese in frontend views with i18n calls — Replaces Chinese text in Vue views with vue-i18n calls; redundant since local views are already English. |
| `fc47ae8` | feat(i18n): replace hardcoded Chinese in frontend components with i18n calls — Replaces Chinese strings in Vue step components with t() calls; touches locales/ and redundant for English-only fork. |
| `fbaa3db` | fix(i18n): fix curly quotes and missing @ alias in vite config — Fixes curly quotes in i18n template tag and adds @ alias for vue-i18n; redundant without upstream i18n stack. |
| `f75c648` | fix(i18n): replace remaining hardcoded language directives in LLM prompts — Modifies Zep tools and prompts to inject dynamic locale directives; not applicable to English-only fork without Zep. |
| `ffe6369` | fix(i18n): fix curly quotes in Home.vue and remove unused dark theme from LanguageSwitcher — Fixes quotes in Home.vue i18n bindings and removes unused LanguageSwitcher styles; not applicable to local fork. |
| `9d43b77` | feat(i18n): replace hardcoded Chinese in backend SSE progress messages — Converts SSE progress messages to t() translation keys; touches locales/ and redundant for English-only fork. |
| `e1db8ba` | feat(i18n): replace hardcoded Chinese in frontend addLog() messages — Replaces Chinese addLog() frontend messages with $t() calls; redundant since local logs are already in English. |
| `97aa583` | fix(i18n): ensure ontology names stay PascalCase regardless of language setting — Guards ontology generator against translation side-effects from dynamic language injection; not needed in English-only fork. |
| `da2490e` | fix(i18n): protect JSON field values from language instruction in config generator — Guards enum fields (poster_type, stance) against upstream i18n translation injection; not needed in English-only fork. |
| `592ee52` | feat(i18n): replace remaining hardcoded Chinese in progress callbacks — Replaces Chinese progress messages with t() calls; touches locales/ and Zep profile generator, redundant for local fork. |
| `7c07237` | fix(i18n): pass locale to background threads via thread-local storage — Propagates thread-local locale to background workers and touches Zep updater; not needed in English-only fork without Zep. |
| `0e55e4c` | feat(i18n): replace remaining Chinese in config generator and profile generator — Replaces Chinese text with t() calls; touches locales/ and Zep profile generator, redundant for local fork. |
| `3a8451c` | feat(i18n): replace remaining hardcoded Chinese in frontend addLog calls — Replaces Chinese frontend addLog messages with $t() calls; redundant since local frontend is already English. |
| `380e456` | fix(i18n): replace hardcoded Chinese stage names in simulation prepare SSE — Replaces Chinese simulation stage names with t() calls; touches locales/ and redundant for English-only fork. |
| `1d358fc` | feat(i18n): replace expand/collapse Chinese text in Step4Report.vue — Replaces expand/collapse Chinese UI text in Step4Report with $t() calls; touches locales/ and redundant for English-only fork. |
| `e79569a` | feat(i18n): replace all user-visible Chinese in report_agent.py — Replaces Chinese ReportLogger messages with t() calls; local fork report agent is already in English. |
| `24e9bee` | feat(i18n): replace all user-visible Chinese logger messages in zep_tools.py — Replaces Chinese logger messages in zep_tools.py with t() calls; pure Zep-specific change, not applicable to local GraphStorage fork. |
| `6db3f98` | fix(i18n): fix English homepage layout with proper font and shorter copy — Adjusts English typography via html[lang=en] CSS and edits locales/en.json; touches locales/ and local fork already has custom English layout. |
| `5072a2e` | feat(i18n): replace Chinese UI text in Step4Report.vue render functions — Replaces Chinese UI strings in Step4Report render functions with t() calls; touches locales/ and local fork is already translated to English. |
| `c6cafdd` | fix(i18n): translate world1/world2 platform labels in interview tool display — vue-i18n translation strings; local fork has English hardcoded and no locales directory |
| `21922da` | fix(i18n): improve English layout for homepage left-pane and report title — Dual-locale CSS rules and locales/en.json changes; conflicts with English-only fork style architecture |
| `3929c3a` | fix(i18n): further shorten English metrics and improve workflow layout — CSS adjustments and text shortening in locales/en.json for upstream dual-locale setup |
| `2421010` | fix(i18n): fix English workflow desc font size with correct CSS selectors — CSS selector fix for lang=en workflow descriptions; local fork moved styles to reactive JS objects |
| `f240490` | fix(i18n): validate Accept-Language header against registered locales — Accept-Language header validation in backend locale.py; file not present in English-only fork |
| `65df257` | chore(deps): upgrade vue-i18n from v9 to v11 — Upgrades vue-i18n dependency; package not installed or used in English-only fork |
| `ed46590` | fix(i18n): set HTML lang attribute before Vue mounts via inline script — Inline script dynamically setting html lang from localStorage; local fork has static English html lang |
| `af71244` | Merge pull request #428 from Ghostubborn/feat/i18n — Merge commit for PR #428 (multi-language framework); conflicts with English-only fork and touches Zep tools |
| `7d07fb7` | Merge pull request #440 from Ghostubborn/fix/security-deps — Merge commit for PR #440 (security dependency updates covered in 223b283) |
| `0e9420e` | docs: rename README.md to README-ZH.md for Chinese documentation — Renames README.md to README-ZH.md in upstream repo; local fork maintains its own documentation |
| `fa0f651` | docs: rename README-EN.md to README.md as default English documentation — Renames README-EN.md to default README.md in upstream; local fork maintains its own documentation |
| `faa1511` | docs(readme): fix typo in Shanda logo alt text — Fixes URL encoding typo in upstream README sponsor logo; local fork does not track upstream README |
| `db1bc14` | Merge pull request #641 from YunyueLi/docs/readme-polish — Merge commit for PR #641 (README logo alt text typo fix) |
| `96096ea` | Merge pull request #640 from lllopic/fix/add-type-hints-and-helper-method — Merge commit for PR #640 (type hints and helper method covered in daec4b6) |
| `4ebb2ed` | feat: localize star history — Upstream GitHub star history scripts and CI automation; out of scope for local fork |
| `086f1fb` | Merge pull request #305: fix graph tasks endpoint crash — Merge commit for PR #305 (to_dict removal in /api/graph/tasks already covered in 32fa3c8) |
| `cbfd03d` | Merge pull request #727: upgrade NLTK to 3.10.0 — Merge commit for PR #727 (NLTK upgrade covered in fe2fd5b) |
| `231ec22` | Merge pull request #445: disable Flask debug mode by default — Merge commit for PR #445 (Flask debug mode security fix covered in e9cac5f) |
| `fbdae29` | Merge pull request #720: keep history date and time in the same local timezone — Merge commit for PR #720 (history local date fix covered in e6a5021) |
| `ad12d41` | Merge pull request #598: preserve repeated user messages in chat history — Merge commit for PR #598 (chat history filter fix covered in 442d5fd) |
| `53af276` | Merge pull request #584: sample long ontology input across the document — Merge commit for PR #584 (ontology sampling fix covered in f0e936a) |
| `e58d4f1` | fix: add max_items limit to fetch_all_edges to prevent unbounded memory growth (#513) — Edge pagination limit for Zep Cloud client; local fork uses Neo4j GraphStorage without Zep |
| `7d6f6bb` | fix: preserve edge paging call compatibility — Pure Zep Cloud edge paging argument ordering; inapplicable to Neo4j. |
| `ce616b0` | Merge pull request #730: cap Zep edge pagination safely — PR merge commit for Zep edge pagination capping. |
| `564a560` | ci: build docker image for arm64 — Adds arm64 to upstream Docker build CI; CI push skipped per policy. |
| `898f3b5` | ci: make manual Docker publishing opt-in — Opt-in Docker publishing workflow; CI workflow skipped per policy. |
| `66f2bb6` | Merge pull request #731: add guarded ARM64 Docker publishing — PR merge commit for ARM64 Docker workflow; skipped per policy. |
| `e164565` | Merge pull request #732: prioritize terminal simulation failure — PR merge commit for simulation polling failure priority. |
| `ca845b0` | Merge pull request #733: support Twitter-only simulation retrieval — PR merge commit for Twitter retrieval and CSV loader. |
| `7970d74` | Merge pull request #734: sanitize fabricated tool results — PR merge commit for report agent tool result sanitization. |
| `2576c06` | Merge pull request #735: stop Step 2 after preparation failure — PR merge commit for Step 2 preparation failure propagation. |
| `666a188` | Merge pull request #736: handle GPT-5 parameters deterministically — PR merge commit for GPT-5 parameter compatibility handling. |
| `c415387` | style: remove trailing whitespace from profile model — Trailing whitespace cleanup; skipped per cosmetic policy. |
| `4064b68` | Merge pull request #737: normalize generated profile fields — PR merge commit for OASIS agent profile normalization. |
| `d2f9e56` | fix: enforce Zep ontology contract limits — Enforces Zep Cloud-specific ontology contract limits (10 types, reserved Zep fields); not applicable to Neo4j. |
| `aa6abf3` | Merge pull request #738: normalize ontology fields for Zep — PR merge commit for Zep ontology normalization. |
| `0d8c23a` | fix: use the supported Zep node-edge method — Pure Zep Cloud SDK method call fix (`node.get_edges`); inapplicable to Neo4j. |
| `dd7a646` | Merge pull request #739: use the supported Zep node-edge method — PR merge commit for Zep SDK get_edges fix. |
| `e4aa38d` | fix: modernize Zep Cloud integration — Modernizes upstream Zep Cloud integration; conflicts with Neo4j GraphStorage architecture. |
| `54bd7a3` | fix: validate complete Zep entity context — Zep Cloud entity context validation script and test harness; not applicable to Neo4j |
| `407c391` | fix: keep Zep timeouts internal — Internalizes Zep timeout configurations and removes env vars; purely Zep Cloud-specific |
| `d641759` | Merge pull request #740 from 666ghj/codex/zep-cloud-comprehensive-audit — Upstream merge commit combining PR #740 |
| `60757b3` | Merge pull request #742 from 666ghj/codex/fix-ontology-provider-compatibility — Upstream merge commit combining PR #742 |
| `a97ba4f` | Fix automated Star History updates on protected main (#752) — Upstream GitHub Actions Star History workflow and test; not applicable to fork |
| `0f67b29` | fix: submit Star History updates as pull requests (#754) — Upstream GitHub Actions Star History PR submission workflow fix and test; not applicable to fork |
| `b5b53ac` | chore: update Star History (#755) — Automated Star History SVG and JSON asset update |
| `117ed37` | chore: update star history [skip ci] (#778) — Automated Star History SVG and JSON asset update |
| `39d8491` | chore: update star history [skip ci] (#792) — Automated Star History SVG and JSON asset update |
| `14e7201` | chore: update Star History (#815) — Automated Star History SVG and JSON asset update |
| `7657031` | chore: update Star History (#832) — Automated Star History SVG and JSON asset update |
