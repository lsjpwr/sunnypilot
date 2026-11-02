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
| Python/native build and process/config/schema checks | Partial | Relevant native builds, Python compilation, messaging/config/schema and real plannerd socket probe pass. Full manager/UI startup is unexecuted. |
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

**Remaining gate:** full manager/UI startup with the intended model/device configuration. Review and separately authorize a
stationary validation session before any device action. Recorded modelV2 allowed the planner comparison above without video;
modeld reinference would require synchronized camera video and actual model artifacts. No new drive or pull is requested.

The following checks are **unexecuted** and require a separately authorized device session; this ticket performed none of them:

- [ ] Record device/vehicle/HW4 or HW4.5/legacy-EPS configuration, exact parent/dependency/firmware/model revisions and a recoverable configuration backup. Confirm debug-versus-release firmware expectations before any future flash decision.
- [ ] Validate full manager/UI startup and process health with production model artifacts. Check selected model, compatible fallback, displayed active model, persisted Blendful=true remaining disabled, GPU/camera failures and device Ratekeeper timing.
- [ ] Check real Panda SPI/USB health/version/CAN compatibility, ignition versus wake, and MADS state. Confirm stock safety behavior, override/re-entry, Summon/Autopark pause and DAS radar behavior in a controlled, separately approved procedure.
- [ ] Verify phone/knavlink GPS freshness, destination ownership/clear behavior, Fleet budget and offline/network failure behavior; check Korean camera/section/bump SLA and SCC settings/display against recorded expectations.
- [ ] After the full replay gate and explicit driving authorization, assess curve entry/recovery, stronger lead braking, stop/go, 2s/1s smoothing, coast/reset and unchanged RELC/BSM policy. Record observed outputs and abort criteria; desktop success is not clearance for public-road testing.

No korea-dev merge, PR, deployment, flash or live driving was performed. Leave #7 and #2 open until their remaining required gates are actually met.
