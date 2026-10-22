# Porting Ledger

Single source of truth for porting state across sessions and worktrees.
Only the orchestrator edits this file.

## Baseline
- Upstream remote: `upstream` -> https://github.com/666ghj/MiroFish
- Base commit (last upstream commit already contained in MiroFish-Offline): TODO
- Last analyzed upstream commit: TODO

## Status values
`pending` | `in-progress` | `ported` | `skipped` | `blocked`

## Portability values
`direct` (cherry-pick works) | `adapt` (needs Neo4j/GraphStorage adaptation) | `skip`

## Items
| ID | Upstream commit | Class | Portability | Risk | Status | Branch/PR | Note |
|----|-----------------|-------|-------------|------|--------|-----------|------|
| 001 | TODO | bugfix | direct | low | pending | | |

## Skipped (with reason)
| Upstream commit | Reason |
|-----------------|--------|
