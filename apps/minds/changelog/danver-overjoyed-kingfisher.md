Documented why the two pnpm supply-chain cooldown exemptions in
`apps/minds/pnpm-workspace.yaml` (`minimumReleaseAgeExclude`: `latchkey`,
`@imbue-ai/detent`) are standing rather than temporary: both are Imbue-published, so
they are trusted on release instead of after the 14-day minimum release age, and a
latchkey bump lands here within days of publish. No behavior change; a repo-wide audit
of cooldown exceptions found these two still load-bearing and the comment keeps a
later audit from removing them.
