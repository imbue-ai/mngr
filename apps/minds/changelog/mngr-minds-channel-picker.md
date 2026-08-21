The Updates panel now lists beta, and holds alpha behind an "Internal channels"
disclosure.

Beta also gets a build to serve. It was declared publishable but had no entry,
so its feed resolved to nothing -- listing a channel in that state would have
shown people one that reads as permanently broken. It now carries the same build
stable does, so the two are identical until someone promotes them apart.

Beta was hidden because nothing had decided who it was for. It is now the
channel for trying new features before they are final, so it is listed like any
other. Alpha is where internal development lands, and it is about to move much
faster; a radio button sitting directly under the one most people want is too
easy to hit on the way past. It stays one click away rather than none -- and the
disclosure opens on its own when alpha is the channel in effect, so what you are
running is never hidden behind something you have to open first.

The channel descriptions say what each channel is, in one line each:

- Stable -- Ready for everyday use.

- Beta -- Test new features early.

- Alpha -- Internal development builds.

The panel also no longer drops the channel you are on. A preference written by a
build that could reach the faster channels survives into one that cannot, and
the list is built from what this build serves -- so that channel used to vanish
from its own list, leaving every radio unselected and nothing naming what was
actually in use.
