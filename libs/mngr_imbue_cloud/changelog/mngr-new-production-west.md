`mngr imbue_cloud admin server order` can now order plans whose mandatory option
families (e.g. bandwidth, vrack) offer more than one choice. Previously the cart
build failed with "expected exactly one X option to auto-pick" on such plans
(e.g. the `24sys*` SYS line). It now auto-picks the cheapest month-to-month offer
in each multi-offer mandatory family -- the included baseline, which is exactly
the configuration the `admin server pricing` table quotes. It still refuses to
guess when offers lack comparable prices or tie at the cheapest price.
