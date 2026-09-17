# TOOLTIME — three-minute judging walkthrough

Use the running application at `http://127.0.0.1:8765`. Keep the public input folder ready for the upload step. Run the programme scenarios once before recording so you understand their measured results, and report the values shown on screen rather than memorized figures. Begin with the default night, shared setup enabled and no delay.

| Time | Action | Narration |
| --- | --- | --- |
| 0:00–0:20 | Show the night overview and railway timeline. | “TOOLTIME schedules the engineering hour, not just the calendar slot. A possession includes travel, isolation, productive work and restoration. The decisive question is: when is the last safe moment to commit?” |
| 0:20–0:45 | Select the cable job; point to p50, p90, latest commitment and rollback. | “Every job has two clocks. The model estimates work duration; the planner protects restoration time. Latest commitment includes setup, p90 work and rollback. The later rollback trigger tells the controller when restoration must begin. These are synthetic demonstration records, so this proves the workflow, not real-world calibration.” |
| 0:45–1:10 | Turn shared setup off, inspect the change, then enable it again. Open negotiation or explainability. | “Sharing setup can recover productive minutes. Requester roles offer concessions and Planner evaluates them, but the rules decide which combinations fit. Shared isolation never waives worksite compatibility. These agents run locally with deterministic rules today; the Gemini integration is a future extension.” |
| 1:10–1:30 | Add a start delay and re-plan; show a moved or deferred job and the explanation. | “Now the night starts late. TOOLTIME rechecks the available margin and explains what changed. Restoration is still protected. The readiness gate also holds the point-machine job until its part and competent supervisor are available.” |
| 1:30–1:50 | Open intake, paste the example request below and extract fields. Show any missing field and clarification. | “A team can paste the request it would otherwise email. Reader produces a reviewable draft and asks for missing information. Extraction is not readiness approval and does not silently add unverified work to the confirmed plan.” |
| 1:50–2:25 | Open programme planning. Show the public dataset, run a scenario, compare A/B/C, then show upload and export. | “The challenge engine works on the full supplied workload: 54 activities and 192 access units. A fixes supply; B fixes deadlines; C balances both. Judges can upload the same eight CSV schemas. The OR-Tools model checks workload, legal sharing, closures, buffers, workfronts and policy constraints, then exports the three required CSVs. These are local checks; the official validator is not in the pack.” |
| 2:25–2:50 | Return to the night plan, review checks, enter an approval reason, approve and download briefs. | “Authority stays with the planning head. Approval requires a reason and is tied to this exact revision. The team briefs include the slot and reasoning. This demonstration downloads briefs; it does not send messages to teams.” |
| 2:50–3:00 | Show the audit trail; optionally change the delay to demonstrate approval invalidation. | “A revised plan needs a revised decision. TOOLTIME records that trail so the controller can see what changed, why, and which plan was approved.” |

## Intake example

Paste this into the intake panel, adapting labels if required by the live form:

```text
Please schedule signal cable replacement between Bishan and Braddell on Tuesday night. Estimated work duration is 120 minutes with a crew of 4. Traction isolation is required. We need a signalling-certified supervisor and a cable trolley. The replacement cable must be available at Bishan depot. Priority high.
```

The parser is intentionally rule based. Show the actual extracted fields and clarification returned; do not claim email ingestion, external inventory verification or autonomous Gemini negotiation.

## Recording and submission checks

- Use values from the live result. Do not present the illustrative “41% to 68%” story as a measured PS1 outcome.
- Keep the distinction visible between the synthetic hourly demonstration and the uploaded challenge programme.
- Export A, B and C separately after each complete feasible solve. If a scenario does not produce a feasible result, disclose it rather than presenting another scenario's file as its answer.
- Publish the video, hosted app and GitLab repository through the hackathon submission process. This document is a recording script; those external deliverables have not been created by writing it.
