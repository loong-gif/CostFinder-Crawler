from utils.monitor_price_extract import extraction_schema


def _required_lists(node):
    if isinstance(node, dict):
        if isinstance(node.get("required"), list):
            yield node["required"]
        for value in node.values():
            yield from _required_lists(value)
    elif isinstance(node, list):
        for value in node:
            yield from _required_lists(value)


def test_extraction_schema_has_no_duplicate_required_entries():
    for required in _required_lists(extraction_schema(["Botox"])):
        assert len(required) == len(set(required)), required
