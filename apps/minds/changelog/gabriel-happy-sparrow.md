A workspace whose machine recovered while the laptop was asleep no longer comes back as a blank white pane after the wake.

Before, the reload the app owes a recovered machine was held until the device's network had been back for a settle interval, but a laptop that went to sleep inside that interval ended it at the instant of the next wake, which is exactly the interface transition the hold exists to outlast. The reload went out into that transition, the frame failed to load, and nothing retried it, so both open windows stayed white until the user navigated away and back.

Now a wake opens a settle window of its own, so a machine whose recovery lands in the seconds after the lid opens is held like one raised at a network transition, and the settle worker records the wake itself when its wait returns, so the window it slept through is superseded by one that starts from the wake.
