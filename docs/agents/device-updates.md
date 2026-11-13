# Device updates

Use lightweight downloads for comma devices. Keep development history on the source repositories; deploy the exact commits selected by the superproject.

## Policy

Develop against commaai/sunnypilot updates while preserving the required korea, Tesla, and MADS changes. Validate that combined tree before advancing deployment pins; devices receive the validated combination rather than independently following upstream HEADs.

- Fetch the selected superproject branch and changed submodule commits with `--depth=1 --no-tags`. Disable recursive fetching while fetching the superproject so it cannot download submodules before their filters are configured.
- Keep submodules at the superproject's gitlinks. `git submodule update --remote` selects a different version and is not this workflow. Keep required Tesla/MADS changes and the existing upstream/fork URLs.
- For tinygrad, combine `--filter=blob:none` with sparse checkout patterns `/*` and `!/test/mockgpu/`. Set the patterns before checking out the target commit. A shallow clone alone still downloads the large test fixtures in that commit.
- Exclude only audited runtime-independent files. Other test folders remain until their runtime/build dependencies are checked. Keep model files, LFS integrity checks, calibration, panda firmware, and AGNOS verification.
- Reuse completed LFS objects and already-recorded AGNOS partitions. Retain existing cached Git packs; shallow fetching does not automatically reclaim their disk space. No history rewrite or new snapshot fork is required.

## Normal workflow

On a build containing the lightweight updater, use the normal Software check/download controls. Keep `UpdaterManualOnly` behavior: lightweight fetching does not authorize automatic checks or downloads. The updater owns its existing lock, prepares the staging overlay, fetches the selected branch at depth one without recursive fetching, and initializes tinygrad's filter and exclusions before checking it out. Other submodules use depth-one initialization at the recorded gitlinks, including nested modules. Existing cached history may remain when no fetch is needed.

LFS downloads, AGNOS verification, final copying, and consistency markers remain in the normal update path. A failed fetch, LFS download, or AGNOS update must not produce a ready installation. Do not freeze the updater or run a separate preparation script during this normal workflow.

The first download of a build containing this change still runs the previously installed updater. Use the already-retained lightweight settings or the older-build fallback below for that transition; the new policy starts when the new updater runs.

## Before downloading

1. Read the device's installed branch/commit, OS version, `IsOffroad`, updater state, free space, and the requested target's remote commit. List target submodule pins and missing LFS objects. Finish with an estimate that distinguishes cached files from new downloads.
2. Inspect the active checkout using `GIT_OPTIONAL_LOCKS=0` and `git --no-optional-locks`. For object inspection, prevent lazy fetching with `GIT_NO_LAZY_FETCH=1`. Active `.git` mtimes changing after `.overlay_init` invalidates the staging overlay; an ordinary `git status` can refresh the index and cause this.
3. Work in `/data/safe_staging/merged`, never by checking out another commit in `/data/openpilot`. Verify that each submodule's Git directory and working tree resolve inside staging. If staging must be recreated, let the existing updater initialize it before preparing the lightweight downloads.
4. For manual staging preparation only, require offroad and an idle updater. The fallback freezes only the identified idle updater process, checks offroad throughout preparation, and resumes that same process in `finally`, including on failure. Resolve the current PID and target pins every time.

## Manual staging fallback for older builds

Use this only when the installed updater predates lightweight downloads. Prefer the normal workflow once the new updater is installed.

1. Fetch the target superproject commit shallowly without recursive submodule fetching. Verify its SHA against the chosen remote branch before downloading submodules.
2. Fetch each changed submodule's exact gitlink shallowly. Configure tinygrad's promisor remote (`remote.origin.promisor=true`, `remote.origin.partialclonefilter=blob:none`) and sparse patterns before its checkout. Verify the selected commit, a clean tinygrad working tree, and absence of `test/mockgpu/` after a hard reset.
3. Verify that the active checkout and its Git mtimes remain unchanged. Set `UpdaterTargetBranch` through the installed Params API, resume the updater, and request its normal fetch with SIGHUP. Keep the normal LFS, AGNOS, final-copy, and consistency-marker steps.
4. Finish only when `UpdaterState=idle`, `UpdateAvailable=1`, the finalized `.overlay_consistent` exists, the finalized branch/commit and all submodule pins match, and the excluded files remain absent. Verify downloaded model hashes against their LFS OIDs. Measure the network counter delta, noting that it includes other device traffic.
5. Respect the user's requested download/install scope. After installation, independently check the active commit, OS, model selection, panda state, and retained shallow/sparse settings when the device is reachable. User-reported installation success is distinct from SSH verification and road validation.

When inspecting AGNOS, use read-only partition reads. The AGNOS CLI's `--verify` path can swap slots; it is not a read-only status command.

## Repository implementation

The implementation is in `openpilot/system/updated/updated.py` on `merge/commaai-20261109`. It bounds the superproject fetch, disables implicit submodule recursion, and calls `update_submodules` before the normal LFS/AGNOS/finalization steps. tinygrad is initialized without a checkout, reuses a deinitialized Git cache when available, and receives partial-clone and sparse settings before fetching its exact gitlink. Its Git directory stays relative so final copying preserves the exclusion.

`openpilot/system/updated/tests/test_updated_lightweight.py` uses local Git remotes to cover fresh/full/partial/deinitialized caches, exact pins including nested modules, reset/final-copy preservation, repeated updates, snapshots without submodules, and fetch/LFS/AGNOS failure handling. All 43 updater and branch-migration tests passed in the Linux build container. A fresh GitHub fetch of the real tinygrad pin used 11.09 MiB of Git packs, kept 69 blobs absent, and passed a runtime import. Device installation of this implementation is a separate deployment step.

The historical helper in `E:/dev/merge-notes/codex-light-update-20261113.py` contains a fixed PID and fixed pins. It is an execution record, not a reusable command. Derive those values from the current device and target when preparing another update.

## Verified example: 2026-11-13

- Target: `merge/commaai-20261109` at `ba6cd1ee2d409e6266f463c6b6ab09e44761884f`. The development checkout `korea-dev` was left at `b617fd35b`; start subsequent implementation from the installed integration branch or explicitly integrate it while preserving local work.
- Finalization checks passed for all seven submodule pins and all three new model hashes. The superproject and six changed submodules had one reachable commit; the unchanged neural-network-data checkout was reused.
- tinygrad stayed at sunnypilot commit `30ddfc2eac4970e0d7be50ffb241d9dcce4c4e04`. On PC, depth-one full checkout required about 856 MiB of Git data; excluding `test/mockgpu/` with partial cloning required about 11 MiB and 44 MiB of working files. The exclusion survived device finalization.
- New LFS payload: 768.15 MB. AGNOS 20.3 reused all seven previously recorded partitions. Device wlan0 received 803.97 MB during preparation, including unrelated traffic. Existing old Git packs were retained.
- Installation preparation took about seven minutes. The user subsequently reported successful installation. The first post-install SSH attempt timed out, so active OS/model/panda state still needs independent verification.

Operational details are in `E:/dev/merge-notes/2026-11-10-commaai-merge-exec.md`. That external log is evidence; this file is the procedure future agents must follow.
