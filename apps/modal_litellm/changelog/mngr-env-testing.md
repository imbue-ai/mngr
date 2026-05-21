- ``modal_litellm``'s README + module docstring drop the wrong
  ``/anthropic`` suffix from the documented ``ANTHROPIC_BASE_URL`` --
  the Anthropic SDK appends ``/v1/messages`` itself, which lands on
  LiteLLM's native route that already accepts the Anthropic request
  shape. (F1)
