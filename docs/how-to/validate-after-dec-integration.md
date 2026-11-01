# After-DEC integration validation — ticket #7

Status: **partial; #7 and parent #2 remain OPEN, 4/5 tickets complete**.
This report records desktop validation on 2026-11-02. It does not authorize deployment.
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
| Representative curve entry/recovery, stronger lead braking, stop/go planner replay | **Unexecuted** | Available qlogs lack modelV2 and full-rate CAN. The component replay below does not satisfy this condition. Full-rlog location/reference was requested and remains pending. |
| MADS/Ratekeeper and excluded policy preservation | Pass | Prior MADS/RELC/BSM/Blendful regressions, new Ratekeeper tests, and unchanged-source comparisons. |
| Distinguish outcomes; document device checks; no automatic deployment | Pass | This report and the checklist below. |

Only conditions 2, 4 and 5 may be checked. This is **3/5 acceptance conditions for #7**, not a fifth completed integration ticket.

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

## Available recordings and limited replay

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

## Remaining replay gate and device checklist

**Next input:** a local path or downloadable route reference containing full rlogs with modelV2, radar/car/control messages,
CarParams/CarParamsSP, and matching settings for curve entry/recovery, stronger lead braking and stop/go.
Include scenario segment/time windows and relevant map/route state where applicable.
Recorded modelV2 permits planner replay without video; modeld reinference additionally requires synchronized camera video and actual model artifacts.
Run baseline/candidate planner replay on pinned sources, record commands, routes, output acceleration/source/stop transitions,
timing and comparison metrics, and review the differences before checking acceptance condition 3.

The following checks are **unexecuted** and require a separately authorized device session; this ticket performed none of them:

- [ ] Record device/vehicle/HW4 or HW4.5/legacy-EPS configuration, exact parent/dependency/firmware/model revisions and a recoverable configuration backup. Confirm debug-versus-release firmware expectations before any future flash decision.
- [ ] Validate full manager/UI startup and process health with production model artifacts. Check selected model, compatible fallback, displayed active model, persisted Blendful=true remaining disabled, GPU/camera failures and device Ratekeeper timing.
- [ ] Check real Panda SPI/USB health/version/CAN compatibility, ignition versus wake, and MADS state. Confirm stock safety behavior, override/re-entry, Summon/Autopark pause and DAS radar behavior in a controlled, separately approved procedure.
- [ ] Verify phone/knavlink GPS freshness, destination ownership/clear behavior, Fleet budget and offline/network failure behavior; check Korean camera/section/bump SLA and SCC settings/display against recorded expectations.
- [ ] After the full replay gate and explicit driving authorization, assess curve entry/recovery, stronger lead braking, stop/go, 2s/1s smoothing, coast/reset and unchanged RELC/BSM policy. Record observed outputs and abort criteria; desktop success is not clearance for public-road testing.

No korea-dev merge, PR, deployment, flash or live driving was performed. Leave #7 and #2 open until their remaining required gates are actually met.
