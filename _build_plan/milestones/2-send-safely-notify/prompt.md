# Milestone 2 — Send safely & notify

You are entering plan mode to plan and then build milestone 2 of this project.

## Context

- Read `@whatsapp-waha-integration/_build_plan/prd.html` for the full project scope, data model, and what/what-not for this milestone.
- Read `@docs/plans/whatsapp-waha-integration.md` for the technical design: the send queue and anti-ban pipeline ("The part that quietly breaks" §2, WI-5), notify/services/groups (WI-6), the rest of session lifecycle (WI-7 — auto-restart toggle, reauth, `PASSKEY_*` handling), diagnostics (WI-8), and docs/packaging (WI-9, deferred). This milestone covers build-sequence steps 5, 6, 8 and 9.
- Read `whatsapp-waha-integration/_build_plan/milestones/1-connect-link-receive/milestone-log.md` to see what milestone 1 actually built, including the answers to the webhook spike's four questions — the send queue and its shutdown handling depend on how registration and the coordinator ended up wired.

## Your task

1. Plan the implementation for **only** milestone 2 as defined in the PRD: sending (all message types), the two-lane send queue with human-like pacing and per-chat caps, the `notify.whatsapp_waha` service, group resolution, diagnostics, reconnect/repair handling, and reauth. Do not plan or re-plan anything milestone 1 already built.
2. Ship the send queue with pacing **disabled** (the plan's `off` preset) until a secondary number has been linked and a handful of real sends have been verified manually — see the plan's build sequence step 8 and Verification items 5, 11 and 12. Confirm with the user before flipping the default pacing on.
3. After the user confirms the plan, build only what is in milestone 2's scope.
4. Verify your work against the "Done when" criteria for milestone 2 in the PRD, and against the plan's Verification section items 5-9 and 12.
5. When complete, write a `milestone-log.md` in this folder (`whatsapp-waha-integration/_build_plan/milestones/2-send-safely-notify/milestone-log.md`). Structure it as follows:
   - **Start with a `## What's new in the app` section at the very top.** A concise, human-readable, bulleted list of the main user-facing capabilities added in this milestone, written so a non-technical reviewer can see at a glance what to expect.
   - Then include:
     - What was built
     - Any decisions made during implementation that weren't pre-specified in the PRD or the technical plan
     - Any deviations from the PRD or the technical plan, and why
     - Whether pacing was turned on by the end of this milestone, and what was verified against the real WAHA server before that happened

Ask me any clarifying questions using AskUserQuestion tool to lock in the implementation plan for this milestone.
