def pr_payload(action="opened", draft=False, author="alice", author_type="User", head_sha="b" * 40):
    return {
        "action": action,
        "installation": {"id": 173273982},
        "repository": {
            "id": 555, "full_name": "ChaithanyaK230/prreview-demo", "private": False,
            "owner": {"login": "ChaithanyaK230", "type": "User"},
        },
        "pull_request": {
            "number": 1, "draft": draft,
            "user": {"login": author, "type": author_type},
            "base": {"sha": "a" * 40}, "head": {"sha": head_sha},
        },
    }