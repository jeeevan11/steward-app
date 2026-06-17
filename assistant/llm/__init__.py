"""LLM access layer.

  * prompts.py — load editable prompts from the prompts/ directory
  * client.py  — thin Anthropic wrapper: cheap haiku noise pass, opus judgment +
                 drafting, forced JSON via output_config.format, prompt caching for
                 the stable few-shot/voice prefix.

The `anthropic` package is imported lazily inside client.py so importing the
testable core never requires it to be installed.
"""
