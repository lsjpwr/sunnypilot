# After-DEC integration validation — ticket #7

Status: **partial; #7 and parent #2 remain OPEN, 4/5 tickets complete**.
This report records desktop validation and the subsequent full-rlog replay on 2026-11-02. It does not authorize deployment.
No production source changes were needed during #7.

## Source and environment

| Role | Exact revision |
| --- | --- |
| Tested integrated source | `8befa34a174646297f2870b4c9d9cb701a3b9773` |
| Parent source tree | `63626afe3fd4f4aa59336323543c9583ff8b6590` |
| OpenDBC | `ccbd2554c9996c52dab80d8d20b23da68469b3fe` |
| Panda | `04b5b2cdcfeae82719f8dd38483f00a4066954d9` |
| Fixed official target | `6f7a2c94c10dffcf9a83b120ab1e6985e4496868` |
| Local comparison | `a721eec2aaa5b16433234fb4ec097c55b46d691d` |

The report commit changes documentation only; the executable source remains the tested revision above.
Branch: `integrate/sunnypilot-after-dec-sunset-0.11.3`.
The existing Ubuntu 24.04 `sp-build` container uses `/tmp/sunnypilot-after-dec` as source.
`/work/.venv` supplies Python 3.12 tools only. There is no source bind mount.
WSL/nsenter transport verifies the existing container identity before access; Docker and Herdr were not restarted.
Explicit PYTHONPATH resolves parent and dependency imports inside the isolated source tree.

The source manifest matched **2,164 ordinary tracked files/symlinks, zero mismatches** across parent, OpenDBC and Panda.
It separately lists **286 LFS pointers**, which are not proof of production model weights or inference.
The original `E:/dev/sunnypilot` was inspected read-only; its partial clone was not repaired.

## Acceptance ledger

| #7 condition | Result | Evidence or remaining gate |
| --- | --- | --- |
| Python/native build and process/config/schema checks | Partial | Relevant native builds, Python compilation, messaging/config/schema and real plannerd socket probe pass. Restricted PC manager/UI smoke passes; full intended-configuration startup remains unexecuted. |
| GPS/destination, Korean SLA/SCC, cruise/curve, Tesla/safety regression | Pass within desktop test scope | Exact-source #6 combined gate reused; no source changes invalidate it. |
| Representative curve entry/recovery, stronger lead braking, stop/go planner-output comparison | Pass at the declared external boundary | Full-rate recorded model/radar inputs drive each revision's real planner; recorded map/SLA outputs supply the explicitly excluded external-state boundary. Numerical results are below. This is not whole-system replay. |
| MADS/Ratekeeper and excluded policy preservation | Pass | Prior MADS/RELC/BSM/Blendful regressions, new Ratekeeper tests, and unchanged-source comparisons. |
| Distinguish outcomes; document device checks; no automatic deployment | Pass | This report and the checklist below. |

Conditions 2, 3, 4 and 5 are met. This is **4/5 acceptance conditions for #7**, not a fifth completed integration ticket. Condition 1, #7 and parent #2 remain open.

## Desktop results

| Gate | Run | Pass | Fail/error | Skip |
| --- | ---: | ---: | ---: | ---: |
| Reused #6 combined unittest gate | 2,230 | 2,014 | 0 | 216 |
| Additional disjoint #7 focused unittest gate | 357 | 357 | 0 | 0 |
| Combined unique unittest accounting | **2,587** | **2,371** | **0** | **216** |

The additional gate covers `openpilot.common.tests.test_realtime`, the three cereal messaging modules
`test_services`, `test_messaging`, `test_pub_sub_master`, and manager `test_duplicate_procs` / `test_blacklisted_procs`.
Module-prefix checks confirm these do not overlap the #6 module list. Earlier subset runs and repeated smoke/replay probes are not added.
The final combined gate checks the result JSON, source provenance, input hashes and required output assertions; it does not pretend that the reused tests were rerun.

The 216 skips retain their original reasons: 154 inapplicable firmware fuzzy-matching cases;
52 unsupported safety capabilities/shared helpers; and 10 parent cases (3 missing PC map-build dependencies,
2 device conditions, 3 removed stock compiler/warp comparisons, 1 unavailable real model artifact, 1 external-network case).
Exact test names/reasons remain in the local JSON. None were changed to make #7 pass.

Additional evidence, counted separately from unittests:

- **1,255 Python files compile**, zero syntax errors, using the exact source manifest; this is not an import or type-check claim.
- Reused successful generated-cereal, pandad, native host-transport and CAN-protocol builds/tests; Panda H7/body debug signed firmware, bootstubs and libpanda builds. The CAN test covers 16 hardware/length combinations, including chunked receive. No hardware transport or release firmware was exercised.
- Real `plannerd.main` ran in an isolated Params/IPC prefix with synthetic messages: **56 longitudinalPlan outputs**, valid plan/SP-plan/driverAssistance messages, positive acceleration followed by the requested braking, clean exit code 0. No process or planner implementation was mocked. PC scheduling behavior is not hardware timing proof.
- Prior selected type checking: 19 files with 3 diagnostics also present at the pinned official baseline, zero new diagnostics; 2 additional navigation files with zero diagnostics. This is not a clean whole-project type check.
- Ten MADS, Python/C++ Ratekeeper, desire/RELC/BSM-policy source files are byte-identical to the local comparison. Regression coverage retains knavlink/phone/Fleet budget, GPS/destination ownership, Korea SLA/SCC, 2s up/1s down smoothing, actual previous output, immediate stronger-brake arbitration, coast/reset, local Tesla behavior, and Blendful disabled even when persisted true. No bfayers policy was introduced.

Preserved diagnostic history: #6 initially had two firmware-query timing failures. Keeping Tesla F195 and F181 requires
0.2s rather than the official single-query 0.1s; the all-brand reference becomes 8.4s rather than 8.3s, with unchanged tolerance.
The final #6 JSON has zero failures/errors and SHA256 `d4ec04e35191ab2687cb3d2769125b88c70139f2ba79861b80e12f92a24203a6`.
The old failures remain separate. Two #7 smoke-harness setup mistakes (dataclass serialization/import path) were corrected before the green probe; production source was unchanged.
The straight-road synthetic input emits a vision-controller divide-by-zero warning when its target becomes infinity; the observed published acceleration remains finite and within bounds.

## Initial qlog-only inventory and limited replay (preserved earlier evidence)

Read-only inventory found 1,293 qlogs / 649,641,001 bytes under `E:/dev/comma_logs/realdata`,
plus 72 newer qlogs over 10 routes under `E:/dev/sunnypilot/.scratch/drive-2026-10-23/qlogs`.
Searches of these artifact roots and the relevant integration/Tesla scratch directories found no rlogs or HEVC inputs.
Samples from all nine existing runtime qlog cache roots have no modelV2; they carry approximately 2Hz plan/action,
4Hz radar and 10Hz car/control samples, with only about three CAN messages per minute.
This establishes the gap in the inspected inputs, not that full logs exist nowhere else.

The limited replay calls real **LongControl.update** with recorded `aTarget`, `shouldStop`, car state,
engagement/override state, recorded CarParams/CarParamsSP and fixed acceleration limits (-3.5, 2.0 m/s²).
Decimated samples are held at 100Hz; five seconds of warmup start from zero controller state.
Windows crossing excessive input gaps are excluded. This does not reconstruct the original 100Hz stream.
The baseline and candidate LongControl/PID source are byte-identical. The baseline module is loaded from its exact Git blob;
both use candidate dependencies/schema, so this is **component compatibility**, not execution of a complete baseline checkout.
The planner, model, map/SCC calculation, vehicle response and closed-loop behavior are not replayed.

| Scenario | Route; monotonic window (s) | Held ticks | Candidate accel min/max (m/s²) | Maximum baseline/candidate difference | Recorded-command MAE (m/s²) |
| --- | --- | ---: | --- | ---: | ---: |
| Vision curve entry and recovery | `0000020f--1356a001e1`; 384.571–407.069 | 2,249 | -1.2000 / 1.0226 | 0 | 0.04707 |
| Lead braking | `000001b8--0e37140717`; 4742.441–4757.441 | 1,500 | -0.9753 / 0 | 0 | 0.02893 |
| Stop and go | `0000020f--1356a001e1`; 306.941–341.849 | 3,490 | -2.0099 / 1.1846 | 0 | 0.04138 |

RMS baseline/candidate difference is also zero in all three windows, as expected for unchanged code.
The lead trigger has dRel 34.19m and aLeadK -1.541m/s²; longitudinal control disengages 4.68s after that trigger.
This is not proof that the changed planner selects stronger braking correctly.
Stop/go transitions are PID → stopping at event +0.13s → PID at +23.11s.
Recorded stop release reaches PID on the same held-input tick (0.00s); the qlog resolution cannot establish real response latency.
Maximum adjacent held-output steps are respectively 0.6057, 0.8177 and 2.1383m/s²; these are not comfort/jerk acceptance results.

All **35 segment files** for the two selected routes match the original host files by SHA256.
One older segment, `000001b5--8832d5eea8--49/qlog.zst`, fails Cap'n Proto parsing because it is truncated;
it was excluded entirely and is not part of the selected routes. The original remains untouched.
Full event counts, input hashes, output rows, transitions and metrics are retained locally.

## Reproduction and evidence

Local recovery directory: `E:/dev/sunnypilot-after-dec/.scratch/after-dec-validation/`.
It contains live issue snapshots, `final-gate.json`, the two qlog result JSONs, `replay-input-provenance.json`,
`process-smoke.json`, `compile.json`, policy/source manifests, logs, runner scripts and `evidence-hashes.json`.
Earlier #3–#6 evidence remains in the adjacent `after-dec-*` directories.
The scratch scripts/logs are local evidence, not committed portable tooling; use the existing verified transport and isolated runtime.

Commands from the integration workspace (the runtime must already contain the recorded inputs and baseline module):

```powershell
python .scratch/after-dec-validation/verify_sources.py
python .scratch/after-dec-validation/run.py focused-tests .scratch/after-dec-tesla/run_tests.py validation-focused openpilot.common.tests.test_realtime openpilot.cereal.messaging.tests.test_services openpilot.cereal.messaging.tests.test_messaging openpilot.cereal.messaging.tests.test_pub_sub_master openpilot.system.manager.test.test_manager.TestManager.test_duplicate_procs openpilot.system.manager.test.test_manager.TestManager.test_blacklisted_procs
python .scratch/after-dec-validation/run.py process-smoke .scratch/after-dec-validation/process_smoke.py
python .scratch/after-dec-validation/run.py qlog-replay .scratch/after-dec-validation/qlog_replay.py
python .scratch/after-dec-validation/run.py qlog-lead .scratch/after-dec-validation/qlog_replay.py lead
python .scratch/after-dec-validation/run.py compile .scratch/after-dec-validation/compile_sources.py
python .scratch/after-dec-validation/final_gate.py
```

Runtime JSON outputs must be copied back with the existing transport before aggregating; `run.py` captures stdout/stderr only.
Re-detect the container PID/identity before future reuse. Do not use `/work` as source or run concurrent overlays/builds.

## Full-rlog planner-output comparison

The full-log input gap is now resolved for the selected windows. Read-only SSH copied only completed historical files:
`0000020f--1356a001e1` segments **3, 4, 5**, and `00000233--0f8b7970bd` segment **8**.
The four rlogs total **45,383,707 bytes**. Fourteen small qlogs from route 233 were also copied to locate the lead event.
Device hashes and size/mtime were checked before and after reads; host and runtime SHA256 match.
The original lead segment 12 on route `000001b8` was no longer available, so route 233 supplies the replacement.
The currently growing route 234 was not copied. No vehicle files/settings/processes changed; the connection is no longer needed.

Each rlog has 1,200 real modelV2 messages and approximately 1,200 radar / 5,970 car/control messages per minute,
plus CarParams, CarParamsSP and historical initData settings. Route 0f records clean revision
`6029a17c49e6fb2b36c8b9959feeb129ec02768a`; route 233 records clean `a721eec2aaa5b16433234fb4ec097c55b46d691d`.
Recording revision is distinguished from the two replay revisions below.

### Independent sources and declared boundary

Baseline `a721eec2aaa5b16433234fb4ec097c55b46d691d` was exported into a separate runtime with all eight repository snapshots:
OpenDBC `3890ac7a54d652cfa8f08618e7d189a789d6d9dc`, Panda `f6887ad012f1924f75983b09b435cd8ac57eb03d`,
tinygrad `66ee3cfb4f3a3908a6a20ddfbec7774ba7c09b4e`, and its other exact gitlinks.
**4,182 baseline files/symlinks and 2,164 candidate files/symlinks match**, zero mismatches.
Baseline Params, msgq/VisionIPC and MPC native targets were built in that tree. Candidate MPC native products were rebuilt
from its source, with earlier products preserved. Each run uses a separate Python process, its own schema/native libraries
and isolated PYTHONPATH; import paths and native artifact hashes are recorded. No baseline code silently imports candidate code.

Both use the same PC toolchain: Python 3.12.13, NumPy 2.5.2, Cython 3.2.9, pycapnp 2.1.0,
comma-deps-acados 0.2.2.post98, comma-deps-capnproto 1.0.1.post98 and SCons 4.10.1.
This is a controlled common toolchain comparison, not a fresh installation of each revision's complete lockfile or device build.
The baseline archive uses explicit build metadata; Git source identity comes from object manifests, not synthetic runtime Git history.

The harness calls the actual `LongitudinalPlanner.update`, retaining real vision SCC, baseline DEC, MPC, coast and final acceleration arbitration.
Recorded messages retain timestamp order; a new model is consumed on the next radar update, as in plannerd.
Monotonic time follows recorded time. Map/SLA output pairs are aligned to the same recorded `longitudinalPlan.modelMonoTime`,
not the preceding frame. Recorded final aTarget is comparison data, never injected as the planner's result.

All allowlisted consumed persistent settings are present in initData and copied into isolated Params.
The candidate correctly omits the two removed DEC settings. Historical evolving `MapTargetVelocities`, `LastGPSPosition`,
`KoreaSectionSpeedLimit` and `KoreaSectionStart` are not in these logs. Therefore **map/SLA outputs are replayed from the recording;
their internal calculations are not re-executed**. Current car settings were not substituted for historical state.
Route 233 publishes no liveMapDataSP in the selected rlog or its 14 selection qlogs; the optional topic retains SubMaster's invalid
initial value, explicitly recorded as absent. No model, radar, GPS or car measurement is synthesized.

Both revisions execute **3,588 updates on route 0f and 1,199 on route 233**, with identical model/timestamp pairs and zero unmatched
model-boundary pairs. The 12 and 1 initial file frames, respectively, precede complete input readiness and are outside the measured windows.
All core input-validity flags are true for executed frames. The planner initializes at the first complete frame;
unrecorded pre-segment internal state is not claimed restored. The measured curve/stop windows follow about 140/62 seconds of replay,
and the lead window follows 9.5 seconds, with the stronger-lead event at 14.8 seconds.

### Numerical verdicts

Acceleration differences are candidate minus baseline, in m/s². These frame counts are replay measurements, not additional unittests.

| Scenario / monotonic seconds | Compared frames | Maximum absolute difference | RMS difference | Source / stop differences |
| --- | ---: | ---: | ---: | --- |
| Curve entry/recovery, route 0f, 384.571–407.069 | 449 | 8.88e-15 | 8.45e-16 | 0 / 0 |
| Stop/go, route 0f, 306.941–341.850 | 699 | 1.3274e-5 | 1.0888e-6 | 0 / 0 |
| Stronger lead braking, route 233, 607.000–620.000 | 260 | 0.0384727 | 0.0023861 | 1 / 0 |

- **Curve: preserved.** Both produce -1.2000 to +1.02545, with the same vision entry/recovery transitions and maximum adjacent
  acceleration step 0.06448. Recorded map output is active for 300/449 frames, so this does not validate reconstructed map behavior.
  Both differ from the older recorded vehicle plan by MAE 0.29632; the recording uses 6029a17, not the comparison baseline.
  This is a baseline/candidate comparison, not a claim of exact reproduction of that older drive.
- **Stop/go: preserved.** Stop asserts at 311.599342, releases at 334.831204, briefly asserts at 335.131458 and releases at
  335.184103 in both revisions. Source and stop decisions agree on all 699 frames; the small numerical acceleration difference
  does not change these transitions. Candidate recorded-plan MAE is 2.5761e-5.
- **Lead braking: no delayed lead response in this window.** Both first select a lead result at least 0.1 stronger than cruise at
  **612.276805**, with four such frames and the same stop assertion at 617.075833. Minimum acceleration is approximately -1.38444.
  The sole source difference is at 607.227855: model gas probability falls to 0.35320, below 0.4; both disable throttle, but the
  candidate's integrated coast limit selects cruise -0.227265 instead of baseline E2E -0.188793. This is **0.038473 stronger braking**,
  consistent with the explicit coast cap now applying to permanent E2E. It is not delayed lead braking. The next frame returns to
  E2E in both. Maximum steps are 0.06093 candidate versus 0.04398 baseline; this is not a device comfort certification.

The three comparisons satisfy the ticket's representative planner-output comparison at this declared external boundary.
They do not establish map/SLA internals, closed-loop vehicle response, model reinference, GPU timing, full manager/UI startup,
or arbitrary-route equivalence. Independent review must retain these limits when interpreting condition 3.

After the candidate MPC rebuild, **13 existing cruise/smoothing tests pass, zero failures/skips**. They are a subset of the prior
2,587 unique tests and are not added to that total. Initial harness enum/input-readiness errors and an initial shared build-cache
failure are preserved separately; the final native builds and all four replay runs pass. No production source fix was required.

Detailed local proof is additive under `.scratch/after-dec-validation/full-log/`: remote transfer manifests, original rlogs,
input counts, exact source manifests, native build logs, runtime/import hashes, raw baseline/candidate rows,
`scenario-comparison.json`, `mpc-regression.json`, and review transcripts. Earlier qlog evidence remains intact.
Representative commands (run sequentially using the already prepared runtimes):

```powershell
python .scratch/after-dec-validation/run.py final-candidate-0f .scratch/after-dec-validation/full-log/replay_planner.py candidate 0000020f--1356a001e1
python .scratch/after-dec-validation/full-log/baseline_command.py final-baseline-0f /work/.venv/bin/python /tmp/validation-replay_planner.py baseline 0000020f--1356a001e1
python .scratch/after-dec-validation/run.py final-candidate-233 .scratch/after-dec-validation/full-log/replay_planner.py candidate 00000233--0f8b7970bd
python .scratch/after-dec-validation/full-log/baseline_command.py final-baseline-233 /work/.venv/bin/python /tmp/validation-replay_planner.py baseline 00000233--0f8b7970bd
python .scratch/after-dec-validation/full-log/compare_replays.py
```

Copy each runtime result JSON back before comparison. Do not reconnect to the device or recopy logs for these commands.

## Remaining manager/UI gate and device checklist

**Remaining gate:** full manager/UI startup with the intended model/device configuration. Condition 1 remains partial;
conditions 2-5 stay met. The following readiness work does not authorize device writes or deployment.
The user requested **local preparation only** for this pass. No further device connection is needed now.

### Additional isolated PC evidence

The existing candidate runtime built `openpilot/system/loggerd/bootlog` successfully. Then the real `manager.main`
initialized fresh PC Params and supervised the real big-screen UI and `logmessaged` for 25 seconds:
**20 managerState messages, 1,198 uiDebug frames, manager exit 0, both owned children reaped**.
A real bootlog was produced. Only these two services were allowed; all 56 other configured services were explicitly blocked,
including Panda, model/camera/control processes, model downloads, updater, deletion and external upload/backup services.
This probe ran from the runtime repository root. The normal manager-directory launch additionally registers
`sunnylink_uploader` and `copyparty-sfx` through relative-path checks; those were absent from this PC graph.
Do not reuse its literal 56-name block list for a device launch.
`NOBOARD=1`, a unique `OPENPILOT_PREFIX`, dedicated `PARAMS_ROOT`/`LOG_ROOT`, a private Xvfb display and a fresh network namespace
with only loopback isolated this run. No hardware was connected, no device settings were copied, and no `deviceState.started`
or ignition message was fabricated. BIG=1 selects the large PC layout; it is not proof of the device's display configuration.

This proves **restricted offroad PC initialization/rendering**, not full manager, production model, camera/GPU, device timing,
network/registration, device Params migration or visual correctness of every settings screen. Wi-Fi's missing system D-Bus
was caught and logged; registration used `UnregisteredDevice`; font glyph warnings and synthetic-runtime Git upstream metadata
warnings remain in the log. These limitations were not patched away. Two initial harness errors (checking network isolation
through a shared sysfs mount, then serializing byte-valued key names) are preserved before the final successful probe.
The final check uses the actual network-interface list. No product source changed or broad test/replay gate was rerun.
The prior **2,371 pass / 216 skip / 0 fail/error** accounting is unchanged; this probe is not an added unittest count.

The runtime's four additional dependency archives match **1,771 files/symlinks, zero mismatches**, against exact Git archive
commit identifiers: tinygrad `92b742f711c7d216fd3ed7a46a30a959e3c43bd4`, msgq
`e7396e76dadbb49e374d4b664ff6bbb43a39bcb0`, rednose `28d4a7f69e80e1c3e0d24ca0733d7daeaeade3d0`,
and teleoprtc `1aa8fc433bef1519a95c0700c96258c3be6dfb34`. These are archive directories, not independent runtime Git checkouts;
`git -C` there resolves to synthetic ancestor metadata and is **not** dependency provenance. The earlier 2,164-file manifest
covers parent/OpenDBC/Panda; the new check supplements it without changing the earlier test claim.

### Model and startup preflight findings

| Item | Verified locally | Live device fact / consequence |
| --- | --- | --- |
| AGNOS | Pinned baseline `launch_env.sh` expects 19.6; candidate expects 19.9. | Current `/VERSION` is **UNKNOWN**. On AGNOS, normal candidate launcher enters its update/reboot path if actual version differs. Do not override `AGNOS_VERSION` to hide a mismatch. |
| Panda | `pandad.py` checks firmware signature, flashes on bootstub/signature mismatch, may recover bootloader/DFU, and resets internal Panda before connection. | Installed MCU signature/version, bootstub/DFU state and transport health are **UNKNOWN**. Different source commits alone do not prove a firmware mismatch. No-flash authority is insufficient to run this wrapper unchecked. |
| Model selector | Baseline requires JSON version 18; candidate requires 21. Recorded route 233/a721 selected tinygrad MACROSTI ref `d70b13931793bc0e4d8efd36128dd66f227a3f81`, version 18. | Current bundle/slot, hardware and artifact hashes are **UNKNOWN**. Historical selection is not a live preflight. When models_manager runs, candidate validation removes an incompatible selection and the affected active slot falls back to the stock runner; this must be observed and recorded, not called the same selected model. |
| Checkout and recovery | Parent/dependency pins above are pushed; original PC development worktree was untouched during this pass. | Current device HEAD, branch, dirty files, submodules/worktrees, launch supervisor, staged updates, prebuilt marker and QuickBootToggle are **UNKNOWN**. Earlier a721 inventory is historical. |

One bounded read-only SSH preflight to the previously known peer timed out before executing any remote command.
The user cannot connect now and requested local completion; no retries or device changes followed.

Two retained ONNX payloads in the existing PC runtime do **not** match the candidate LFS OIDs. They were already excluded from
source-manifest/inference claims and were not used by the recorded-model planner replay or this UI probe. The exact candidate
stock inputs were downloaded separately into local scratch, with byte count and SHA256 verified:

| Input | Bytes | Candidate LFS SHA256 |
| --- | ---: | --- |
| driving_supercombo.onnx | 60,918,562 | `65a08adc31d5c456219687d99b7bf5e44d61dae2d49ea67850e76105c7248cce` |
| dmonitoring_model.onnx | 7,844,499 | `dee5a294e8afaacc9295ac5d100e00733ecac278e79264b96331e40a3ede1b04` |

These files are **uninstalled and unexecuted**. No inference was attempted. Stock compiled driving/DM pickles and the big-model
pickle are absent from the current PC runtime. For a later production run, verify the chosen bundle's compatible version,
runner, every artifact hash, QCOM/Chestnut slot and hardware-specific compiled model/warp inputs. Do not substitute a CPU model,
empty bundle, different custom model, or stock fallback and label it the user's intended configuration. A selected v21 bundle
is a later explicit configuration decision; the historical MACROSTI v18 payload is not automatically compatible.

### Reviewable stationary procedure (not executed or authorized)

This is a two-stage proposal: a no-flash offroad smoke can provide limited device evidence; the full intended-configuration
startup must separately meet its OS/model/Panda prerequisites. Neither stage requests new driving. Do not use
`openpilot/selfdrive/test/test_onroad.py` on the user's device: its normal setup changes Params and recursively deletes the log
root. Do not run the normal launcher, updater or Panda wrapper as a read-only preflight.

1. **Read-only preflight and freeze the recovery identity.** While parked, first read `/VERSION`, the resolved active checkout,
   exact HEAD/branch/status, all submodule roots/SHAs/remotes, worktree list and launcher/supervisor state. Use
   `GIT_OPTIONAL_LOCKS=0`, `GIT_NO_LAZY_FETCH=1`, `git -c gc.auto=0`; inspect each dependency's own repository root before treating
   its HEAD as a dependency. Read only allowlisted model slots/runner, QuickBootToggle, DeviceBootMode and offroad state;
   inventory/hash the referenced model artifacts and both baseline/candidate firmware files. Verify that the isolated Params
   copy will contain the existing DongleId; otherwise registration may read `/persist/comma/dongle_id` or attempt an external
   registration POST. Do not start from an empty device identity or publish private keys/identifiers. Inspect `.overlay_init`,
   `/data/safe_staging/finalized/.overlay_consistent`, `/data/safe_staging/old_openpilot` and `prebuilt` without changing them.
   Verify the actual AGNOS power-watchdog timeout and safe supervisor-stop duration: without hardwared/deviceState the
   restricted smoke does not refresh `/var/tmp/power_watchdog` or run fan management. Do not assume 60 seconds is safe or
   disable the watchdog; if the permitted interval is insufficient, omit this optional smoke.
   Obtain the **full installed MCU signature** through an established non-mutating observation or existing complete evidence;
   the wrapper's truncated log prefix or a file on disk is not proof of the installed signature. Do not instantiate Panda merely
   to query it without auditing constructor/reconnect side effects. Stop if identity, firmware state or recovery ownership is unknown.
2. **After separate device-write authorization, stage and back up without altering korea-dev.** Record the exact approved
   candidate documentation HEAD (whose production source is 8befa34), all gitlinks and LFS hashes. Use a separate candidate
   directory, e.g. `/data/sp-afterdec-validation`, preserving the baseline checkout, branch, uncommitted files and submodule
   repositories. Keep a verified private backup of Params with absence/type information, calibration, selected-model metadata,
   needed model files, baseline firmware/bootstub and launcher/staging state; do not publish secrets or delete driving logs.
   Hash/compare the backup before proceeding. Use candidate-only package paths and build products; do not relink `/data/pythonpath`
   or create a candidate `prebuilt` marker to bypass build validation. On device, a prefix alone does not isolate every path:
   supply dedicated `PARAMS_ROOT` and `LOG_ROOT`, audit shared model/map/staging paths, and keep update/download/delete/upload/
   backup services blocked for the restricted smoke. `LOG_ROOT` does not redirect `/data/log`: logmessaged uses this shared
   directory and its rotating handler can delete old files beyond retention. The minimal device smoke therefore blocks
   logmessaged too and captures managerState/stdout/stderr in the dedicated evidence directory. Cloudlog IPC can drop messages
   without logmessaged, so this smoke cannot prove complete diagnostic-log coverage. Keep bootlogs/evidence private and outside
   `/data/media/0/realdata`, e.g. `/data/sp-afterdec-validation-evidence`, to avoid baseline upload/deletion scans.
   Invoke `manager.main` directly,
   avoiding the command-line exception handler that attaches a shared file logger. Preserve shared logs before any later full run. In the isolated Params copy, set QuickBootToggle false to prevent
   `manager_init` writing hardcoded `/data/openpilot/prebuilt`; preserve the user's original value in backup.
3. **Run only the approved stationary stage.** Stop the baseline supervisor/processes by its verified normal procedure and
   confirm they have stopped before starting the candidate; retain a recovery shell and avoid competing hardware owners.
   For an optional no-flash smoke, directly invoke candidate `manager.main` with unique prefix, isolated Params/logs,
   `NOBOARD=1`, and `BLOCK` containing every configured process except `ui`. Generate this list from `managed_processes`
   using the **same candidate manager-directory cwd, isolated environment and imports as the actual launch**; save and inspect
   the resolved list before starting. Explicitly check that `sunnylink_uploader`, `copyparty-sfx`, `logmessaged` and every other
   non-UI service are blocked when present. The manager and observer must use that same configuration. This differs from the
   PC two-child smoke because device logging is shared. It bypasses the launcher and excludes model/Panda/camera/control
   services; record the actual OS and call it restricted offroad smoke.
   It does **not** close condition 1. For full intended-configuration startup, first resolve any actual AGNOS mismatch,
   choose/verify the intended v21 model and fallback, and compare installed versus candidate **production** firmware signatures
   and protocol versions. Prior H7/body debug builds are not deployment firmware. Any needed OS/Panda/bootloader update,
   or the wrapper's reset/recovery behavior, requires explicit authorization including rollback. Stop rather than entering
   update paths under no-flash authority. Do not fake ignition, skip model compilation or force a hardware backend to obtain green output.
4. **Observe explicit pass/stop criteria.** For restricted smoke, require 60 seconds of live UI and regular managerState,
   only UI running as a managed child, no crash/restart loop, and clean owned-process shutdown, within the verified watchdog/
   thermal-safe interval. If that interval cannot support 60 seconds, do not run this proposal or claim its smoke criterion met. Record UI/model/settings
   observations without claiming inference. For the full gate, require exact intended OS/model/firmware identity; expected
   process health and valid real inputs/outputs; correct selected versus active/fallback display; preserved Korean/Tesla
   settings; Blendful still disabled; and no unexplained startup errors. Capture process/model/fallback and timing evidence
   for the exercised state. Parked/offroad does not exercise onroad-only processes; mark them unexecuted unless actually run
   under separately approved stationary hardware conditions. Stop for unexpected flashing/updating, wrong model/backend,
   altered baseline files/settings, crash loops, non-finite outputs, unsafe vehicle state or loss of the recovery channel.
   Leave unexercised device and driving claims open; do not invent a mandatory road test to close this desktop integration ticket.
5. **Restore and verify before leaving the device.** Stop only the candidate's owned processes and verify they exited. For
   the isolated no-flash smoke, original Params/checkout/OS/Panda should be unchanged: compare them to the frozen identity,
   preserve evidence and resume the baseline through its verified supervisor. Do not blindly reboot: pending overlay installation
   and either launcher's AGNOS check can change the system. If a later authorized full run modified shared settings/models,
   restore the exact backed-up baseline state while processes are stopped, including missing keys, after reviewing differences.
   If OS or firmware changed, a Git checkout alone is not rollback: use the separately approved OS/firmware recovery plan;
   baseline pandad may reflash on signature mismatch. Confirm baseline model selection, process/UI health, logs and configuration
   before declaring recovery complete. Keep candidate artifacts/evidence for review; cleanup is not required for this gate.

Source anchors for the procedure: `launch_env.sh`, `launch_chffrplus.sh`,
`openpilot/system/manager/{manager,process_config}.py`, `openpilot/selfdrive/pandad/pandad.py`,
`panda/python/__init__.py`, `openpilot/sunnypilot/models/helpers.py`,
`openpilot/sunnypilot/system/params_migration.py`, and `openpilot/common/hardware/hw.py`.
Model migration seeds the Chestnut slot from the legacy USBGPU/single slot when needed; validation can then clear incompatible
v18 selections. Preserve both old and new slot metadata. The baseline v18 selector likewise cannot be assumed to accept a v21
selection on rollback. Never reuse mutated candidate Params as the baseline backup.

### Evidence and remaining action

Additional local proof is under `.scratch/after-dec-validation/startup/`: `pc_probe.py`, `pc-probe.json`, `manager.log`,
`bootlog-build.log`, `runtime-inventory.json`, exact dependency manifests/results, `model-source-preflight.json`,
`model-input-downloads.json`, the two verified model inputs, failed read-only SSH preflight, independent reviews and recovery state.
The initial Claude review's unconditional firmware/OS update claims were corrected to the conditional behavior above.
Historical full-log and unittest evidence remains unchanged. No inference, full production startup, flash, deployment,
korea-dev merge, PR or driving was performed in this pass.

Later user action: **request a parked-device read-only preflight** when the connection is available. That preflight establishes
the concrete OS/model/firmware/recovery decisions before any device-write authorization. There is no local work pending now;
#7 and parent #2 remain OPEN at 4/5 integration tickets complete.
