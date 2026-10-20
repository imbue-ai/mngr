Follow the hosted signup page's plan cards and the free plan's new "Limited" label.

- The Accounts page shows the `free` plan as "Limited", in both the current-plan line and the plan switcher; the stored plan name is unchanged. The switcher's Limited description matches the signup page's notice.

- `test_hosted_pages_signup_via_playwright` asserts Explorer is preselected by checking that the Explorer card's radio is marked checked (`aria-checked`), instead of the old dropdown's value.

- `test_hosted_pages_signup_via_playwright` checks the create-account tab's "By continuing, you agree to ..." notice instead of ticking the removed terms checkbox (and no longer expects a submit without it to be refused).
