# mng-tmr

Test map-reduce plugin for [mng](https://github.com/imbue-ai/mng).

Collects tests via pytest, launches one agent per test to run and optionally fix failures, polls for completion, and generates an HTML report. Successful fixes are pulled into local branches and optionally merged by an integrator agent.
