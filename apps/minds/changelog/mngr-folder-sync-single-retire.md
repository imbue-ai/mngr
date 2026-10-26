- Fixed a race in folder sync where a click landing just as a folder's previous move finished could have its own move marked as finished early, so a wait on the folder returned while its sync was still coming up, and a further click could start a second, duplicate mover for the same folder.

- A click on a folder whose sync had just given up after too many moves is no longer dropped; it is carried out.

- Quitting Imbue Studio now reliably stops every folder sync: a sync that was part-way to starting when the app shut down no longer brings up a `mngr pair` (and the unison under it) that outlives the app.
