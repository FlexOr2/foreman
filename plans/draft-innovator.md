# Plan: Foreman Innovator — Autonomous Improvement Discovery

> **Depends on: analyze-command**

## Summary

Add a `foreman innovate` command that autonomously analyzes the codebase, generates improvement ideas, puts each through an adversarial multi-round review pipeline, and presents survivors as `draft-*` plans for human approval.

```
foreman innovate
  → Deep codebase analysis with provocative questions
  → Generates candidate ideas
  → Each idea → 3 adversarial review rounds (different personas)
  → Survivors written as draft-* plans
  → Human approves or rejects
  → Approved plans flow into Foreman orchestrator
```

## The Innovation Loop

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  1. EXPLORE                                                 │
│     Read codebase structure, dependencies, recent git log   │
│     Read README, CLAUDE.md, config files, entry points      │
│     Optionally: web search for ecosystem trends, CVEs       │
│                                                             │
│  2. PROVOKE                                                 │
│     Ask the brain targeted provocative questions:           │
│     - "What's the biggest risk nobody is addressing?"       │
│     - "What would fail first under 10x load?"              │
│     - "What would a new developer find most confusing?"    │
│     - "What's the highest-impact, lowest-effort change?"   │
│     - "What dependency is a ticking time bomb?"            │
│     - "Where is the code lying to itself?"                 │
│     Brain produces N candidate ideas (raw, unfiltered)      │
│                                                             │
│  3. SHAPE                                                   │
│     For each candidate idea:                                │
│       Turn it into a structured plan with:                  │
│       - Problem statement (what's wrong and why it matters) │
│       - Proposed solution (concrete, actionable)            │
│       - Scope estimate (files affected, effort)             │
│       - Risk assessment (what could go wrong)               │
│                                                             │
│  4. DESTROY (adversarial review — 3 rounds)                 │
│     Round 1: Devil's Advocate                               │
│     Round 2: Pragmatist                                     │
│     Round 3: Architect                                      │
│     Each round can: PASS, REVISE, or KILL                   │
│     Plan gets max 1 revision per round                      │
│     Must survive all 3 to reach human                       │
│                                                             │
│  5. PRESENT                                                 │
│     Survivors written as draft-* plans                      │
│     Summary printed to console                              │
│     Human reviews and decides                               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Adversarial Review Pipeline

The review pipeline is the core quality filter. Each reviewer is a separate Claude session with a distinct persona and objective. They receive the plan AND all prior reviewer feedback.

### Round 1: Devil's Advocate

**Objective**: Find every reason this is a bad idea.

```
You are a ruthless critic. Your job is to destroy this plan.

- What will this break?
- What's the hidden cost nobody mentioned?
- What second-order effects will this cause?
- Is this solving a real problem or a phantom one?
- What's the worst-case scenario if this goes wrong?
- Has the author considered rollback?

Verdict: KILL (with reason), REVISE (with specific demands), or PASS
```

### Round 2: Pragmatist

**Objective**: Is this worth doing? Is there a simpler way?

```
You are a pragmatic senior engineer with limited time and patience.

- Is the juice worth the squeeze? (effort vs impact)
- Is there a simpler solution that gets 80% of the benefit?
- Does this actually need to be done now, or is it premature?
- Will the team understand and maintain this change?
- What's the opportunity cost — what won't get done if we do this?

Previous reviewer said: {round_1_feedback}

Verdict: KILL, REVISE, or PASS
```

### Round 3: Architect

**Objective**: Does this fit the system? Will it age well?

```
You are a systems architect who thinks in decades.

- Does this align with the system's existing architecture?
- Does this create coupling or reduce it?
- Will this scale with the codebase as it grows?
- Does this introduce a new pattern, and is that pattern worth its weight?
- Is this the right abstraction level?

Previous reviewers said: {round_1_feedback}, {round_2_feedback}

Verdict: KILL, REVISE, or PASS
```

### Review Flow

```python
async def adversarial_review(plan_text: str) -> tuple[bool, str]:
    reviewers = [
        ("devil", DEVIL_PROMPT),
        ("pragmatist", PRAGMATIST_PROMPT),
        ("architect", ARCHITECT_PROMPT),
    ]

    feedback_history = []

    for reviewer_name, prompt in reviewers:
        review_input = format_review_input(plan_text, feedback_history)
        result = await brain.think(prompt + "\n\n" + review_input)
        verdict = parse_verdict(result)

        if verdict.action == "KILL":
            return False, f"Killed by {reviewer_name}: {verdict.reason}"

        feedback_history.append((reviewer_name, result))

        if verdict.action == "REVISE":
            plan_text = await brain.think(
                f"Revise this plan based on the reviewer's demands:\n\n"
                f"Demands: {verdict.demands}\n\n"
                f"Original plan:\n{plan_text}"
            )

    return True, plan_text
```

## Provocative Questions

The innovation quality depends on asking the right questions. These are not generic — they're designed to surface non-obvious insights.

### Risk & Resilience
- "What's the single point of failure in this system?"
- "If the database went down for 5 minutes, what would happen to users?"
- "What error conditions are silently swallowed?"
- "Where does this system trust input it shouldn't?"

### Scalability & Performance
- "What would fail first under 10x the current load?"
- "What O(n) operation is hiding that will become O(n²) with growth?"
- "What data structure will become the bottleneck?"

### Developer Experience
- "What would a new developer find most confusing in the first week?"
- "What takes 10 steps that should take 1?"
- "Where does the code contradict the documentation?"
- "What convention is inconsistently followed?"

### Architecture & Design
- "Where is the code lying to itself? (abstraction says X, implementation does Y)"
- "What module knows too much about another module's internals?"
- "What would you redesign if you could start this component over?"
- "What's duplicated across the codebase that should be unified?"

### Dependencies & Ecosystem
- "What dependency hasn't been updated in over a year?"
- "What dependency has a better alternative that didn't exist when this was written?"
- "What's the blast radius if dependency X has a breaking change?"

### Business & Product (speculative — based on code patterns)
- "What feature appears half-finished based on the code?"
- "What configuration option exists but appears unused?"
- "What error message would confuse a user?"

## CLI Interface

```bash
# Run full innovation cycle
foreman innovate

# With web search for ecosystem awareness
foreman innovate --web

# Limit scope
foreman innovate --path src/api/

# Control output volume
foreman innovate --max-ideas 5

# Skip adversarial review (just generate raw ideas as drafts)
foreman innovate --skip-review

# Only run review on existing draft plans (no new ideas)
foreman innovate --review-only

# Use specific question categories
foreman innovate --categories risk,architecture
```

## Implementation

### New module: `foreman/innovate.py`

```python
async def innovate(config: Config, options: InnovateOptions) -> list[Path]:
    brain = ForemanBrain(...)
    context = await gather_codebase_context(config, options)

    # Phase 1: Provoke — generate raw ideas
    questions = select_questions(options.categories)
    raw_ideas = await generate_ideas(brain, context, questions)

    # Phase 2: Shape — turn ideas into structured plans
    candidate_plans = await shape_plans(brain, raw_ideas, context)

    # Phase 3: Destroy — adversarial review
    survivors = []
    for plan in candidate_plans:
        survived, final_text = await adversarial_review(brain, plan)
        if survived:
            survivors.append(final_text)

    # Phase 4: Present — write draft plans
    draft_paths = write_draft_plans(config.plans_dir, survivors)
    return draft_paths
```

### Review session isolation

Each adversarial reviewer should be a **fresh Claude session** (no `--resume`), so they don't share context with the innovator brain. The innovator brain generates ideas; the reviewers are independent judges.

### Structured output for verdicts

Use `--json-schema` for reviewer verdicts:

```json
{
  "type": "object",
  "properties": {
    "action": {"type": "string", "enum": ["KILL", "REVISE", "PASS"]},
    "reason": {"type": "string"},
    "demands": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["action", "reason"]
}
```

## Config

```toml
[foreman.innovate]
max_ideas = 10
categories = ["risk", "performance", "architecture", "debt", "dx"]
review_rounds = 3
max_revisions_per_round = 1

[foreman.innovate.prompts]
explore = "plans/prompt-innovate-explore.md"
devil = "plans/prompt-innovate-devil.md"
pragmatist = "plans/prompt-innovate-pragmatist.md"
architect = "plans/prompt-innovate-architect.md"
```

## Changes Required

- [ ] New `foreman/innovate.py` module
- [ ] New `innovate` command in `cli.py`
- [ ] Codebase context gathering (shared with `analyze`)
- [ ] Adversarial review pipeline
- [ ] Reviewer prompt templates (devil, pragmatist, architect)
- [ ] Provocative question bank (categorized)
- [ ] `--json-schema` for structured reviewer verdicts
- [ ] Config additions for innovate settings

## What Makes This Different From `analyze`

| | `analyze` | `innovate` |
|---|---|---|
| Focus | User-directed (`--focus security`) | Autonomous (asks its own questions) |
| Scope | Single concern | Cross-cutting |
| Review | None (human reviews drafts directly) | Adversarial 3-round pipeline |
| Output | Draft plans | Reviewed, refined draft plans |
| Risk | Low (user chose the focus) | Medium (AI chose the focus) |
| Best for | Known concerns | Unknown unknowns |

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| AI generates obvious/useless suggestions | Provocative questions designed to surface non-obvious insights |
| Adversarial review is a rubber stamp | Separate sessions per reviewer, hostile personas, structured verdicts |
| Adversarial review kills everything | Max 3 rounds, 1 revision allowed per round, `--skip-review` escape hatch |
| Token cost is high (many Claude calls) | `--max-ideas` caps volume, `--categories` narrows scope |
| Ideas require business context AI doesn't have | Human gate is final — user rejects what doesn't fit |
| Innovation becomes noise | Plans are `draft-*` — zero impact until human renames them |
