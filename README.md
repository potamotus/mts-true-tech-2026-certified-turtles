# Hackathon Team Repo

## AGENT INSTRUCTIONS

If you are an AI agent (Claude, Cursor, Copilot, etc.), read this file carefully and follow all rules.

### Core Rules
1. **Read before writing** — never assume file contents, always read first
2. **Follow existing patterns** — match naming, style, and structure of surrounding code
3. **Feature branches only** — create `feature/<short-description>`, never push to `main` directly
4. **Small commits** — many small commits > one huge commit
5. **No secrets** — never commit `.env` files or credentials
6. **Build before PR** — run the build/lint command and verify it passes before claiming work is done
7. **One task = one branch = one PR**
8. **Don't over-engineer** — hackathon means ship fast, not perfect
9. **Delete unused code** — no commented-out blocks, no dead files
10. **Ask if ambiguous** — if the task is unclear, ask clarifying questions before proceeding

### Conventional Commits
- `feat:` new feature
- `fix:` bug fix
- `chore:` config, deps, tooling
- `docs:` documentation
- `refactor:` code restructuring (no behavior change)

### Task Tracking
- Use GitHub Issues — create issue per task, close on merge
- Reference issues in PR descriptions: `Closes #1`

---

## Project Status

**Stack and task are NOT yet decided.** We are choosing between:

### Option 1: LocalScript — Local Lua Code Generation (MWS Octapi)
Autonomous agent system on a lightweight local LLM that generates and validates Lua code without sending data externally.

**Likely stack:** Python + ollama/llama.cpp + Lua parser + CLI/TUI
**Key:** offline-first, Lua validation, MWS Octapi integration

### Option 2: WikiLive — Live Tables in Text (MWS Tables)
Wiki module where text and tables become a unified tool for collaborative work and knowledge management.

**Likely stack:** Next.js + TypeScript + Tailwind + WebSocket/CRDT
**Key:** real-time collaboration, live tables, wiki syntax

**Current repo structure is a placeholder** (Next.js starter). It will be restructured once the task is chosen.

---

## Karpathy Guidelines

Behavioral guidelines to reduce common LLM coding mistakes, derived from [Andrej Karpathy's observations](https://x.com/karpathy/status/2015883857489522876) on LLM coding pitfalls.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.
- Remove imports/variables/functions that YOUR changes made unused.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

---

## Team Workflow

1. **Clone:** `git clone https://github.com/potamotus/mts-true-tech-2026-certified-turtles.git`
2. **Branch:** `git checkout -b feature/<name>`
3. **Work → Commit → Push → PR** into `main`
4. **Review:** at least 1 approve before merge

## Deploy

Connected to Vercel. Every push to `main` = auto-deploy.
Preview: https://mts-true-hack-certified-turtles.vercel.app
