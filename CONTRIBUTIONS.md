# Contributions and AI-assistance disclosure

This document records only what can be supported by repository history. It does not infer team
roles from names, and it does not merge identities that may belong to the same person.

## Repository-visible contributions

| Git identity | Repository-visible work | Evidence boundary |
|---|---|---|
| `jinsan02 <jinsanroh02@gmail.com>` / `jinsa <jinsa@users.noreply.github.com>` | Initial stack and documentation; Redis/MQTT/API integration; M1–M5 orchestration; Qwen backends and rule gate; Phase 2 alert flow; performance and thread tuning; tests and release documentation | The two identities appear related but are listed together only for readability; ownership should be confirmed by the team before external publication. |
| `sominseob <pksomin@gmail.com>` | M3 environment-sound integration, label/remote-sound adjustments, audio timing fields, and backend handoff documentation | Based on commit authorship and subjects; fine-grained design/testing responsibility is not inferred. |
| `composedly13 <composedly13@gmail.com>` | M1 64-subcarrier/per-node-window experiments and their reverts | Repository history shows both implementation and reversion; current production ownership is not inferred. |

For resumes, portfolios, or interviews, each person should state only the features they personally
designed, implemented, tested, or reviewed. Team confirmation is required for data collection,
hardware assembly, paper writing, and presentation roles because commit history alone cannot prove
those activities.

## AI-tool assistance

Three M3-related commits contain a `Co-authored-by: Cursor <cursoragent@cursor.com>` trailer.
Repository history therefore supports disclosing AI assistance for those changes. No additional
AI authorship should be claimed or denied without separate evidence.

AI assistance does not establish correctness. Human contributors remain responsible for:

- selecting the system architecture and requirements;
- reviewing generated code and documentation;
- running tests and hardware measurements;
- checking licenses, model provenance, safety limits, and final claims;
- deciding what is merged and presented externally.

Suggested portfolio disclosure: “AI coding tools assisted selected implementation and documentation
tasks; I reviewed the changes and separate tool-generated output from measured evidence.”

## Research award

The 2026 Korean Digital Contents Society undergraduate-paper Bronze Award is a team research
outcome associated with the SafeWave/capstone direction. It must not be used to imply that every
repository contributor authored the paper, nor that model accuracy or clinical safety was validated.
