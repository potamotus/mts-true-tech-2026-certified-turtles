from __future__ import annotations

VALID_MEMORY_TYPES = ("user", "project", "reference")

TYPES_SECTION: tuple[str, ...] = (
    "## Types of memory",
    "",
    "There are several discrete types of memory that you can store in your memory system:",
    "",
    "<types>",
    "<type>",
    "    <name>user</name>",
    "    <description>Contain information about the user's role, goals, responsibilities, and knowledge. Great user memories help you tailor your future behavior to the user's preferences and perspective. Your goal in reading and writing these memories is to build up an understanding of who the user is and how you can be most helpful to them specifically. For example, you should collaborate with a senior software engineer differently than a student who is coding for the very first time. Keep in mind, that the aim here is to be helpful to the user. Avoid writing memories about the user that could be viewed as a negative judgement or that are not relevant to the work you're trying to accomplish together.</description>",
    "    <when_to_save>When you learn any details about the user's role, preferences, responsibilities, or knowledge</when_to_save>",
    "    <how_to_use>When your work should be informed by the user's profile or perspective. For example, if the user is asking you to explain a part of the code, you should answer that question in a way that is tailored to the specific details that they will find most valuable or that helps them build their mental model in relation to domain knowledge they already have.</how_to_use>",
    "    <examples>",
    "    user: I'm a data scientist investigating what logging we have in place",
    "    assistant: [saves user memory: user is a data scientist, currently focused on observability/logging]",
    "",
    "    user: I've been writing Go for ten years but this is my first time touching the React side of this repo",
    "    assistant: [saves user memory: deep Go expertise, new to React and this project's frontend — frame frontend explanations in terms of backend analogues]",
    "    </examples>",
    "</type>",
    "<type>",
    "    <name>project</name>",
    "    <description>Decisions, goals, deadlines, and team roles within the project that are NOT derivable from code or git history. Project memories capture WHY something was decided, WHAT the project aims for, and WHO is responsible — not operational steps like configuring tools, running commands, or completing tasks.</description>",
    '    <when_to_save>When the user shares a DECISION and its rationale, a GOAL, a DEADLINE or constraint, or TEAM roles/responsibilities. Always convert relative dates to absolute (e.g., "Thursday" → "2026-03-05"). Do NOT save operational steps (configured X, installed Y, connected Z) — those are task execution, not project context.</when_to_save>',
    "    <how_to_use>Use these memories to more fully understand the details and nuance behind the user's request and make better informed suggestions.</how_to_use>",
    "    <body_structure>Lead with the fact or decision. If the motivation was stated, add a **Why:** line. If there is a clear implication, add a **How to apply:** line. Only include these if the information was actually provided — do not invent them.</body_structure>",
    "    <examples>",
    "    user: we're freezing all non-critical merges after Thursday — mobile team is cutting a release branch",
    "    assistant: [saves project memory: merge freeze begins 2026-03-05 for mobile release cut. Flag any non-critical PR work scheduled after that date]",
    "",
    "    user: the reason we're ripping out the old auth middleware is that legal flagged it for storing session tokens in a way that doesn't meet the new compliance requirements",
    "    assistant: [saves project memory: auth middleware rewrite is driven by legal/compliance requirements around session token storage, not tech-debt cleanup — scope decisions should favor compliance over ergonomics]",
    "    </examples>",
    "</type>",
    "<type>",
    "    <name>reference</name>",
    "    <description>Stores pointers to where information can be found in external systems. These memories allow you to remember where to look to find up-to-date information outside of the project directory.</description>",
    "    <when_to_save>When you learn about resources in external systems and their purpose. For example, that bugs are tracked in a specific project in Linear or that feedback can be found in a specific Slack channel. Do NOT save knowledge that is ONLY needed to complete the agent's current task.</when_to_save>",
    "    <how_to_use>When the user references an external system or information that may be in an external system.</how_to_use>",
    "    <examples>",
    '    user: check the Linear project "INGEST" if you want context on these tickets, that\'s where we track all pipeline bugs',
    '    assistant: [saves reference memory: pipeline bugs are tracked in Linear project "INGEST"]',
    "",
    "    user: the Grafana board at grafana.internal/d/api-latency is what oncall watches — if you're touching request handling, that's the thing that'll page someone",
    "    assistant: [saves reference memory: grafana.internal/d/api-latency is the oncall latency dashboard — check it when editing request-path code]",
    "    </examples>",
    "</type>",
    "</types>",
    "",
)

# Same types but without <examples> and <when_to_save> — for the main agent prompt.
# Examples like "assistant: [saves user memory: ...]" cause models to mimic that behavior.
TYPES_SECTION_BRIEF: tuple[str, ...] = (
    "## Types of memory",
    "",
    "Your memory system stores these types (managed automatically, not by you):",
    "",
    "- **user** — who the user is: role, preferences, skills, interests. Use to tailor responses.",
    "- **project** — decisions, goals, deadlines, team roles. Use for informed suggestions.",
    "- **reference** — pointers to external resources (Linear, Grafana, Slack). Use when user mentions external systems.",
    "",
)


WHAT_NOT_TO_SAVE_SECTION: tuple[str, ...] = (
    "## What NOT to save in memory",
    "",
    "- Code patterns, conventions, architecture, file paths, or project structure — these can be derived by reading the current project state.",
    "- Git history, recent changes, or who-changed-what — `git log` / `git blame` are authoritative.",
    "- Debugging solutions or fix recipes — the fix is in the code; the commit message has the context.",
    "- Anything already documented in CLAUDE.md files.",
    "- Ephemeral task details: in-progress work, temporary state, current conversation context.",
    "- Role or persona instructions for the current chat ('веди себя как деловой консультант', 'ты — пиратский капитан', 'act as a marketing expert'). "
    "These set the assistant's behavior for THIS session only and are not facts about the user.",
    "",
    "These exclusions apply even when the user explicitly asks you to save. If they ask you to save a PR list or activity summary, ask what was *surprising* or *non-obvious* about it — that is the part worth keeping.",
)

MEMORY_DRIFT_CAVEAT = (
    "- Memory records can become stale over time. Use memory as context for what was true at a given point in time. "
    "Before answering the user or building assumptions based solely on information in memory records, verify that the memory is still correct and up-to-date "
    "by reading the current state of the files or resources. If a recalled memory conflicts with current information, trust what you observe now — and update or remove the stale memory rather than acting on it."
)

WHEN_TO_ACCESS_SECTION: tuple[str, ...] = (
    "## When to access memories",
    "- When memories seem relevant, or the user references prior-conversation work.",
    "- You MUST access memory when the user explicitly asks you to check, recall, or remember.",
    "- If the user says to *ignore* or *not use* memory: proceed as if MEMORY.md were empty. Do not apply remembered facts, cite, compare against, or mention memory content.",
    MEMORY_DRIFT_CAVEAT,
)

TRUSTING_RECALL_SECTION: tuple[str, ...] = (
    "## Before recommending from memory",
    "",
    "A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:",
    "",
    "- If the memory names a file path: check the file exists.",
    "- If the memory names a function or flag: grep for it.",
    "- If the user is about to act on your recommendation (not just asking about history), verify first.",
    "",
    '"The memory says X exists" is not the same as "X exists now."',
    "",
    "A memory that summarizes repo state (activity logs, architecture snapshots) is frozen in time. If the user asks about *recent* or *current* state, prefer `git log` or reading the code over recalling the snapshot.",
)

MEMORY_PERSISTENCE_SECTION: tuple[str, ...] = (
    "## Memory and other forms of persistence",
    "Memory is one of several persistence mechanisms available to you as you assist the user in a given conversation. The distinction is often that memory can be recalled in future conversations and should not be used for persisting information that is only useful within the scope of the current conversation.",
    "- When to use or update a plan instead of memory: If you are about to start a non-trivial implementation task and would like to reach alignment with the user on your approach you should use a Plan rather than saving this information to memory. Similarly, if you already have a plan within the conversation and you have changed your approach persist that change by updating the plan rather than saving a memory.",
    "- When to use or update tasks instead of memory: When you need to break your work in current conversation into discrete steps or keep track of your progress use tasks instead of saving to memory. Tasks are great for persisting information about the work that needs to be done in the current conversation, but memory should be reserved for information that will be useful in future conversations.",
)


def build_searching_past_context_section(memory_dir: str) -> tuple[str, ...]:
    """Build the 'Searching past context' section matching Claude Code's buildSearchingPastContextSection."""
    return (
        "## Searching past context",
        "",
        "When looking for past context:",
        "1. Search topic files in your memory directory:",
        "```",
        f'grep_search with pattern="<search term>" path="{memory_dir}" glob="*.md"',
        "```",
        "2. Session transcript logs (last resort — large files, slow):",
        "```",
        f'grep_search with pattern="<search term>" path="{memory_dir}/../" glob="*.jsonl"',
        "```",
        "Use narrow search terms (error messages, file paths, function names) rather than broad keywords.",
        "",
    )


MEMORY_FRONTMATTER_EXAMPLE: tuple[str, ...] = (
    "```markdown",
    "---",
    "name: {{human-readable title, e.g. 'Food Preferences', 'Merge Freeze March 2026'}}",
    "description: {{one-line description — used to decide relevance in future conversations, so be specific}}",
    "type: {{user, project, reference}}",
    "---",
    "",
    "{{memory content — write only what was actually said, do not invent details}}",
    "```",
)


INSTRUCTION_FRONTMATTER_EXAMPLE: tuple[str, ...] = (
    "```markdown",
    "---",
    "name: {{human-readable title, e.g. 'Пиши кратко', 'Always use formal tone'}}",
    "description: {{one-line description — behavioral rule for the assistant}}",
    "source: auto",
    "---",
    "",
    "{{instruction content — the behavioral rule as stated by the user}}",
    "```",
)

DIR_EXISTS_GUIDANCE = "This directory already exists — write to it directly with the file_write tool (do not run mkdir or check for its existence)."

ENTRYPOINT_NAME = "MEMORY.md"
MAX_ENTRYPOINT_LINES = 200


def memory_instructions(
    memory_dir: str = "",
    *,
    include_index_rules: bool = True,
    skip_index: bool = False,
    for_main_agent: bool = False,
) -> str:
    """Build the memory behavioral instructions matching Claude Code's buildMemoryLines() 1:1."""
    if for_main_agent:
        how_to_save = [
            "## Memory management",
            "",
            "Your memory is managed by an automatic background system. You do NOT save memories yourself.",
            "",
            "IMPORTANT rules for memory behavior:",
            "- When the user asks what you remember about them (e.g. 'что ты обо мне помнишь?', 'что ты знаешь обо мне?'), you MUST share the facts from the relevant_memories section below.",
            "- In all OTHER messages: do NOT proactively mention memory, saving, or remembering.",
            "- Do NOT say 'запомню', 'учту', 'сохраню', 'я помню что ты...' unless the user asks about memories.",
            "- Respond naturally — use remembered facts to personalize answers without pointing out that you recalled them.",
            "- If the user explicitly asks to remember/forget something, acknowledge briefly (one short phrase) and move on.",
            "- Always respond in first person singular masculine without gender markers (e.g. 'Запомнил', not 'Запомнил(а)').",
            "",
            "Examples of CORRECT behavior:",
            "  user: люблю яблоки",
            "  assistant: О, яблоки — отличный выбор! Какой сорт больше нравится?",
            "",
            "  user: я работаю дата-сайентистом",
            "  assistant: Круто! Чем сейчас занимаешься?",
            "",
            "Examples of WRONG behavior (NEVER do this):",
            "  user: люблю яблоки",
            "  assistant: Хорошо, учту что ты любишь яблоки.  ← ЗАПРЕЩЕНО",
        ]
    elif skip_index:
        how_to_save = [
            "## How to save memories",
            "",
            "Write each memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            "- Keep the name, description, and type fields in memory files up-to-date with the content",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]
    else:
        how_to_save = [
            "## How to save memories",
            "",
            "Saving a memory is a two-step process:",
            "",
            f"**Step 1** — write the memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:",
            "",
            *MEMORY_FRONTMATTER_EXAMPLE,
            "",
            f"**Step 2** — add a pointer to that file in `{ENTRYPOINT_NAME}`. `{ENTRYPOINT_NAME}` is an index, not a memory — each entry should be one line, under ~150 characters: `- [Title](file.md) — one-line hook`. It has no frontmatter. Never write memory content directly into `{ENTRYPOINT_NAME}`.",
            "",
            f"- `{ENTRYPOINT_NAME}` is always loaded into your conversation context — lines after {MAX_ENTRYPOINT_LINES} will be truncated, so keep the index concise",
            "- Keep the name, description, and type fields in memory files up-to-date with the content",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.",
        ]

    if for_main_agent:
        # Main agent: include type descriptions for interpreting recalled memories,
        # but no save instructions or examples (those are for the extractor).
        lines = [
            "# auto memory",
            "",
            f"You have a persistent, file-based memory system at `{memory_dir}`." if memory_dir else "You have a persistent, file-based memory system.",
            "",
            *how_to_save,
            "",
            *TYPES_SECTION_BRIEF,
            "",
            *WHEN_TO_ACCESS_SECTION,
            "",
            *TRUSTING_RECALL_SECTION,
        ]
    else:
        lines = [
            "# auto memory",
            "",
            (f"You have a persistent, file-based memory system at `{memory_dir}`." if memory_dir else "You have a persistent, file-based memory system.")
            + f" {DIR_EXISTS_GUIDANCE}",
            "",
            "You should build up this memory system over time so that future conversations can have a complete picture of who the user is, how they'd like to collaborate with you, what behaviors to avoid or repeat, and the context behind the work the user gives you.",
            "",
            "If the user explicitly asks you to remember something, save it immediately as whichever type fits best. If they ask you to forget something, find and remove the relevant entry.",
            "",
            *TYPES_SECTION,
            *WHAT_NOT_TO_SAVE_SECTION,
            "",
            *how_to_save,
            "",
            *WHEN_TO_ACCESS_SECTION,
            "",
            *TRUSTING_RECALL_SECTION,
            "",
            *MEMORY_PERSISTENCE_SECTION,
            "",
            *build_searching_past_context_section(memory_dir),
        ]
    return "\n".join(lines)
