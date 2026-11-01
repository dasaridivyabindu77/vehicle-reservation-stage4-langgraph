import re
from dataclasses import dataclass

PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
SECRET_RE = re.compile(r"(?i)\b(password|passcode|otp|pin|api[_ -]?key|secret|access[_ -]?token)\b\s*[:=]?\s*[^\s]+")
INJECTION_RE = re.compile(r"(?i)\b(ignore|reveal|print|show)\b.{0,40}\b(system|developer|prompt|secret|instruction)s?\b")


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    redacted: str
    reasons: tuple[str, ...]


def sanitize_input(text: str) -> GuardrailResult:
    value = (text or "").strip()
    if len(value) > 1000:
        return GuardrailResult(False, "", ("message_too_long",))
    reasons = []
    if INJECTION_RE.search(value):
        reasons.append("prompt_injection")
    if SECRET_RE.search(value):
        reasons.append("secret_material")
    date_tokens = {}
    for index, match in enumerate(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", value)):
        token = f"[DATE_TOKEN_{index}]"
        date_tokens[token] = match
        value = value.replace(match, token)
    value = EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = PHONE_RE.sub("[REDACTED_PHONE]", value)
    value = CARD_RE.sub("[REDACTED_CARD]", value)
    value = SECRET_RE.sub("[REDACTED_SECRET]", value)
    for token, original in date_tokens.items():
        value = value.replace(token, original)
    return GuardrailResult(not reasons, value, tuple(reasons))
