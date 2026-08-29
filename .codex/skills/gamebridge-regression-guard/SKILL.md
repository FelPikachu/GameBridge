---
name: gamebridge-regression-guard
description: Protect previously accepted GameBridge behavior while diagnosing bugs, changing code, refactoring, packaging, installing, or releasing. Use for any GameBridge change that can affect a platform, provider, launch route, runtime, storage, cleanup, Steam integration, UI state, account boundary, or an already tested feature.
---

# Guard GameBridge against regressions

Treat accepted behavior as a product contract, not as an implementation detail.

## Establish the baseline

1. Locate the repository root and read `AGENTS.md`, `docs/verified-baseline.md`, and `docs/protected-baseline.json` completely.
2. Before any edit, run `git rev-parse --show-toplevel` and confirm the root is this GameBridge repository. Run `python3 scripts/regression_guard.py --baseline-only`; stop if it reports that the repository does not descend from the pinned beta4 commit or a locked test is missing.
3. Preserve the distinction between:
   - installed-package real-device acceptance;
   - partial real-device observation;
   - automated-test coverage;
   - unverified behavior.
4. Never promote a result to real-device acceptance from unit tests, logs alone, source inspection, or an unpackaged local command.
5. Read any more specialized project Skill that matches the task when it exists.

## Before changing code

1. State the user-visible behavior being changed and the behavior that must remain unchanged.
2. Map the likely blast radius by platform, game, region, storage location, shared Prefix, runtime, Steam shortcut, and cleanup behavior.
3. Inspect the relevant current code and tests. For a previously successful launch or channel route, compare the last known-good package before replacing the route.
4. Prefer the smallest correction. Do not combine a bug fix with unrelated cleanup or style rewriting.
5. Never replace the repository or a functional subtree with files from an older ZIP, backup, extracted package, or unrelated working directory. Migrate a reviewed change into the protected repository instead.

## Build the protection

1. Add or update an automated regression test for every behavior that can be checked without a real game or external account.
2. Preserve Provider and region isolation, user-selected storage, external game files, credentials, Decky, and unrelated plugins.
3. When shared code changes, test every locked contract that uses the shared path, not only the game or button named in the current request.
4. Run `python3 scripts/regression_guard.py` before calling the change complete. A narrower diagnostic run may come first, but it does not replace the full guard.
5. A missing locked test, an unrecognized repository root, or a history that does not descend from the pinned beta4 commit is a hard stop, not a warning.

## Real-device and release gate

1. Record real-device evidence in `docs/verified-baseline.md` only after the installed package reproduces the result.
2. Record device class, date, package version or hash, exact flow reached, and any limitation. Never record account secrets, device identifiers, tokens, or full private paths.
3. If a legal agreement, account login, purchase, large download, or other user decision blocks the final step, stop there and mark only the observed stage as passed.
4. Do not say "fixed", "fully working", or "released" when an affected locked real-device contract has not been rerun. Say exactly what is automated, observed, or awaiting verification.
5. If a locked behavior regresses, block the release or explicitly obtain the user's decision to accept the regression. Do not silently downgrade the baseline.
6. Build release ZIPs only through `scripts/build_plugin_zip.py`. It invokes `scripts/regression_guard.py --release` and must refuse packaging when the gate fails. Do not bypass this by calling `zip` directly.
7. Before delivering a new GameBridge ZIP to the desktop, create the project `releases/` backup and verify both files have the same SHA-256. Then move older desktop GameBridge ZIPs to Trash so the desktop keeps only the newest ZIP. Never include non-GameBridge files, cleanup tools, or project backups in that cleanup.

The baseline is cumulative: add newly accepted platforms and individual features to the same ledger instead of creating one Skill per feature.
