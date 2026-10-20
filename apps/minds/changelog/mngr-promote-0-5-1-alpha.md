Promoted minds 0.5.1 to the alpha channel (build `260908p5hzq053h`, `minds-v0.5.1`), at 100% of the channel. Beta and stable stay on 0.5.0 build `260902shwco3ynx`.

The production pool is baked at `minds-v0.5.1` ahead of this: 11 slices in US-WEST-OR and 7 in US-EAST-VA, all verified container-side. US-WEST-OR had no leasable rows at any version beforehand, so every create there was taking the slow rebuild path.

The production services are still the 0.4.3 deploy, so browser creates (`/hosts/claim`) remain pinned to `minds-v0.4.3` while alpha desktop clients ask for `minds-v0.5.1`. That is why this stops at alpha: the services deploy is optional for alpha and required before beta or stable.
