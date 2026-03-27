# mngr-tmr

Test map-reduce plugin for [mngr](https://github.com/imbue-ai/mngr).

Collects tests via pytest, launches one agent per test to run and optionally fix failures, polls for completion, and generates an HTML report. Successful fixes are pulled into local branches and optionally merged by an integrator agent.
