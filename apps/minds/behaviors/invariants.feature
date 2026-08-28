Feature: Minds desktop client invariants
  Properties that hold across every surface, flow, and interleaving in this corpus. They concern what the desktop client may conclude about a workspace or a provider from evidence gathered on a laptop, whose sleep and network are conditions of the observer rather than of the thing observed.

  @no-verdict-on-unobserved-time
  Rule: No negative verdict rests on time this process was not running
    Every conclusion that something has failed -- a workspace convicted as stuck, a discovery producer read as stalled, a provider reported unreachable, this device reported offline -- is reached only from evidence gathered while this process was running.
    A measurement whose window contains an interval in which the process was not running (a laptop sleep, a suspension) is not evidence: a failure run accumulates again from the wake, a staleness age is measured from the wake, and a reading or a poll whose window spanned the interval is discarded rather than recorded.
    Rationale: when the laptop stops, every remote thing stops answering at once, and each such silence would otherwise read as the far side dying. A verdict built on such a window blames a workspace or a provider for the observer having been away.

  @no-blame-past-an-unmeasured-device
  Rule: Nothing on the far side of this device's network is blamed while the device's own reach is confirmed bad or unmeasured
    Every verdict whose evidence is this device's failure to reach something -- a provider reported unreachable, a connection reported as failing on this device -- is withheld while this device is confirmed unable to reach the network those verdicts depend on, and equally while nothing has yet measured that reach (before the first probe, and after a wake until the next one). A confirmed device-side condition is named instead; an unmeasured one names nobody. A verdict that reports an observed outcome rather than attributing reach -- a restart that ran and failed -- keeps its report; what this rule adds there is that a confirmed device-side condition outranks it, since it explains it.
    An unattended restart of a workspace reached over the network is likewise withheld while the device is confirmed unable to reach it, and released when its reach returns. A workspace that runs on this device is exempt from every part of this rule: the network says nothing about it.
    An unmeasured device withholds a verdict but never an action: the absence of a measurement is not evidence that the device is broken.
    Rationale: a laptop with no network cannot reach the provider either, so the provider's own poll fails too; naming the provider then blames a backend that is fine for a condition only the user can fix.
