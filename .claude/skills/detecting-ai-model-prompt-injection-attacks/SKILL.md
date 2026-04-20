# Detecting AI Model Prompt Injection Attacks

Local-only checklist for reviewing prompt-injection defenses in AI features.

## Safe scope

Use this skill to:
- inspect prompts, templates, and tool-call surfaces for injection exposure
- review whether user content is separated from system guidance
- check for output validation and least-privilege tool access
- summarize likely prompt-injection risk patterns from code and configuration

Do not use this skill to fetch models, install packages, call external services, or execute downloaded detection agents.

## Review workflow

1. Identify where untrusted text enters the system:
   - chat input
   - retrieved documents
   - web content
   - tool results
2. Check whether system guidance is isolated from untrusted content.
3. Check whether tools or secrets are reachable from compromised prompts.
4. Look for signs of missing output validation or missing privilege boundaries.
5. Produce findings with concrete file/function references.

## Findings taxonomy

- `input_boundary_missing`
- `retrieval_content_untrusted`
- `tool_privilege_too_broad`
- `output_validation_missing`
- `prompt_template_mixes_control_and_data`

## Output contract

```text
risk_level: low|medium|high

entry_points:
- path: ...
  kind: chat_input|retrieval|tool_output|other

findings:
- severity: low|medium|high
  kind: input_boundary_missing|retrieval_content_untrusted|tool_privilege_too_broad|output_validation_missing|prompt_template_mixes_control_and_data
  path: ...
  note: ...

recommended_controls:
- isolate system prompts from user content
- reduce tool privileges
- validate outputs before action
- treat retrieved content as untrusted
```
