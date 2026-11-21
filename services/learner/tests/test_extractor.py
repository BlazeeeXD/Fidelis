from app.extractor import extract_rules


def test_extracts_repository_conventions_from_merged_changes():
    paths = [
        "src/api/users.py", "src/api/orders.py", "src/core/models.py",
        "tests/test_users.py", "tests/test_orders.py", "src/core/auth.py",
    ]
    patches = [
        (11, "+def create_user(user_id: int) -> User:"),
        (12, "+def fetch_orders(order_id: str):"),
        (13, "+def get_account(account_id: int):"),
    ]
    rules = extract_rules(paths, patches, [11, 12, 13])
    keys = {rule["rule_key"] for rule in rules}
    assert "dominant-language" in keys
    assert "test-file-convention" in keys
    assert "python-naming" in keys
    assert all(0 <= rule["confidence"] <= 1 for rule in rules)


def test_no_source_files_yields_no_claims():
    assert extract_rules([], [], []) == []
