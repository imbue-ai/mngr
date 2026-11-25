A host dir symlinked onto a host volume can now sit in a directory the image does not have yet (for example `/home/user/.mngr` on a bare image): host setup creates its parent before linking it.
