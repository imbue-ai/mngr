`mngr`'s generated command docs no longer call the desktop app by its old name: the `mngr forward`, `mngr latchkey` and `mngr imbue_cloud` reference pages now say "the Imbue Studio desktop client" where they named the product.

`mngr chat`'s page is deliberately untouched: the command no longer exists, so `make_cli_docs.py` cannot regenerate that file and it is left as the orphan it is.
