Added `apps/minds/scripts/vm-testing/`: a Tart-based macOS VM test harness
for the packaged `minds.app`. Team members can run
`apps/minds/scripts/vm-testing/run-test.sh <build-url-or-local-zip> minds-fresh`
on their own Macs to verify a build end-to-end (install -> launch -> agent
creation -> first message -> agent reply observed). Results land as
junit.xml + summary.json + raw logs under
`apps/minds/scripts/vm-testing/.results/<ts>-<persona>/`. v1 ships one
persona (`minds-fresh`) and one happy-path scenario; the structure accepts
more personas without rework.
