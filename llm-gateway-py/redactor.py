"""
Streaming PII redaction.

A pattern can be split across chunks ("john@exa" + "mple.com"). The redactor
sends text as soon as it is safe and holds back only the tail that could still
become a match, found with the `regex` package's partial matching.
"""

import regex

REDACTED = "[REDACTED]"

EMAIL = r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}"
SSN = r"\b\d{3}-\d{2}-\d{4}\b"
CREDIT_CARD = r"\b(?:\d[ -]?){12,18}\d\b"  # 13-19 digits, optional space/dash separators

# Bounded quantifiers cap the held-back text at ~343 characters.
PII = regex.compile(f"{EMAIL}|{SSN}|{CREDIT_CARD}")

# PII + U+FFFF (never in text) can only match partially, so a partial search returns
# the earliest point that could still become a match. A plain partial search would
# prefer a later complete match and release text too early.
HOLD = regex.compile(f"(?:{PII.pattern})\\uFFFF")


class StreamRedactor:
    """Redacts PII in one text stream that arrives in chunks."""

    def __init__(self) -> None:
        self.pending = ""  # held text that could still become a match
        self.context = ""  # last sent character, so \b sees what precedes `pending`
        self.count = 0     # redactions made

    def feed(self, chunk: str) -> str:
        """Add a chunk; return the text now safe to send."""
        return self._redact(chunk, final=False)

    def flush(self) -> str:
        """End of stream: return the held text, redacted."""
        return self._redact("", final=True)

    def _redact(self, chunk: str, final: bool) -> str:
        buf = self.context + self.pending + chunk
        start = len(self.context)
        hold = len(buf) if final else self._hold_point(buf, start)
        out, pos = [], start

        # Redact complete matches that start before the hold point.
        for m in PII.finditer(buf, pos=start):
            if m.start() >= hold:
                break
            out.append(buf[pos:m.start()])
            out.append(REDACTED)
            self.count += 1
            pos = m.end()
            # Match ran into held text; it can't grow, so recompute the hold point after it.
            if pos > hold:
                hold = len(buf) if final else self._hold_point(buf, pos)

        hold = max(hold, pos)
        out.append(buf[pos:hold])
        self.pending = buf[hold:]
        if hold > start:
            self.context = buf[hold - 1]
        return "".join(out)

    @staticmethod
    def _hold_point(buf: str, pos: int) -> int:
        """Earliest index from `pos` that could still become a match; len(buf) if none."""
        tail = HOLD.search(buf, pos=pos, partial=True)
        return tail.start() if tail else len(buf)
