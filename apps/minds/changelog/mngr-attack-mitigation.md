# Deployment test updated for the Google-first signup page

- The hosted-pages Playwright deployment test now clicks the sign-up tab's "Use email and password instead" reveal link when present: with Google configured on a tier, the accounts page keeps the email/password fields collapsed behind it (part of the sign-up abuse mitigation work in the connector).
