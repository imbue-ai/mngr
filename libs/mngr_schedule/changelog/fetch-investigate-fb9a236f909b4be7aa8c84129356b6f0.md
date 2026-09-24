Tightened the direct-subprocess ratchet count from 7 to 6 now that the shared rule no longer counts `subprocess.Popen[...]` type annotations as spawns. (Affects test infrastructure only.)
