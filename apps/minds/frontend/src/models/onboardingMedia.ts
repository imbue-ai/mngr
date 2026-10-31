// The onboarding's hosted media: the to-do list recording an onboarding
// create's creation page shows under its setup line (and the seeded welcome
// chat carries). Nothing is bundled or committed;
// every file lives in the onboarding-media R2 bucket under a content-addressed
// name (scripts/onboarding_media/publish.py), so a changed recording is a new
// name here and an old one keeps resolving for every client that still holds it.

/** The bucket's public hostname: one constant on every tier, since the content is the same everywhere. */
export const ONBOARDING_MEDIA_BASE_URL =
  "https://onboarding-media.imbueminds.com";

/** The published name of the showcase recording (its SHA-256 and extension). */
export const SHOWCASE_RECORDING_NAMES = {
  todoList:
    "ac28361af1792b4167a8fcd3dcf155e859fa14a31c57cb66906902ea0bd74cbe.gif",
} as const;

export function onboardingMediaUrl(name: string): string {
  return `${ONBOARDING_MEDIA_BASE_URL}/${name}`;
}
