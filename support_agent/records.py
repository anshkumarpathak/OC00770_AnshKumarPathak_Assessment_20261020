"""Schema-checked record editor behind the portal's "Inspect or edit the source records".

A reviewer can change a record in a mock enterprise system and re-run the agent,
to see that its answers come from the data and not from a script. An edited
record is only written if it passes the same schema the agent's API client
enforces on every response, so a bad edit is rejected with the reason instead
of breaking the agent. Fields can change; the record's key and its set of fields
can't, because the mock systems' own logic relies on them.
"""
import json

from pydantic import BaseModel, ValidationError


def check_record(text: str, original: dict, schema: type[BaseModel], key_field: str) -> tuple[dict | None, list[str]]:
    """Return (record, []) when the edit can be saved, else (None, reasons)."""
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"Not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})."]
    if not isinstance(record, dict):
        return None, ["The record must be one JSON object, in curly braces."]
    if record.get(key_field) != original[key_field]:
        return None, [f"{key_field} identifies the record, so it can't be changed (keep {original[key_field]!r})."]
    reasons = []
    if missing := sorted(set(original) - set(record)):
        reasons.append(f"Fields can't be removed: {', '.join(missing)}.")
    if added := sorted(set(record) - set(original)):
        reasons.append(f"Fields can't be added: {', '.join(added)}.")
    if reasons:
        return None, reasons
    try:
        schema.model_validate(record)
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc'])) or 'record'}: {e['msg']}" for e in exc.errors()]
    return record, []
