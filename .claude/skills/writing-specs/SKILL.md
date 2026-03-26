---
name: writing-specs
description: Write high quality specifications or design docs for a program. Use any time you are asked to write, improve, or update specs / design docs (e.g., files in a `specs/` folder).
---

# Writing Specifications (specs)

This skill provides some guidelines and best practices for writing high quality specifications / design docs for a program (e.g., files in a `specs/` folder).

Note that writing user-facing documentation (docs) is covered a different skill; see the "writing-docs" skill for that.

Specs are targeted at developers and other technical stakeholders, and are intended to clearly communicate the design, architecture, and functionality of a program or system.

## Instructions

Before getting started, be sure to read through any relevant user documentation, existing specs, and current code (important to ensure consistency)

In particular, be sure to read any top level and project level `llm_faq.md` files, as those contain important information that you often forget about.

When writing specs from scratch, follow these steps *for each requested spec*:

1. Create a short description of the spec's purpose and scope.
2. Consider the existing documentation and code: is there *already* any conflicting or potentially outdated information? If so, concatenate an entry to the top-level `uncertainties.md` file listing the conflict so it can be resolved later, and then make a reasonable assumption about which information is correct for the purposes of writing the new spec.
3. Outline the main sections and topics to be covered.
4. Critique the outline: are there any gaps, redundancies, or unclear areas? In what order should the sections be presented? How can the content be organized for maximum clarity and usability?
5. Write 4 alternative outlines that address the critique and improve the structure. Focus on clear organization and logical flow. Use headings and subheadings to break down the content.
6. Choose the best outline (out of all of those that were generated.
7. Write a first draft of the spec based on the chosen outline.
8. Critique the draft: are there any parts of the spec that seem potentially wrong? Are there details that were overlooked? What are the main areas of complexity? Could the design be simplified? Is there anything missing that was requested? Are there any potential bugs or edge cases that were not considered? Basically--can anything go wrong here? If so, what? Is that *actually* a problem?  Be sure to actually write out the full text of your critique.
9. Revise the draft to address the important parts of the critique and then delete the critique file.
10. Evaluate the revised spec on a scale from 1 to 10 (1 = very poor, 10 = excellent) based on correctness, simplicity, clarity, and completeness.
11. If the evaluation score is below 8, repeat steps 8-11 until the score is 8 or higher.
12. Do a final editing pass to check for minor errors, inconsistencies, and ambiguities. Is there anything that could be clarified to make it easier and clearer for the person who will be implementing this? Have all the below best practices been followed?
13. Apply any final edits.

When updating or improving existing specs, follow an abbreviated version of the above process:

1. Consider the existing content (both the spec to improve, and any other sources): is there any conflicting or potentially outdated information? If so, concatenate an entry to the top-level `uncertainties.md` file listing the conflict so it can be resolved later. Decide whether it makes sense to give up on this spec and wait for the user to answer, or to make a reasonable assumption and proceed (depending on the request).
2. Create a plan for the improvements or updates, including an outline of the changes to be made. 
3. Critique the plan: does the entire spec need larger changes, or just specific sections? Are there any potentially important updates that are missing? Are there suggested updates that are pointless?
4. If the entire spec needs to be re-written, use the above process for writing a spec from scratch, starting at step 3 (outlining). Otherwise, proceed to the next step.
5. Make the improvements and updates.
6. Critique the draft: are there any parts of the spec that seem potentially wrong? Are there details that were overlooked? What are the main areas of complexity? Could the design be simplified? Is there anything missing that was requested? Are there any potential bugs or edge cases that were not considered? Basically--can anything go wrong here? If so, what? Is that *actually* a problem?  Be sure to actually write out the full text of your critique.
7. Revise the spec to address the important parts of the critique.
8. Do a final editing pass to check for minor errors, inconsistencies, and ambiguities. Is there anything that could be clarified to make it easier and clearer for the person who will be implementing this? Have all the below best practices been followed?
9. Apply any final edits.
 
## Best Practices

Always remember the following best practices for specifications and design docs:

- Write sentences that are clear, concise, and easy to understand.
- Be explicit about requirements, constraints, interfaces, and assumptions.
- Always write in Markdown format (.md files).
- Name files and headings clearly to reflect their content.
- Use "snake_case" for file names.
- Always link to other relevant specs and documentation.
- Each spec should have a clear purpose and target audience.
- Use headings, subheadings, and lists to organize content and improve readability.
- Include examples, code snippets, and diagrams where appropriate to illustrate concepts.
- Use consistent formatting and style throughout.
- Use callouts (e.g., **Note:**, **Warning:**) to highlight important information.
- Document edge cases, error handling, and failure modes.
- Never use emojis.
