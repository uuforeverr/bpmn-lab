# Prompt Structure Template

Every business-process modeling prompt must use the following seven top-level sections in this exact order:

1. `# Role`
2. `# Task Objective`
3. `# Input`
4. `# Domain Knowledge`
5. `# Execution Rules`
6. `# Output Format`
7. `# Examples`

## Reading Order

- Organize information from top to bottom. Never use a term before defining it.
- Role states only the identity and core responsibility. It must not reference input fields, output objects, or other Agents that have not been introduced.
- Task Objective states the business problem without prematurely describing schemas, domain rules, or output details.
- Input first defines every input field and specialized term used later.
- Domain Knowledge contains only the business-process knowledge required for the task.
- Execution Rules follow the actual reasoning and execution order. Avoid repeated rules and long enumerations of prohibited behavior.
- Output Format defines the exact output structure, field constraints, and empty result.
- Examples are optional few-shot content. Leave the section empty when examples are unnecessary instead of adding them by default.
- Use at most three few-shot examples in one prompt, and use the smallest number needed to cover scenarios that are difficult to define through rules alone.

## Writing Principles

- Express the task with minimal, precise, and unambiguous language.
- Emphasize how to perform the task instead of listing many things not to do.
- State each requirement once. Repeat only essential field constraints in Output Format.
- Define concepts before using them, state the objective before the rules, and place output requirements and examples last.
- Currently only the incremental Generator and Generator Revision use few-shot examples. Other prompts remain example-free unless a later task explicitly requires them.
