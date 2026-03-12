---
name: writing-docs
description: Write high quality, user-facing documentation. Use any time you need to write, improve, or update a significant amount of user-facing documentation (e.g., files in a "docs/" folder or README file).
---

# Writing Documentation

This skill provides some guidelines and best practices for writing high quality, user-facing documentation (e.g., files in a "docs/" folder or README file).

Note that writing specs (specifications) and design documents is covered a different skill; see the "writing-specs" skill for that.

Docs are targeted at end users, and are intended to clearly communicate how to use a program or system.

## Instructions

Before getting started, be sure to read through the relevant documentation that already exists (important to avoid duplication and ensure consistency).

In particular, be sure to read any top level and project level "llm_faq.md" files, as those contain important information that you often forget about.

Be mindful that some documentation may be purposefully incomplete (ex: placeholders for future functionality).

In order to write effective documentation from scratch, follow these steps *for each document*:

1. Create a short description of the documentation's purpose and scope.
2. Consider the existing documentation: is there *already* any conflicting or potentially outdated information? If so, concatenate an entry to the top-level "uncertainties.md" file listing the conflict so it can be resolved later, and then make a reasonable assumption about which information is correct for the purposes of writing the new documentation.
3. Outline the main sections and topics to be covered.
4. Critique the outline: are there any gaps, redundancies, or unclear areas? In what order should the sections be presented? How can the content be organized for maximum clarity and usability?
5. Write 4 alternative outlines that address the critique and improve the structure. Focus on clear organization and logical flow. Use headings and subheadings to break down the content.
6. Choose the best outline (out of all of those that were generated.
7. Write a first draft of the documentation based on the chosen outline.
8. Critique the draft: is the content clear, concise, and accurate? Are there any areas that need more explanation or examples? Is the formatting consistent and easy to read? Is all necessary information included? Is there information that should be moved somewhere else?
9. Revise the draft to address the important parts of the critique.
10. Evaluate the revised draft on a scale from 1 to 10 (1 = very poor, 10 = excellent) based on clarity, completeness, accuracy, and usability.
11. If the evaluation score is below 8, repeat steps 8-11 until the score is 8 or higher.
12. Do a final editing pass to check for minor errors, inconsistencies, and wording improvements. Can some words be trimmed? Are there certain sentences that are hard to read or redundant? Have all the below best practices been followed?
13. Apply any final edits.
14. Consider the document you wrote: is it consistent with other related documentation? If not, identify any necessary changes to other documents to ensure consistency, and make those changes following the same process outlined here.

In order to improve or update existing documentation, follow these steps:

1. Review the existing documentation and identify areas for improvement or updates.
2. Consider the existing documentation (both the doc to improve, and the other docs): is there any conflicting or potentially outdated information? If so, concatenate an entry to the top-level "uncertainties.md" file listing the conflict so it can be resolved later. Decide whether it makes sense to give up on this document and wait for the user to answer, or to make a reasonable assumption and proceed (depending on the request).
3. Create a plan for the improvements or updates, including an outline of the changes to be made.
4. Critique the plan: does the entire document need larger changes, or just specific sections? Are there any potentially important updates that are missing? Are there suggested updates that are pointless?
5. If the entire document needs to be re-written, use the above process for writing documentation from scratch, starting at step 3 (outlining). Otherwise, proceed to the next step.
6. Make the improvements and updates.
7. Critique the revised documentation: is the content clear, concise, and accurate? Were any changes unnecessary? Did any changes make the doc worse? Is the information consistent with the rest of the documentation? Is the document the right length and at the right level of depth? Is there anything redundant, either within the document, or with other documents? Is there information that should be moved somewhere else?
8. Revise the documentation to address the important parts of the critique.
9. Evaluate the revised documentation on a scale from 1 to 10 (1 = very poor, 10 = excellent) based on clarity, completeness, accuracy, and usability.
10. If the evaluation score is below 8, repeat steps 7-10 until the score is 8 or higher.
11. Do a final editing pass to check for minor errors, inconsistencies, and wording improvements. Can some words be trimmed? Are there certain sentences that are hard to read or redundant? Have all the below best practices been followed?
12. Apply any final edits.
 
## Best Practices

Always remember the following best practices for user-facing documentation:

- Write sentences that are clear, concise, and easy to understand.
- Always write in Markdown format (.md files).
- Name files and headings clearly to reflect their content.
- Use "snake_case" for file names.
- Keep files reasonably short (ideally under 1,500 words); if a document is getting too long, consider breaking it into smaller, linked documents.
- Always link to other relevant documentation!
- Never repeat information.
- Each document should have a clear purpose and target audience.
- Each document should stay at the same level of abstraction and detail.
- Use an active voice and address the reader directly (e.g., "you can do X by...").
- Use simple language and avoid jargon or technical terms unless necessary.
- Use headings, subheadings, bullet points, and numbered lists to organize content and improve readability.
- Prefer lists over tables (e.g. if there are 2 or fewer columns).
- Include examples, code snippets, and diagrams where appropriate to illustrate concepts.
- Use consistent formatting and style throughout the documentation.
- Avoid talking about implementation details.
- Use callouts (e.g., **Note:**, **Warning:**) to highlight important information.
- Never use emojis.
