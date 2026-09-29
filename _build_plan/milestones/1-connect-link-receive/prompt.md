# Milestone 1 — Connect, link & receive

You are entering plan mode to plan and then build milestone 1 of this project.

## Context

- Read `@whatsapp-waha-integration/_build_plan/prd.html` for the full project scope, data model, and what/what-not for this milestone.
- Read `@docs/plans/whatsapp-waha-integration.md` for the technical design: the webhook self-registration algorithm ("The one architectural problem to solve first" and WI-2), the scaffold review (S1-S18) and its fixes (WI-1, WI-3, WI-4, WI-7), the risky-mechanism analysis, the build sequence, and the manual-setup list. This milestone covers build-sequence steps 1-4 and 7's linking pieces (not the send queue or auto-restart toggle — those are milestone 2).
- The existing scaffold at `waha-ha-integration-package/whatsapp_waha/custom_components/whatsapp_waha/` is broken (cannot load on current Home Assistant) and is reference only, not a starting point to edit in place. Part of this milestone is establishing the new `whatsapp-waha-integration/custom_components/whatsapp_waha/` layout described in the plan's "Repo layout" section.
- There is no prior milestone to read.

## Before you build anything

Follow the plan's build sequence steps 1 and 2 first, in order, before writing the rest of the integration:

1. Build the API client (`WI-1`) against a `FakeWaha` test double, then verify it against the real WAHA server on the user's LXC (already running, not yet linked to a number) — 401 vs 403, `?all=true`, the real NOWEB groups shape, `/auth/qr` on a `SCAN_QR_CODE` session.
2. **Spike the webhook round trip on a throwaway, unpaired session** before writing any registration code that assumes an answer. Answer the four questions in the plan's build sequence step 2 (does GET redact secrets; does a PUT of the GET body round-trip; does create-with-webhook work; do signed deliveries verify over raw bytes with `X-Webhook-Hmac`). Record what you found in the plan doc or in this milestone's log — the registration design in the plan assumes GET does not redact secrets, and falls back to "create-with-webhook only, never PUT" if that assumption is wrong.

## Your task

1. Plan the implementation for **only** milestone 1 as defined in the PRD. Do not plan or build the send queue, anti-ban pacing, the notify service, extra send types, or diagnostics — those are milestone 2.
2. After the user confirms the plan, build only what is in milestone 1's scope.
3. Verify your work against the "Done when" criteria for milestone 1 in the PRD, and against the plan's Verification section items 1-4 and 10-11 (the ones that apply to what this milestone builds).
4. When complete, write a `milestone-log.md` in this folder (`whatsapp-waha-integration/_build_plan/milestones/1-connect-link-receive/milestone-log.md`). Structure it as follows:
   - **Start with a `## What's new in the app` section at the very top.** A concise, human-readable, bulleted list of the main user-facing capabilities added in this milestone, written so a non-technical reviewer can see at a glance what to expect. Frame each bullet as something the user can now see or do.
   - Then include:
     - What was built (files created, the answers to the webhook spike's four questions, any deviation the spike forced from the plan's assumed design)
     - Any decisions made during implementation that weren't pre-specified in the PRD or the technical plan
     - Anything milestone 2 will need to know
     - Any deviations from the PRD or the technical plan, and why

Ask me any clarifying questions using AskUserQuestion tool to lock in the implementation plan for this milestone.
