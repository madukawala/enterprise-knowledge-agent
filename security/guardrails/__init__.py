"""Guardrails: checks on what goes into the language model and what comes out.

- text.py: cleans text (invisible characters, HTML, look-alike letters) and
  prepares it for scanning.
- injection.py: rules that spot prompt-injection attempts in a question or
  in a retrieved document.
- output.py: checks on an answer: repeated internal instructions, document
  labels above the user's role, advice the bank does not give, unsafe links.
- Personal data masking lives in core/pii.py, because logging uses it too.

The agents call these from the input guard (the question), the retrieval
agent (each retrieved chunk) and the validator (the answer). The approach
is written up in docs/security/PROMPT_INJECTION.md.
"""
