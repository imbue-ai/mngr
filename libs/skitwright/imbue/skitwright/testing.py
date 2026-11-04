from imbue.imbue_common.pure import pure


@pure
def random_looking_credential_body(length: int) -> str:
    """A deterministic credential body the placeholder heuristic treats as real: letters and digits alternate, so it has no repeated runs and spells no words."""
    pairs = "".join(chr(ord("A") + (idx * 7) % 26) + str(idx % 10) for idx in range((length + 1) // 2))
    return pairs[:length]
