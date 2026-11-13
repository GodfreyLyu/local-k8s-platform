<!-- Purpose: Define development conventions and change boundaries for this standalone skill project. -->

# Skill project conventions

Work inside this directory. Use sibling projects only as read-only references. Do not modify or delete their files.

Use English for documentation, comments, configuration descriptions, and examples. Keep code direct and readable. Check user input at the entry point, then pass resolved values to internal functions. Repeat a check only when files, external data, or live state may have changed.

Start each maintained file with a purpose comment. Keep `SKILL.md` frontmatter first, then add its HTML purpose comment. Use a top-level `$comment` in source JSON. Generated state, plans, certificates, and other runtime files keep their required formats.

Above each function or method, describe its inputs, outputs, purpose, and dependencies. Two short comment lines are enough. Explain non-obvious decisions near the relevant code.

Use the packaged scripts to run operations. Keep credentials out of Terraform inputs, state, command arguments, and printed output. Use private work directories. Preserve data and resource ownership.

The build task allows file creation, local validation, and isolated tests. It does not authorize cluster changes. Get authorization for the current plan before live deployment. Include Secret writes and any tests that change cluster state in that scope.

Do not create or recreate clusters, replace operators or CNI, or import resources as a side effect. Do not destroy environments or clean up unknown data.

Check changed behavior with focused offline tests. Use fake tools to test command orchestration. Keep test files in private temporary directories. Runtime acceptance needs evidence from the selected environment.
