Replaced the brand lockup with the new "studio" wordmark. The loading document's
intro lockup, the mark it parks in the titlebar band, and the running app's start
titlebar all draw the new shape, in the same brand green on the same white page as
before. `mind-wordmark.svg` is gone; the artwork ships as `studio-wordmark.svg`.

The intro's schedule is untouched -- only the lockup's box changed, from 32% of the
page at 577 x 139 to 27.5% at 1035 x 290, which holds the wider drawing at the
height the old one had.

The intro's second line now reads "honest software" in place of the
ungrammatical "An honest software".

The intro now plays on the brand's brown rather than a white page: the lockup and
the two lines are cream on `#492222`. Once the mark has parked in the titlebar
band the page settles over 600ms -- the brown goes white while the mark goes
brown with it, so the drawing never loses contrast against the page, and the
state it lands on is the one every later launch opens on at once.

The intro is no longer a fixed film. The wordmark's six letters now pop in one
after the next on a spring rig (ported from the splash-page sketch), and once
they have landed the loader fades in under them and the page waits there for
however long starting up takes. When the app is ready the loader goes, *honest
software* plays, and only then does the lockup travel up to the titlebar -- so
that move always means the same thing: the app is ready for you. The opening
line, "Create an intentional life", is gone.

The mark is bigger on the splash -- 48% of the window's width rather than 27.5%
-- and no longer clipped while it animates: a letter squashing and ringing on
arrival reaches outside the drawing's own bounds, and the box it is drawn in now
leaves room for that.

The splash holds its page empty for a second before the first letter lands, so
the mark arrives onto a page that is already there.

Fixed the loader reappearing for a moment at the very end of the intro. It
reports what startup is doing, and the intro now only finishes once startup is
over, so there was nothing left for it to say.

Nothing jumps when the loader gives way to the line: the slot under the mark
reserves a line's height whether or not anything is in it, and the loader's
status line sits exactly where *honest software* will be.

The loader also stops flashing past on a quick start. A launch that is ready
within half a second of the mark landing never shows it, and one that does show
it holds it for at least a second and a half, so what it says can be read.

Clicking or pressing a key no longer cuts the intro short. The page waits on the
loader for as long as starting up takes, and the controls on screen during that
wait -- "Show details" in particular -- are meant to be pressed.

The *honest software* line is gone from the splash, and with it everything that
served only it. The departure is now the loader leaving, the mark travelling and
the page settling -- 1.65s after the app is ready, down from 3.95s.

The loader is restyled: a 14px status line, a 12px bar with fully rounded ends on
both the track and the slider inside it, and a 12px "Show details".

The startup log is now opened from the status line itself, which underlines on
hover. The separate "Show details" link is gone.

The mark and the loader are also one centred column now, so opening the log lifts
the mark rather than pushing the log off the bottom of the window -- and because
the loader holds its place in that column from the first frame, nothing shifts
when it appears.

The loader sits closer under the mark, and its bar is 160px wide.

The splash survives a short window now. The mark is capped against the window's
height as well as its width, and the startup log shrinks to fit rather than
running off the bottom -- previously a wide, short window cropped the mark's top
and the log's bottom at the same time.

The progress bar and the status line are one target now -- pressing either opens
the startup log.

Under "reduce motion" the bar no longer slides: an indeterminate indicator loops
forever, which is the case that setting is most clearly about. The phase line
carries the progress instead. A long phase line also wraps rather than running
past the edge of the window.

Every launch now shows the same screen. There is no longer a first-launch intro
and a plainer screen for every launch after it: the app has to start either way,
so the quitting screen and the error screen are on the brand's page under the
mark too, and the wordmark animates in whatever brought you here.

What a later launch skips is the mark's travel to the titlebar, which only means
anything when the start flow is about to hold it there. Every other route paints
over the page as it stands.

The wordmark is redrawn from the latest artwork -- the s and the t have changed
slightly. The flat asset the parked mark shows is now built from the same six
letter paths as the animation, so the two can no longer drift apart.

The quitting screen no longer fades its report in -- it is there from the first
frame. A quit is not waiting for anything to become worth saying.

The start flow now opens with the agent saying the thing the exchange is about:
"Imbue Studio is honest software." Two seconds later -- a beat long enough to
read it and be brought up short by it -- your side asks "Wait.. what is honest
software?", which the five points answer. The exchange used to start
mid-thought, with a question about a phrase nobody had said. The same opening
leads the workspace's seeded first chat and the creation page's restatement of
the conversation, so all three still match.

The mark in the start flow's titlebar is 20px tall rather than 16px, and the
loading page's mark travels to that height, so the hand-off between the two
documents is still a substitution rather than a resize.

The app icon is redrawn from the new artwork -- the cream figure on the brand's
brown, in place of the old head-and-circles mark.

Each platform now gets the corner it expects, rather than one file for all of
them. macOS takes the brand's squircle on Apple's grid: an 824x824 body centred
in the 1024 canvas, where before the icon bled to all 1024 and so sat visibly
larger in the Dock than every native app beside it. Linux takes a full-bleed
rounded square instead, since no Linux desktop masks the icon to a shape of its
own -- what the file holds is what is drawn. The dev dock icon keeps its olive
and its `dev` label on the new figure.

The quitting screen runs the loader bar too. A quit is a wait like any other --
the backend gets a SIGTERM and up to five seconds to go, and local workspaces
may be stopping -- so the screen moves while it waits instead of holding a
line of text still. The startup log stays off it: there is nothing to disclose.

Fixed a flash of no logo between the loading page and the start flow. The mark
reached the titlebar and then vanished for 714ms before the app's own mark
appeared. Two things caused it: the page swapped the animated lockup for the
flat one at the *start* of the settle rather than at its end, and the flat one
was painted underneath the layer carrying the page's colour, so for that stretch
neither was on screen. The mark now stays on the page throughout, crossing from
cream to brown as the page goes the other way, which is what the settle was
always meant to look like. What is left is the 62ms the next document takes to
paint its own.

The start flow's opening line sets *honest software* in italics -- the phrase
the rest of the exchange is about, picked out where it is first said. The
workspace's seeded copy of the conversation matches. The streaming is
untouched: every character still lands exactly when it did.

Fixed the mark shifting by up to half a pixel as the loading page gives way to
the start flow. The travel to the titlebar was aimed with `offsetTop` and
`offsetHeight`, which round to whole pixels, so where the mark came to rest
depended on how the window's width happened to round -- it parked a fraction
high in some windows and a fraction low in others, and the app's own mark then
snapped to the right place. The travel now measures in fractions of a pixel and
lands on it exactly, at every window size.

The start flow now closes its manifesto by asking about error reporting, and
that answer is what starts the questions:

> Is it ok if we report errors to help improve Studio?
> *See more*

"See more" opens, in italics under the question, what the reports carry and
where the answer can be changed. "No" and "Sounds great" answer it, and whichever
is pressed is saved and becomes the user's turn.

This replaces two things. The "Sounds great, let's continue" button is gone: the
exchange now ends by asking rather than by inviting a press. And the
error-reporting checkbox that sat above the where-to-run answers is gone with
it. That checkbox was where nearly every install actually consented, yet it was
the one screen that said nothing about what is collected -- no explanation, no
link -- while the `/consent` screen, which carries both, is reached only by an
install that got past onboarding without answering. The question is now asked
once, on its own, in the exchange that has just finished promising transparency.
