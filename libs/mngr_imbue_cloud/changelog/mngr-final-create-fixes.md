- Removed the soft `preferred_region` lease knob. A lease now takes only the hard
  `region` build arg (`-b region=<dc>`): when set, only a host in that OVH
  datacenter is leased, otherwise the lease is region-agnostic.
