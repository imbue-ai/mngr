// The onboarding's hosted media: the to-do list recording the creation page
// shows a reader who asked to see examples (and the seeded welcome chat carries
// after that answer). Nothing is bundled or committed;
// every file lives in the onboarding-media R2 bucket under a content-addressed
// name (scripts/onboarding_media/publish.py), so a changed recording is a new
// name here and an old one keeps resolving for every client that still holds it.

/** The bucket's public hostname: one constant on every tier, since the content is the same everywhere. */
export const ONBOARDING_MEDIA_BASE_URL =
  "https://onboarding-media.imbueminds.com";

/** The published name of the showcase recording (its SHA-256 and extension). */
export const SHOWCASE_RECORDING_NAMES = {
  todoList:
    "aff0d7c67d01df7d22d45bbe47296190e20a5f58413cbf87c64adf41ee505718.gif",
} as const;

export function onboardingMediaUrl(name: string): string {
  return `${ONBOARDING_MEDIA_BASE_URL}/${name}`;
}
