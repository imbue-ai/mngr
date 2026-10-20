Tightened the direct-subprocess ratchet count from 15 to 13 now that the shared rule no longer counts `subprocess.Popen[...]` type annotations as spawns. (Affects test infrastructure only.)
